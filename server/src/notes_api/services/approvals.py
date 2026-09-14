"""Approvals: an owner's explicit approval of an edit request's current proposal (design guide, section 3).

Rows change only while a request is open, so closing freezes them without a copy. This module holds every
write to them: the deletion an owner's removal performs on the note's open requests (slice 10) and the two
approval actions (slice 11). ``edit_requests.approval_count`` reads them at merge time.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from notes_api import etags
from notes_api.models import Approval, EditRequest
from notes_api.services.edit_requests import OPEN


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
