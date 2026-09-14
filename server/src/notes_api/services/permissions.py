"""Who may do what with a note.

An owner (author or co-owner) may do everything. Anyone else holds the union of their direct share and
the shares addressed to teams they currently belong to: ``read`` is implied by any share, ``comment`` and
``propose_edit`` come from the share flags. Team roles never matter: an admin of a recipient team gains
nothing beyond the share. Visibility follows the lifecycle: an expired note is invisible to everyone, a
trashed note is visible to its owners only, an active note to anyone who may read it.

``lock=True`` locks the share and membership rows the decision used (``FOR SHARE`` on PostgreSQL; SQLite
serializes writers anyway), so a revocation racing a child mutation cannot commit underneath it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from notes_api.models import Membership, Note, NoteOwner, Share

USER = "user"
TEAM = "team"


@dataclass(frozen=True, slots=True)
class Access:
    is_owner: bool
    read: bool
    comment: bool
    propose: bool

    @classmethod
    def owner(cls) -> Access:
        return cls(is_owner=True, read=True, comment=True, propose=True)

    @classmethod
    def none(cls) -> Access:
        return cls(is_owner=False, read=False, comment=False, propose=False)

    @classmethod
    def from_shares(cls, shares: list[Share]) -> Access:
        if not shares:
            return cls.none()
        return cls(
            is_owner=False,
            read=True,
            comment=any(share.can_comment for share in shares),
            propose=any(share.can_propose for share in shares),
        )

    def permissions(self) -> list[str]:
        """The canonical ``PermissionSet``: ``read``, ``comment``, ``propose_edit``, in that order."""
        granted = []
        if self.read:
            granted.append("read")
        if self.comment:
            granted.append("comment")
        if self.propose:
            granted.append("propose_edit")
        return granted


def resolve(session: Session, note_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False) -> Access:
    if session.get(NoteOwner, (note_id, user_id)) is not None:
        return Access.owner()
    return Access.from_shares(shares_for(session, note_id, user_id, lock=lock))


def shares_for(
    session: Session, note_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> list[Share]:
    """The caller's direct share and the shares to teams they currently belong to."""
    direct = select(Share).where(
        Share.note_id == note_id, Share.recipient_type == USER, Share.recipient_id == user_id
    )
    through_teams = (
        select(Share)
        .join(Membership, Membership.team_id == Share.recipient_id)
        .where(Share.note_id == note_id, Share.recipient_type == TEAM, Membership.user_id == user_id)
    )
    if lock:
        direct = direct.with_for_update(read=True)
        through_teams = through_teams.with_for_update(read=True)
    return [*session.execute(direct).scalars(), *session.execute(through_teams).scalars()]


def access_predicates(caller_id: uuid.UUID) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """``(owned, readable)`` as ``EXISTS`` predicates correlated on ``Note``, for statements that select
    notes or rows joined to them: an owner row; an owner row, a direct share, or a share to a team the
    caller currently belongs to. A note matches once however many paths grant it."""
    owner_row = select(NoteOwner.note_id).where(NoteOwner.note_id == Note.id, NoteOwner.user_id == caller_id)
    direct_share = select(Share.id).where(
        Share.note_id == Note.id, Share.recipient_type == USER, Share.recipient_id == caller_id
    )
    team_share = (
        select(Share.id)
        .join(Membership, Membership.team_id == Share.recipient_id)
        .where(Share.note_id == Note.id, Share.recipient_type == TEAM, Membership.user_id == caller_id)
    )
    owned = owner_row.exists()
    return owned, or_(owned, direct_share.exists(), team_share.exists())


def is_trashed(note: Note) -> bool:
    return note.deleted_at is not None


def is_expired(note: Note, now: datetime) -> bool:
    return note.expires_at is not None and now >= note.expires_at


def visible(note: Note, access: Access, now: datetime) -> bool:
    """Whether the caller may know the note exists: expired never, trashed owners only, active readers."""
    if is_expired(note, now):
        return False
    if is_trashed(note):
        return access.is_owner
    return access.read
