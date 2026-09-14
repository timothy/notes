"""Comments: flat Markdown remarks on a note, each with its own ETag.

The router resolves the note first (``notes.read`` for reads, ``notes.lock(lock_access=True)`` for
mutations), so everything here knows the caller may see the note. Anyone who can read the note reads its
comments; adding one needs current ``comment`` permission (owners always have it); the author edits their
own while they keep that permission; any owner, or the author with that permission, deletes. A comment
must belong to the note in the path, and a mis-nested id is ``404`` before any ``403``: every reader may
list the comments, so there is nothing to hide, and the author rule needs the row. Comments never touch
the note's version or ``updated_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from notes_api import cursors, etags
from notes_api.http.problems import Conflict, Forbidden, NotFound, PreconditionFailed
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


def for_edit(session: Session, *, view: NoteView, caller: User, comment_id: uuid.UUID) -> Comment:
    """The comment a PATCH may touch: the caller's own, while they hold current ``comment`` permission.

    Owners cannot rewrite someone else's comment, and an author who was downgraded to ``read`` keeps
    reading it but not editing it.
    """
    comment = get_comment(session, view=view, comment_id=comment_id)
    if comment.author_id != caller.id or not view.access.comment:
        raise Forbidden()
    return comment


def for_delete(session: Session, *, view: NoteView, caller: User, comment_id: uuid.UUID) -> Comment:
    """The comment a DELETE may remove: any owner deletes any comment; the author needs current
    ``comment`` permission."""
    comment = get_comment(session, view=view, comment_id=comment_id)
    if not view.access.is_owner and not (comment.author_id == caller.id and view.access.comment):
        raise Forbidden()
    return comment


def require_version(comment: Comment, if_match: str) -> None:
    if if_match != etag(comment):
        raise PreconditionFailed()


def update_comment(
    session: Session, *, view: NoteView, comment: Comment, body: str, now: datetime
) -> Comment:
    """Replace the body of a comment whose author and version were already checked.

    A trashed note is ``409 note_not_active``. An identical body is a no-op that keeps the version and
    ``updated_at``; an effective change takes a new version.
    """
    if permissions.is_trashed(view.note):
        raise Conflict("note_not_active")
    if body != comment.body:
        comment.body, comment.version, comment.updated_at = body, etags.new_version(), now
        session.flush()
    return comment


def delete_comment(session: Session, *, view: NoteView, comment: Comment) -> None:
    """Remove a comment permanently; a trashed note is ``409 note_not_active``."""
    if permissions.is_trashed(view.note):
        raise Conflict("note_not_active")
    session.delete(comment)
    session.flush()
