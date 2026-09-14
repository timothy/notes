"""Notes: create and read, with the building blocks the mutations share (lock, visibility, versions, tags).

Every mutation locks the note row first (``SELECT ... FOR UPDATE``; SQLite serializes writers with
``BEGIN IMMEDIATE``), resolves the caller's access, and only then looks at preconditions and lifecycle, so
a caller who may not see the note learns nothing (``404``) and a reader who may not act gets ``403``
before any version or conflict detail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from notes_api import etags
from notes_api.clock import Clock
from notes_api.http.problems import Forbidden, NotFound, PreconditionFailed
from notes_api.models import Note, NoteOwner, NoteTag, User
from notes_api.services import permissions
from notes_api.services.permissions import Access

SELF_MERGE = "self_merge"


@dataclass(frozen=True, slots=True)
class NoteView:
    """A note as one caller sees it: the row, its owners and tags by position, and the caller's access."""

    note: Note
    owner_ids: list[uuid.UUID]
    tags: list[str]
    access: Access

    @property
    def etag(self) -> str:
        return etags.quote(self.note.version)


def create(
    session: Session, *, author: User, title: str, body: str, tags: list[str], clock: Clock
) -> NoteView:
    now = clock.now()
    note = Note(
        id=uuid.uuid4(),
        author_id=author.id,
        title=title,
        body=body,
        title_fold=title.casefold(),
        body_fold=body.casefold(),
        review_mode=SELF_MERGE,
        review_required_approvals=None,
        created_at=now,
        updated_at=now,
        deleted_at=None,
        expires_at=None,
        version=etags.new_version(),
    )
    session.add(note)
    session.flush()  # the owner and tag rows reference it; nothing orders the inserts otherwise
    session.add(NoteOwner(note_id=note.id, user_id=author.id, position=0, added_at=now))
    session.add_all(_tag_rows(note.id, tags))
    session.flush()
    return NoteView(note, [author.id], list(tags), Access.owner())


def read(session: Session, *, caller: User, note_id: uuid.UUID, now: datetime) -> NoteView:
    """The note as the caller sees it; ``404`` when missing, hidden, trashed for a reader, or expired."""
    return _visible_view(session, session.get(Note, note_id), caller, now)


def lock(session: Session, *, caller: User, note_id: uuid.UUID, now: datetime) -> NoteView:
    """The note locked for the rest of the transaction, with the same ``404`` rule as ``read``."""
    statement = select(Note).where(Note.id == note_id).with_for_update()
    return _visible_view(session, session.execute(statement).scalar_one_or_none(), caller, now)


def require_owner(view: NoteView) -> None:
    if not view.access.is_owner:
        raise Forbidden()


def require_version(view: NoteView, if_match: str) -> None:
    if if_match != view.etag:
        raise PreconditionFailed()


def owner_ids(session: Session, note_id: uuid.UUID) -> list[uuid.UUID]:
    statement = select(NoteOwner.user_id).where(NoteOwner.note_id == note_id).order_by(NoteOwner.position)
    return list(session.execute(statement).scalars())


def tags_of(session: Session, note_id: uuid.UUID) -> list[str]:
    statement = select(NoteTag.tag).where(NoteTag.note_id == note_id).order_by(NoteTag.position)
    return list(session.execute(statement).scalars())


def replace_tags(session: Session, note_id: uuid.UUID, tags: list[str]) -> None:
    """Replace the tag rows wholesale.

    The Core delete runs first and immediately; adding the new rows before it would autoflush them into
    the old positions, and deleting the old rows through the ORM in the same flush would collide on
    ``UNIQUE(note_id, tag)`` through row-switch updates.
    """
    session.execute(delete(NoteTag).where(NoteTag.note_id == note_id))
    session.add_all(_tag_rows(note_id, tags))
    session.flush()


def _tag_rows(note_id: uuid.UUID, tags: list[str]) -> list[NoteTag]:
    return [NoteTag(note_id=note_id, position=position, tag=tag) for position, tag in enumerate(tags)]


def _visible_view(session: Session, note: Note | None, caller: User, now: datetime) -> NoteView:
    if note is None:
        raise NotFound()
    access = permissions.resolve(session, note.id, caller.id)
    if not permissions.visible(note, access, now):
        raise NotFound()
    return NoteView(note, owner_ids(session, note.id), tags_of(session, note.id), access)
