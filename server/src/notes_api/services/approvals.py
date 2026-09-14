"""Approvals: an owner's explicit approval of an edit request's current proposal (design guide, section 3).

Rows change only while a request is open, so closing freezes them without a copy. This module holds every
write to them: the deletion an owner's removal performs on the note's open requests and the two approval
actions. ``edit_requests.approval_count`` reads them at merge time.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from notes_api import etags
from notes_api.http.problems import Forbidden
from notes_api.models import Approval, EditRequest, User
from notes_api.services.edit_requests import OPEN, RequestView, approvals_of, require_open_and_active


def delete_for_owner(
    session: Session, *, note_id: uuid.UUID, user_id: uuid.UUID, now: datetime
) -> list[uuid.UUID]:
    """Delete a departing owner's approvals from the note's open requests, under the note lock.

    Only the requests that lost an approval take a new version and ``updated_at``; the others keep their
    ETags, because ``requiredApprovals`` is a live view outside them. Closed requests are frozen and never
    touched. Returns the ids of the requests that changed.
    """
    open_requests = list(
        session.execute(
            select(EditRequest)
            .where(EditRequest.note_id == note_id, EditRequest.status == OPEN)
            .with_for_update()
        ).scalars()
    )
    if not open_requests:
        return []
    by_id = {request.id: request for request in open_requests}
    statement = (
        delete(Approval)
        .where(Approval.request_id.in_(list(by_id)), Approval.user_id == user_id)
        .returning(Approval.request_id)
        .execution_options(synchronize_session=False)
    )
    affected = set(session.execute(statement).scalars())
    for request_id in affected:
        by_id[request_id].version = etags.new_version()
        by_id[request_id].updated_at = now
    session.flush()
    return [request.id for request in open_requests if request.id in affected]


# -- the approval actions (slice 11) --------------------------------------------------------------------

SELF_APPROVAL_DETAIL = (
    "You proposed this request, so your approval does not count. Another owner must approve or merge it."
)


def require_not_proposer(rv: RequestView, caller: User, *, detail: str | None) -> None:
    """The proposer can neither approve nor revoke: approving carries the contract's self-approval detail,
    revoking the default one. Checked before ownership, since the message is true for any proposer."""
    if rv.request.proposer_id == caller.id:
        raise Forbidden(detail=detail)


def approve(session: Session, *, rv: RequestView, approver: User, now: datetime) -> RequestView:
    """Record the approver's approval of the current proposal on an open request of an active note.

    A repeat is a no-op that leaves the version and ``updated_at`` alone. Approvals are recorded even where
    the policy does not require them (``self_merge``, a single owner).
    """
    require_open_and_active(rv)
    if any(row.user_id == approver.id for row in rv.approvals):
        return rv
    session.add(Approval(request_id=rv.request.id, user_id=approver.id, approved_at=now))
    _touch(rv.request, now)
    session.flush()
    return RequestView(rv.request, rv.note, approvals_of(session, rv.request.id))


def revoke(session: Session, *, rv: RequestView, caller: User, now: datetime) -> RequestView:
    """Delete the caller's own approval; holding none is a no-op."""
    require_open_and_active(rv)
    mine = next((row for row in rv.approvals if row.user_id == caller.id), None)
    if mine is None:
        return rv
    session.delete(mine)
    _touch(rv.request, now)
    session.flush()
    return RequestView(rv.request, rv.note, approvals_of(session, rv.request.id))


def _touch(request: EditRequest, now: datetime) -> None:
    request.version = etags.new_version()
    request.updated_at = now
