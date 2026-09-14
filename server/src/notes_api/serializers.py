"""Response bodies as plain dicts in the contract's shapes.

Responses are built here as dicts and validated against the contract's JSON Schemas by the test harness on
every request, so the serializer is short and the proof of conformance lives in the tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse

from notes_api import etags
from notes_api.merge.three_way import Content, proposal_diff
from notes_api.models import Approval, Comment, EditRequest, Membership, Note, Share, Team, User
from notes_api.services.edit_requests import RequestView
from notes_api.services.permissions import Access

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def timestamp(moment: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.ffffffZ``: always UTC, always six fractional digits."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def json_response(body: Any, *, status: int = 200, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=dict(headers or {}))


def page(items: list[dict[str, Any]], next_cursor: str | None) -> dict[str, Any]:
    return {"items": items, "nextCursor": next_cursor}


def user(row: User) -> dict[str, Any]:
    return {"id": str(row.id), "displayName": row.display_name, "createdAt": timestamp(row.created_at)}


def team(row: Team) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "createdAt": timestamp(row.created_at),
        "updatedAt": timestamp(row.updated_at),
    }


def membership(row: Membership) -> dict[str, Any]:
    return {
        "teamId": str(row.team_id),
        "userId": str(row.user_id),
        "role": row.role,
        "joinedAt": timestamp(row.joined_at),
        "updatedAt": timestamp(row.updated_at),
    }


def review_policy(row: Note) -> dict[str, Any]:
    return {"mode": row.review_mode, "requiredApprovals": row.review_required_approvals}


def _note_fields(row: Note, owner_ids: list[uuid.UUID], tags: list[str], access: Access) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "authorId": str(row.author_id),
        "ownerIds": [str(owner_id) for owner_id in owner_ids],
        "reviewPolicy": review_policy(row),
        "title": row.title,
        "tags": list(tags),
        "createdAt": timestamp(row.created_at),
        "updatedAt": timestamp(row.updated_at),
        "deletedAt": timestamp(row.deleted_at) if row.deleted_at is not None else None,
        "expiresAt": timestamp(row.expires_at) if row.expires_at is not None else None,
        "isOwner": access.is_owner,
        "effectivePermissions": access.permissions(),
    }


def note(row: Note, owner_ids: list[uuid.UUID], tags: list[str], access: Access) -> dict[str, Any]:
    """The full ``Note``; ``ownerIds`` and ``tags`` come ordered by position from the service."""
    return {**_note_fields(row, owner_ids, tags, access), "body": row.body}


def note_summary(row: Note, owner_ids: list[uuid.UUID], tags: list[str], access: Access) -> dict[str, Any]:
    """A ``NoteSummary``: the note without its body, as list operations return it."""
    return _note_fields(row, owner_ids, tags, access)


def share_permissions(row: Share) -> list[str]:
    """The canonical ``PermissionSet`` of a share: ``read`` always, then the granted flags in order."""
    granted = ["read"]
    if row.can_comment:
        granted.append("comment")
    if row.can_propose:
        granted.append("propose_edit")
    return granted


def share(row: Share) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "noteId": str(row.note_id),
        "recipient": {"type": row.recipient_type, "id": str(row.recipient_id)},
        "permissions": share_permissions(row),
        "createdAt": timestamp(row.created_at),
        "updatedAt": timestamp(row.updated_at),
    }


def comment(row: Comment) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "noteId": str(row.note_id),
        "authorId": str(row.author_id),
        "body": row.body,
        "createdAt": timestamp(row.created_at),
        "updatedAt": timestamp(row.updated_at),
    }


def approval(row: Approval) -> dict[str, Any]:
    return {"userId": str(row.user_id), "approvedAt": timestamp(row.approved_at)}


def _edit_request_fields(rv: RequestView) -> dict[str, Any]:
    """``EditRequestFields``: ``noteTitle`` and an open request's ``requiredApprovals`` are live views of the
    note, outside the request's ETag."""
    request = rv.request
    return {
        "id": str(request.id),
        "noteId": str(request.note_id),
        "noteTitle": rv.note.note.title,
        "proposerId": str(request.proposer_id),
        "status": request.status,
        "requiredApprovals": rv.required_approvals(),
        "approvals": [approval(row) for row in rv.approvals],
        "createdAt": timestamp(request.created_at),
        "updatedAt": timestamp(request.updated_at),
        "closedAt": timestamp(request.closed_at) if request.closed_at is not None else None,
    }


def edit_request_summary(rv: RequestView) -> dict[str, Any]:
    """An inbox entry: the shared fields without content or diffs."""
    return _edit_request_fields(rv)


def edit_request(rv: RequestView) -> dict[str, Any]:
    """The full ``EditRequest``; every key is present so the per-status ``if/then`` rules can hold."""
    request = rv.request
    base = Content(request.base_title, request.base_body)
    proposed = Content(request.proposed_title, request.proposed_body)
    return {
        **_edit_request_fields(rv),
        "baseContent": base.to_dict(),
        "proposedContent": proposed.to_dict(),
        "explanation": request.explanation,
        "proposalDiff": proposal_diff(base, proposed).to_dict(),
        "rejectedBy": str(request.rejected_by) if request.rejected_by is not None else None,
        "rejectionReason": request.rejection_reason,
        "mergeRecord": _merge_record(request),
    }


def _merge_record(request: EditRequest) -> dict[str, Any] | None:
    if request.merged_at is None:
        return None
    assert request.merged_title is not None and request.merged_body is not None
    assert request.merged_note_version is not None and request.merged_by is not None
    return {
        "content": {"title": request.merged_title, "body": request.merged_body},
        "noteETag": etags.quote(request.merged_note_version),
        "mergedBy": str(request.merged_by),
        "mergedAt": timestamp(request.merged_at),
    }
