"""Teams and memberships.

Every mutation locks the team row first (``SELECT ... FOR UPDATE`` on PostgreSQL; SQLite serializes
writers with ``BEGIN IMMEDIATE``), then checks who the caller is, then acts, so two admins demoting or
removing each other, or the last admin leaving twice, are decided one at a time and a team can never be
left without an admin. Team mutations never touch a note, so no lock order with notes exists.

Authorization comes before existence for membership targets: a nonmember cannot learn who belongs to a
team by probing ``/members/{userId}``, so "may the caller act" (``403``) is checked before "is the target a
member" (``404``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from notes_api import cursors
from notes_api.clock import Clock
from notes_api.contract import FieldError
from notes_api.http.problems import Conflict, Forbidden, NotFound, ValidationFailed
from notes_api.models import Membership, Share, Team, User

ADMIN = "admin"
MEMBER = "member"
TEAMS_COLLECTION = "teams"
MEMBERS_COLLECTION = "memberships"


# -- teams --------------------------------------------------------------------------------------------


def create_team(session: Session, *, creator: User, name: str, clock: Clock) -> Team:
    """A team and its first admin, in the caller's transaction."""
    now = clock.now()
    team = Team(id=uuid.uuid4(), name=name, created_at=now, updated_at=now)
    session.add(team)
    session.flush()  # no ORM relationships, so the parent row must reach the database before its membership
    session.add(Membership(team_id=team.id, user_id=creator.id, role=ADMIN, joined_at=now, updated_at=now))
    session.flush()
    return team


def list_teams(
    session: Session, *, caller: User, scope: str, limit: int, cursor: str | None
) -> cursors.Page[Team]:
    statement = select(Team)
    if scope == "mine":
        member_of = exists().where(Membership.team_id == Team.id, Membership.user_id == caller.id)
        statement = statement.where(member_of)
    return cursors.paginate(
        session,
        statement,
        moment=Team.created_at,
        id_column=Team.id,
        key_of=lambda row: cursors.Key(row.created_at, row.id),
        limit=limit,
        cursor=cursor,
        fingerprint=cursors.fingerprint(caller.id, TEAMS_COLLECTION, {"scope": scope}, limit),
    )


def get_team(session: Session, team_id: uuid.UUID) -> Team:
    team = session.get(Team, team_id)
    if team is None:
        raise NotFound()
    return team


def lock_team_as_admin(session: Session, *, caller: User, team_id: uuid.UUID) -> Team:
    """The team, locked for the rest of the transaction.

    ``404`` if it does not exist, ``403`` unless the caller is one of its admins.
    """
    team = _lock_team(session, team_id)
    membership = _membership(session, team_id, caller.id)
    if membership is None or membership.role != ADMIN:
        raise Forbidden()
    return team


def rename_team(session: Session, *, team: Team, name: str, clock: Clock) -> Team:
    """Rename a team already locked by ``lock_team_as_admin``; an unchanged name changes nothing."""
    if team.name != name:
        team.name = name
        team.updated_at = clock.now()
    return team


def delete_team(session: Session, *, team: Team) -> None:
    """Delete a team already locked by ``lock_team_as_admin``.

    Its memberships and the shares addressed to it go with it; notes, comments, and edit requests stay.
    """
    session.execute(delete(Share).where(Share.recipient_type == "team", Share.recipient_id == team.id))
    session.execute(delete(Membership).where(Membership.team_id == team.id))
    session.delete(team)
    session.flush()


# -- memberships --------------------------------------------------------------------------------------


def list_members(
    session: Session, *, caller: User, team_id: uuid.UUID, limit: int, cursor: str | None
) -> cursors.Page[Membership]:
    """Members only; a nonmember gets ``403`` because team metadata is discoverable by everyone."""
    get_team(session, team_id)
    if _membership(session, team_id, caller.id) is None:
        raise Forbidden()
    filters = {"teamId": str(team_id)}
    return cursors.paginate(
        session,
        select(Membership).where(Membership.team_id == team_id),
        moment=Membership.joined_at,
        id_column=Membership.user_id,
        key_of=lambda row: cursors.Key(row.joined_at, row.user_id),
        limit=limit,
        cursor=cursor,
        fingerprint=cursors.fingerprint(caller.id, MEMBERS_COLLECTION, filters, limit),
    )


def add_member(session: Session, *, team: Team, user_id: uuid.UUID, role: str, clock: Clock) -> Membership:
    """Add a registered user to a team already locked by ``lock_team_as_admin``."""
    if session.get(User, user_id) is None:
        raise ValidationFailed([FieldError("body", "/userId", "unknown user")])
    if _membership(session, team.id, user_id) is not None:
        raise Conflict("duplicate_membership")
    now = clock.now()
    membership = Membership(team_id=team.id, user_id=user_id, role=role, joined_at=now, updated_at=now)
    session.add(membership)
    session.flush()
    return membership


def set_role(session: Session, *, team: Team, user_id: uuid.UUID, role: str, clock: Clock) -> Membership:
    """Change a member's role on a team already locked by ``lock_team_as_admin``; the last admin stays."""
    membership = _membership(session, team.id, user_id)
    if membership is None:
        raise NotFound()
    if membership.role == role:
        return membership
    if membership.role == ADMIN and _admin_count(session, team.id) == 1:
        raise Conflict("last_admin")
    membership.role = role
    membership.updated_at = clock.now()
    return membership


def remove_member(session: Session, *, caller: User, team_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Admins remove anyone; a member removes only themself; the last admin cannot leave."""
    team = _lock_team(session, team_id)
    own = _membership(session, team.id, caller.id)
    if (own is None or own.role != ADMIN) and user_id != caller.id:
        raise Forbidden()
    membership = _membership(session, team.id, user_id)
    if membership is None:
        raise NotFound()
    if membership.role == ADMIN and _admin_count(session, team.id) == 1:
        raise Conflict("last_admin")
    session.delete(membership)
    session.flush()


# -- internals ----------------------------------------------------------------------------------------


def _lock_team(session: Session, team_id: uuid.UUID) -> Team:
    statement = select(Team).where(Team.id == team_id).with_for_update()
    team = session.execute(statement).scalar_one_or_none()
    if team is None:
        raise NotFound()
    return team


def _membership(session: Session, team_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None:
    return session.get(Membership, (team_id, user_id))


def _admin_count(session: Session, team_id: uuid.UUID) -> int:
    statement = (
        select(func.count())
        .select_from(Membership)
        .where(Membership.team_id == team_id, Membership.role == ADMIN)
    )
    count: int = session.execute(statement).scalar_one()
    return count
