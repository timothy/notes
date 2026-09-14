"""Owners and the review policy (design guide, section 2, "Owners and the review policy").

Every note has one author and up to nineteen co-owners. The author administers ownership and the review
policy; a co-owner may remove only themselves; the author can never be removed. These are conditional note
mutations under the note lock: they advance the note's version and ``updated_at`` and answer with the note.
With two or more owners the note is protected (``notes.update`` refuses title and body), and the review
policy decides how an edit request may then merge.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notes_api import etags
from notes_api.contract import FieldError
from notes_api.http.problems import Conflict, Forbidden, NotFound, ValidationFailed
from notes_api.models import Note, NoteOwner, User
from notes_api.services import approvals, permissions
from notes_api.services.notes import NoteView
from notes_api.services.permissions import Access

MAX_OWNERS = 20
PEER_APPROVAL = "peer_approval"
USER_POINTER = "/userId"
POLICY_POINTER = "/requiredApprovals"
TOO_MANY_APPROVALS_DETAIL = "requiredApprovals cannot exceed the number of owners."
TOO_MANY_OWNERS_DETAIL = f"A note has at most {MAX_OWNERS} owners."
# A departing owner without any share still receives the note they just left: this response is the last
# read they get, so it reports the one permission it exercised.
LAST_READ = Access(is_owner=False, read=True, comment=False, propose=False)


def require_author(view: NoteView, caller: User) -> None:
    """Adding an owner and setting the policy are the author's alone; co-owners and readers are ``403``."""
    if view.note.author_id != caller.id:
        raise Forbidden()


def authorize_removal(view: NoteView, caller: User, user_id: uuid.UUID) -> None:
    """A target who is not an owner is ``404`` (``ownerIds`` is in every note representation, so nothing is
    hidden by resolving it first); then the author may remove anyone and everyone else only themselves."""
    if user_id not in view.owner_ids:
        raise NotFound()
    if caller.id != view.note.author_id and caller.id != user_id:
        raise Forbidden()


def add_owner(session: Session, *, view: NoteView, user_id: uuid.UUID, now: datetime) -> NoteView:
    """Add a co-owner to a note whose author and version were already checked.

    Trashed is ``409``; an unknown user ``422``; an existing owner ``409 duplicate_owner``; a twenty-first
    owner ``422``. Positions continue from the highest one in use, so removals leave gaps and ``ownerIds``
    keeps the order of addition. Any share the new owner holds stays in place.
    """
    note = view.note
    if permissions.is_trashed(note):
        raise Conflict("note_not_active")
    if session.get(User, user_id) is None:
        raise ValidationFailed([FieldError("body", USER_POINTER, "unknown user")], detail="No such user.")
    if user_id in view.owner_ids:
        raise Conflict("duplicate_owner")
    if len(view.owner_ids) >= MAX_OWNERS:
        raise ValidationFailed(
            [FieldError("body", USER_POINTER, f"the note already has {MAX_OWNERS} owners")],
            detail=TOO_MANY_OWNERS_DETAIL,
        )
    next_position = select(func.coalesce(func.max(NoteOwner.position), 0) + 1).where(
        NoteOwner.note_id == note.id
    )
    position = session.execute(next_position).scalar_one()
    session.add(NoteOwner(note_id=note.id, user_id=user_id, position=position, added_at=now))
    _touch(note, now)
    session.flush()
    return NoteView(note, [*view.owner_ids, user_id], view.tags, view.access)


def remove_owner(
    session: Session, *, view: NoteView, caller: User, user_id: uuid.UUID, now: datetime
) -> NoteView:
    """Remove a co-owner whose target, caller, and version were already checked.

    Trashed is ``409 note_not_active``; the author is ``409 author_cannot_be_removed``. The owner row goes
    through the ORM (the caller's own row is in the identity map from the lock), the leaver's approvals go
    from every open request on the note, and the note takes a new version. The response is the caller's
    view: for a self-removal it is re-resolved, so ``isOwner`` is false and the permissions are those of any
    remaining share, or read alone.
    """
    note = view.note
    if permissions.is_trashed(note):
        raise Conflict("note_not_active")
    if user_id == note.author_id:
        raise Conflict("author_cannot_be_removed")
    row = session.get(NoteOwner, (note.id, user_id))
    assert row is not None  # authorize_removal saw it under the same lock
    session.delete(row)
    session.flush()
    approvals.delete_for_owner(session, note_id=note.id, user_id=user_id, now=now)
    _touch(note, now)
    session.flush()
    owner_ids = [owner for owner in view.owner_ids if owner != user_id]
    access = view.access
    if caller.id == user_id:
        access = permissions.resolve(session, note.id, caller.id)
        if not access.read:
            access = LAST_READ
    return NoteView(note, owner_ids, view.tags, access)


def set_review_policy(
    session: Session, *, view: NoteView, mode: str, required: int | None, now: datetime
) -> NoteView:
    """Replace the review policy of a note whose author and version were already checked.

    Trashed is ``409``. Under ``peer_approval`` the requirement may not exceed the current owner count
    (``422`` with the contract's detail); the schema already couples ``mode`` and ``requiredApprovals``. An
    identical policy is a no-op; otherwise both columns are written together and the version advances.
    """
    note = view.note
    if permissions.is_trashed(note):
        raise Conflict("note_not_active")
    owners = len(view.owner_ids)
    if mode == PEER_APPROVAL and required is not None and required > owners:
        noun = "owner" if owners == 1 else "owners"
        raise ValidationFailed(
            [FieldError("body", POLICY_POINTER, f"the note has {owners} {noun}")],
            detail=TOO_MANY_APPROVALS_DETAIL,
        )
    if (note.review_mode, note.review_required_approvals) == (mode, required):
        return view
    note.review_mode, note.review_required_approvals = mode, required
    _touch(note, now)
    session.flush()
    return view


def _touch(note: Note, now: datetime) -> None:
    note.version = etags.new_version()
    note.updated_at = now
