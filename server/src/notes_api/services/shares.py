"""Shares: an owner's grants of ``read``, ``comment``, and ``propose_edit`` to users and teams.

The router resolves the note first (``notes.read`` for reads, ``notes.lock`` for mutations, then
``require_owner``), so everything here already knows the caller owns the note. Every mutation runs under
the note lock, which serializes duplicate checks and, later, revocations against child mutations. ``read``
is implied by any share and never stored; the two flags carry the other capabilities.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from notes_api import cursors
from notes_api.contract import FieldError
from notes_api.http.problems import Conflict, NotFound, ValidationFailed
from notes_api.models import Share, Team, User
from notes_api.services import permissions
from notes_api.services.notes import NoteView

COLLECTION = "shares"
RECIPIENT_POINTER = "/recipient/id"
OWNER_RECIPIENT_DETAIL = "The note owner already has every permission; sharing with them is redundant."


def list_shares(
    session: Session, *, caller: User, view: NoteView, limit: int, cursor: str | None
) -> cursors.Page[Share]:
    """The note's shares, newest first; a trashed note simply has none."""
    return cursors.paginate(
        session,
        select(Share).where(Share.note_id == view.note.id),
        moment=Share.created_at,
        id_column=Share.id,
        key_of=lambda row: cursors.Key(row.created_at, row.id),
        limit=limit,
        cursor=cursor,
        fingerprint=cursors.fingerprint(caller.id, COLLECTION, {"noteId": str(view.note.id)}, limit),
    )


def get_share(session: Session, *, view: NoteView, share_id: uuid.UUID) -> Share:
    """The share, which must belong to the note in the path; anything else is ``404``."""
    share = session.execute(
        select(Share).where(Share.id == share_id, Share.note_id == view.note.id)
    ).scalar_one_or_none()
    if share is None:
        raise NotFound()
    return share


def create_share(
    session: Session,
    *,
    view: NoteView,
    recipient_type: str,
    recipient_id: uuid.UUID,
    granted: list[str],
    now: datetime,
) -> Share:
    """Grant a user or a team; the recipient must exist and must not already hold the note as an owner."""
    if permissions.is_trashed(view.note):
        raise Conflict("note_not_active")
    if recipient_type == permissions.USER:
        if session.get(User, recipient_id) is None:
            raise ValidationFailed([FieldError("body", RECIPIENT_POINTER, "unknown recipient")])
        if recipient_id in view.owner_ids:
            raise ValidationFailed(
                [FieldError("body", RECIPIENT_POINTER, "recipient is the note owner")],
                detail=OWNER_RECIPIENT_DETAIL,
            )
    elif session.get(Team, recipient_id) is None:
        raise ValidationFailed([FieldError("body", RECIPIENT_POINTER, "unknown recipient")])
    duplicate = select(Share.id).where(
        Share.note_id == view.note.id,
        Share.recipient_type == recipient_type,
        Share.recipient_id == recipient_id,
    )
    if session.execute(duplicate).first() is not None:
        raise Conflict("duplicate_share")
    share = Share(
        id=uuid.uuid4(),
        note_id=view.note.id,
        recipient_type=recipient_type,
        recipient_id=recipient_id,
        can_comment="comment" in granted,
        can_propose="propose_edit" in granted,
        created_at=now,
        updated_at=now,
    )
    session.add(share)
    session.flush()
    return share


def update_share(
    session: Session, *, view: NoteView, share_id: uuid.UUID, granted: list[str], now: datetime
) -> Share:
    """Replace the permission set; ``updated_at`` moves only when something changed."""
    share = get_share(session, view=view, share_id=share_id)
    can_comment, can_propose = "comment" in granted, "propose_edit" in granted
    if (share.can_comment, share.can_propose) != (can_comment, can_propose):
        share.can_comment, share.can_propose, share.updated_at = can_comment, can_propose, now
        session.flush()
    return share


def delete_share(session: Session, *, view: NoteView, share_id: uuid.UUID) -> None:
    session.delete(get_share(session, view=view, share_id=share_id))
    session.flush()
