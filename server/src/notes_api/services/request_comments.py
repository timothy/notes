"""Comments on edit requests: the review conversation between a note's owners and the proposer.

Authorization is the inspect rule (``edit_requests.inspect``): the note's owners, and the proposer while
they can still read the note. No note ``comment`` permission is needed, so a propose-only proposer can answer
review feedback. Comments are accepted on closed requests while the note is active; a trashed note freezes
them while its owners still read them. A comment must belong to the request in the path (``404`` before any
``403``). No request-comment operation touches the request's version or the note's.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from notes_api import cursors, etags
from notes_api.http.problems import Conflict, Forbidden, NotFound, PreconditionFailed
from notes_api.models import RequestComment, User
from notes_api.services import permissions
from notes_api.services.edit_requests import RequestView

COLLECTION = "request_comments"


def etag(comment: RequestComment) -> str:
    return etags.quote(comment.version)


def list_comments(
    session: Session,
    *,
    caller: User,
    rv: RequestView,
    limit: int,
    cursor: str | None,
    codec: cursors.CursorCodec,
) -> cursors.Page[RequestComment]:
    """The request's comments, oldest first (``createdAt ASC, id ASC``)."""
    return cursors.paginate(
        session,
        select(RequestComment).where(RequestComment.request_id == rv.request.id),
        moment=RequestComment.created_at,
        id_column=RequestComment.id,
        key_of=lambda row: cursors.Key(row.created_at, row.id),
        limit=limit,
        cursor=cursor,
        codec=codec,
        fingerprint=cursors.fingerprint(caller.id, COLLECTION, {"requestId": str(rv.request.id)}, limit),
        descending=False,
    )


def get_comment(session: Session, *, rv: RequestView, comment_id: uuid.UUID) -> RequestComment:
    """The comment, which must belong to the request in the path; anything else is ``404``."""
    statement = select(RequestComment).where(
        RequestComment.id == comment_id, RequestComment.request_id == rv.request.id
    )
    comment = session.execute(statement).scalar_one_or_none()
    if comment is None:
        raise NotFound()
    return comment


def create_comment(
    session: Session, *, rv: RequestView, author: User, body: str, now: datetime
) -> RequestComment:
    """Add a comment for an inspector; only the note's lifecycle can refuse it, never the request's."""
    _require_active(rv)
    comment = RequestComment(
        id=uuid.uuid4(),
        request_id=rv.request.id,
        author_id=author.id,
        body=body,
        created_at=now,
        updated_at=now,
        version=etags.new_version(),
    )
    session.add(comment)
    session.flush()
    return comment


def for_edit(session: Session, *, rv: RequestView, caller: User, comment_id: uuid.UUID) -> RequestComment:
    """The comment a PATCH may touch: the author's own, owners included only as authors."""
    comment = get_comment(session, rv=rv, comment_id=comment_id)
    if comment.author_id != caller.id:
        raise Forbidden()
    return comment


def for_delete(session: Session, *, rv: RequestView, caller: User, comment_id: uuid.UUID) -> RequestComment:
    """The comment a DELETE may remove: any owner deletes any comment, the author their own."""
    comment = get_comment(session, rv=rv, comment_id=comment_id)
    if not rv.note.access.is_owner and comment.author_id != caller.id:
        raise Forbidden()
    return comment


def require_version(comment: RequestComment, if_match: str) -> None:
    if if_match != etag(comment):
        raise PreconditionFailed()


def update_comment(
    session: Session, *, rv: RequestView, comment: RequestComment, body: str, now: datetime
) -> RequestComment:
    """Replace the body; an identical body is a no-op keeping the version and ``updated_at``."""
    _require_active(rv)
    if body != comment.body:
        comment.body, comment.version, comment.updated_at = body, etags.new_version(), now
        session.flush()
    return comment


def delete_comment(session: Session, *, rv: RequestView, comment: RequestComment) -> None:
    """Remove a comment permanently."""
    _require_active(rv)
    session.delete(comment)
    session.flush()


def _require_active(rv: RequestView) -> None:
    if permissions.is_trashed(rv.note.note):
        raise Conflict("note_not_active")
