"""Access is owner-or-union-of-shares, team roles grant nothing, visibility follows the lifecycle, and the
note serializers produce the contract's ``Note`` and ``NoteSummary``."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session

from notes_api import serializers
from notes_api.contract import Contract
from notes_api.models import Membership, Note, NoteOwner, Share, Team, User
from notes_api.services.permissions import Access, resolve, visible

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
TRASHED_AT = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def user(session: Session, subject: str) -> uuid.UUID:
    row = User(id=uuid.uuid4(), issuer="i", subject=subject, display_name=subject.title(), created_at=NOW)
    session.add(row)
    session.flush()
    return row.id


def note(session: Session, author_id: uuid.UUID, **overrides: object) -> Note:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "author_id": author_id,
        "title": "Release checklist",
        "body": "## Release\n",
        "title_fold": "release checklist",
        "body_fold": "## release\n",
        "review_mode": "self_merge",
        "review_required_approvals": None,
        "created_at": NOW,
        "updated_at": NOW,
        "deleted_at": None,
        "expires_at": None,
        "version": "v1",
    }
    values.update(overrides)
    row = Note(**values)
    session.add(row)
    session.flush()
    session.add(NoteOwner(note_id=row.id, user_id=author_id, position=0, added_at=NOW))
    session.flush()
    return row


def share(
    session: Session,
    note_id: uuid.UUID,
    kind: str,
    recipient: uuid.UUID,
    *,
    comment: bool = False,
    propose: bool = False,
) -> None:
    session.add(
        Share(
            id=uuid.uuid4(),
            note_id=note_id,
            recipient_type=kind,
            recipient_id=recipient,
            can_comment=comment,
            can_propose=propose,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()


def team(session: Session, *members: tuple[uuid.UUID, str]) -> uuid.UUID:
    row = Team(id=uuid.uuid4(), name="Platform", created_at=NOW, updated_at=NOW)
    session.add(row)
    session.flush()
    for user_id, role in members:
        session.add(Membership(team_id=row.id, user_id=user_id, role=role, joined_at=NOW, updated_at=NOW))
    session.flush()
    return row.id


@pytest.fixture
def session(app: FastAPI) -> Iterator[Session]:
    with app.state.session_factory() as session, session.begin():
        yield session


def test_access_is_the_owner_or_the_union_of_share_paths(session: Session) -> None:
    ada, ben, cara, dan, eve, fay = (
        user(session, name) for name in ("ada", "ben", "cara", "dan", "eve", "fay")
    )
    row = note(session, ada)
    share(session, row.id, "user", ben)  # read only
    platform = team(session, (cara, "member"), (dan, "member"), (eve, "admin"))
    share(session, row.id, "team", platform, propose=True)
    share(session, row.id, "user", dan, comment=True)  # dan: direct comment + team propose
    other = team(session, (fay, "admin"))  # fay is an admin somewhere, but of a team without a share

    assert resolve(session, row.id, ada) == Access(is_owner=True, read=True, comment=True, propose=True)
    assert resolve(session, row.id, ben) == Access(is_owner=False, read=True, comment=False, propose=False)
    assert resolve(session, row.id, cara) == Access(is_owner=False, read=True, comment=False, propose=True)
    assert resolve(session, row.id, dan) == Access(is_owner=False, read=True, comment=True, propose=True)
    assert resolve(session, row.id, eve) == resolve(
        session, row.id, cara
    )  # admin of the team: same as a member
    assert resolve(session, row.id, fay) == Access.none()
    assert resolve(session, row.id, uuid.uuid4()) == Access.none()
    assert other is not None
    # Losing the membership loses the team path but keeps a direct one.
    session.delete(session.get(Membership, (platform, dan)))
    session.flush()
    assert resolve(session, row.id, dan) == Access(is_owner=False, read=True, comment=True, propose=False)
    session.delete(session.get(Membership, (platform, cara)))
    session.flush()
    assert resolve(session, row.id, cara) == Access.none()
    # The locking variant returns the same answer (FOR SHARE on PostgreSQL, nothing on SQLite).
    assert resolve(session, row.id, dan, lock=True) == resolve(session, row.id, dan)


def test_permissions_render_in_canonical_order() -> None:
    assert Access.owner().permissions() == ["read", "comment", "propose_edit"]
    assert Access(False, True, False, False).permissions() == ["read"]
    assert Access(False, True, True, False).permissions() == ["read", "comment"]
    assert Access(False, True, False, True).permissions() == ["read", "propose_edit"]
    assert Access(False, True, True, True).permissions() == ["read", "comment", "propose_edit"]
    assert Access.none().permissions() == []


def test_visibility_follows_the_lifecycle(session: Session) -> None:
    ada, ben = user(session, "ada"), user(session, "ben")
    active = note(session, ada)
    share(session, active.id, "user", ben)
    owner, reader, stranger = (
        resolve(session, active.id, ada),
        resolve(session, active.id, ben),
        Access.none(),
    )
    assert [visible(active, a, NOW) for a in (owner, reader, stranger)] == [True, True, False]

    expires = TRASHED_AT + timedelta(hours=720)
    trashed = note(session, ada, deleted_at=TRASHED_AT, expires_at=expires)
    assert [visible(trashed, a, NOW) for a in (owner, reader, stranger)] == [True, False, False]
    assert visible(trashed, owner, expires - timedelta(microseconds=1)) is True
    assert visible(trashed, owner, expires) is False
    assert visible(trashed, owner, expires + timedelta(days=1)) is False


def test_serializers_produce_the_contracts_note_shapes(session: Session) -> None:
    contract = Contract.load()
    ada, ben, cara = user(session, "ada"), user(session, "ben"), user(session, "cara")
    row = note(session, ada)
    body = serializers.note(row, [ada], ["release", "ops"], Access.owner())
    assert contract.validate_instance(contract.schemas["Note"], body) == []
    assert body["ownerIds"] == [str(ada)] and body["tags"] == ["release", "ops"]
    assert body["reviewPolicy"] == {"mode": "self_merge", "requiredApprovals": None}
    assert body["deletedAt"] is None and body["expiresAt"] is None
    assert body["isOwner"] is True and body["effectivePermissions"] == ["read", "comment", "propose_edit"]
    assert body["body"] == "## Release\n"

    summary = serializers.note_summary(row, [ada], [], Access(False, True, False, True))
    assert contract.validate_instance(contract.schemas["NoteSummary"], summary) == []
    assert "body" not in summary
    assert summary["isOwner"] is False and summary["effectivePermissions"] == ["read", "propose_edit"]

    protected = note(
        session,
        ada,
        review_mode="peer_approval",
        review_required_approvals=1,
        deleted_at=TRASHED_AT,
        expires_at=TRASHED_AT + timedelta(hours=720),
    )
    trashed = serializers.note(protected, [ada, cara], [], Access.owner())
    assert contract.validate_instance(contract.schemas["Note"], trashed) == []
    assert trashed["reviewPolicy"] == {"mode": "peer_approval", "requiredApprovals": 1}
    assert trashed["deletedAt"] == "2026-09-11T12:00:00.000000Z"
    assert trashed["expiresAt"] == "2026-10-11T12:00:00.000000Z"
    assert trashed["ownerIds"] == [str(ada), str(cara)]
    assert ben is not None
