"""Comments: flat Markdown remarks on a note, each with its own ETag.

The router resolves the note first (``notes.read`` for reads, ``notes.lock(lock_access=True)`` for
mutations), so everything here knows the caller may see the note. Anyone who can read the note reads its
comments; adding one needs current ``comment`` permission (owners always have it). A comment must belong
to the note in the path, and a mis-nested id is ``404`` before any ``403``: every reader may list the
comments, so there is nothing to hide, and the author rule needs the row. Comments never touch the note's
version or ``updated_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from notes_api import cursors, etags
from notes_api.http.problems import Conflict, Forbidden, NotFound
from notes_api.models import Comment, User
from notes_api.services import permissions
from notes_api.services.notes import NoteView

COLLECTION = "comments"


def etag(comment: Comment) -> str:
    return etags.quote(comment.version)


def list_comments(
    session: Session, *, caller: User, view: NoteView, limit: int, cursor: str | None
) -> cursors.Page[Comment]:
    """The note's comments, oldest first (``createdAt ASC, id ASC``)."""
    return cursors.paginate(
        session,
        select(Comment).where(Comment.note_id == view.note.id),
        moment=Comment.created_at,
        id_column=Comment.id,
        key_of=lambda row: cursors.Key(row.created_at, row.id),
        limit=limit,
        cursor=cursor,
        fingerprint=cursors.fingerprint(caller.id, COLLECTION, {"noteId": str(view.note.id)}, limit),
        descending=False,
    )


def get_comment(session: Session, *, view: NoteView, comment_id: uuid.UUID) -> Comment:
    """The comment, which must belong to the note in the path; anything else is ``404``."""
    statement = select(Comment).where(Comment.id == comment_id, Comment.note_id == view.note.id)
    comment = session.execute(statement).scalar_one_or_none()
    if comment is None:
        raise NotFound()
    return comment


def create_comment(session: Session, *, view: NoteView, author: User, body: str, now: datetime) -> Comment:
    """Add a comment: the caller needs current ``comment`` permission and the note must be active."""
    if not view.access.comment:
        raise Forbidden()
    if permissions.is_trashed(view.note):
        raise Conflict("note_not_active")
    comment = Comment(
        id=uuid.uuid4(),
        note_id=view.note.id,
        author_id=author.id,
        body=body,
        created_at=now,
        updated_at=now,
        version=etags.new_version(),
    )
    session.add(comment)
    session.flush()
    return comment
