"""Edit requests: submission, inspection, the two lists, and the proposer's and owners' transitions.

Request-scoped mutations lock the note first, then the request (``edit_requests.inspect(lock=True)``), so
the ladder is 404 (note invisible or not owner/proposer), 403, If-Match (428/400), 412, 409
``request_not_open``, 409 ``note_not_active``, semantic 422, then the write.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from notes_api import serializers, uow
from notes_api.etags import read_if_match
from notes_api.generated.schemas import EditRequestStatus, InboxView, NoteState
from notes_api.http.bodies import parse_body
from notes_api.http.deps import CurrentUser
from notes_api.merge.three_way import Content
from notes_api.routers import API_PREFIX, Cursor, Limit, add_route, clock, sessions
from notes_api.services import approvals, edit_requests, notes


def install_edit_request_routes(app: FastAPI) -> None:
    add_route(app, "POST", "/notes/{noteId}/edit-requests", create_edit_request)
    add_route(app, "GET", "/notes/{noteId}/edit-requests", list_note_edit_requests)
    add_route(app, "GET", "/edit-requests", list_edit_requests)
    add_route(app, "GET", "/edit-requests/{requestId}", get_edit_request)
    add_route(app, "PATCH", "/edit-requests/{requestId}", revise_edit_request)
    add_route(app, "POST", "/edit-requests/{requestId}/withdraw", withdraw_edit_request)
    add_route(app, "POST", "/edit-requests/{requestId}/reject", reject_edit_request)
    add_route(app, "POST", "/edit-requests/{requestId}/preview", preview_edit_request)
    add_route(app, "POST", "/edit-requests/{requestId}/merge", merge_edit_request)
    add_route(app, "POST", "/edit-requests/{requestId}/approve", approve_edit_request)
    add_route(app, "POST", "/edit-requests/{requestId}/revoke-approval", revoke_edit_request_approval)


def list_note_edit_requests(
    user: CurrentUser,
    request: Request,
    noteId: uuid.UUID,
    status: EditRequestStatus = EditRequestStatus.open,
    limit: Limit = 25,
    cursor: Cursor = None,
) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "list_note_edit_requests"):
        view = notes.read(session, caller=user, note_id=noteId, now=clock(request).now())
        page = edit_requests.list_for_note(
            session,
            caller=user,
            view=view,
            status=status.value,
            limit=limit,
            cursor=cursor,
            codec=request.app.state.cursor_codec,
        )
        items = [serializers.edit_request_summary(rv) for rv in page.items]
        body = serializers.page(items, page.next_cursor)
    return serializers.json_response(body)


def list_edit_requests(
    user: CurrentUser,
    request: Request,
    view: InboxView = InboxView.incoming,
    status: EditRequestStatus = EditRequestStatus.open,
    state: NoteState = NoteState.active,
    limit: Limit = 25,
    cursor: Cursor = None,
) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "list_edit_requests"):
        page = edit_requests.inbox(
            session,
            caller=user,
            view=view.value,
            status=status.value,
            state=state.value,
            limit=limit,
            cursor=cursor,
            codec=request.app.state.cursor_codec,
            now=clock(request).now(),
        )
        items = [serializers.edit_request_summary(rv) for rv in page.items]
        body = serializers.page(items, page.next_cursor)
    return serializers.json_response(body)


def get_edit_request(user: CurrentUser, request: Request, requestId: uuid.UUID) -> JSONResponse:
    """Owners and the proposer with current read access; everyone else is 404."""
    with sessions(request)() as session, uow.transaction(session, "get_edit_request"):
        rv = edit_requests.inspect(
            session, caller=user, request_id=requestId, now=clock(request).now(), lock=False
        )
        payload, etag = serializers.edit_request(rv), rv.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def create_edit_request(user: CurrentUser, request: Request, noteId: uuid.UUID) -> JSONResponse:
    """Body shape, note lock and visibility (404), propose_edit (403), baseNoteETag (412), lifecycle (409),
    identical proposal (422), then the insert; the note itself is untouched."""
    body = parse_body(request, "CreateEditRequest")
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "create_edit_request"):
        view = notes.lock(session, caller=user, note_id=noteId, now=now, lock_access=True)
        proposed = body["proposedContent"]
        rv = edit_requests.create(
            session,
            view=view,
            proposer=user,
            base_etag=body["baseNoteETag"],
            proposed=Content(proposed["title"], proposed["body"]),
            explanation=body.get("explanation"),
            now=now,
        )
        payload, etag = serializers.edit_request(rv), rv.etag
    location = f"{API_PREFIX}/edit-requests/{payload['id']}"
    return serializers.json_response(payload, status=201, headers={"Location": location, "ETag": etag})


def revise_edit_request(user: CurrentUser, request: Request, requestId: uuid.UUID) -> JSONResponse:
    """Body shape, then the request under the note lock (404), the proposer with propose_edit (403), the
    request's If-Match (428/400), 412, 409 request_not_open, 409 note_not_active, 422, then the write."""
    body = parse_body(request, "ReviseEditRequest")
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "revise_edit_request"):
        rv = edit_requests.inspect(session, caller=user, request_id=requestId, now=now, lock=True)
        edit_requests.require_proposer_with_propose(rv, user)
        edit_requests.require_version(rv, read_if_match(request.headers))
        proposed = body.get("proposedContent")
        rv = edit_requests.revise(
            session,
            rv=rv,
            proposed=Content(proposed["title"], proposed["body"]) if proposed is not None else None,
            explanation=body.get("explanation"),
            explanation_given="explanation" in body,
            now=now,
        )
        payload, etag = serializers.edit_request(rv), rv.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def withdraw_edit_request(user: CurrentUser, request: Request, requestId: uuid.UUID) -> JSONResponse:
    """No request body: whatever arrives is ignored, as for restoreNote."""
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "withdraw_edit_request"):
        rv = edit_requests.inspect(session, caller=user, request_id=requestId, now=now, lock=True)
        edit_requests.require_proposer(rv, user)
        edit_requests.require_version(rv, read_if_match(request.headers))
        rv = edit_requests.withdraw(session, rv=rv, now=now)
        payload, etag = serializers.edit_request(rv), rv.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def reject_edit_request(user: CurrentUser, request: Request, requestId: uuid.UUID) -> JSONResponse:
    """The body is optional; an empty one under any content type counts as omitted."""
    body = parse_body(request, "RejectEditRequest", required=False)
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "reject_edit_request"):
        rv = edit_requests.inspect(session, caller=user, request_id=requestId, now=now, lock=True)
        notes.require_owner(rv.note)
        edit_requests.require_version(rv, read_if_match(request.headers))
        rv = edit_requests.reject(session, rv=rv, rejecter=user, reason=body.get("reason"), now=now)
        payload, etag = serializers.edit_request(rv), rv.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def preview_edit_request(user: CurrentUser, request: Request, requestId: uuid.UUID) -> JSONResponse:
    """Owners only, no locks, no writes: the body is optional and an empty one counts as omitted."""
    body = parse_body(request, "PreviewEditRequest", required=False)
    with sessions(request)() as session, uow.transaction(session, "preview_edit_request"):
        rv = edit_requests.inspect(
            session, caller=user, request_id=requestId, now=clock(request).now(), lock=False
        )
        notes.require_owner(rv.note)
        final = body.get("finalContent")
        computation = edit_requests.preview(
            rv, Content(final["title"], final["body"]) if final is not None else None
        )
        payload = serializers.preview_result(rv, computation)
    return serializers.json_response(payload)


def merge_edit_request(user: CurrentUser, request: Request, requestId: uuid.UUID) -> JSONResponse:
    """Body shape, then the request under the note lock (404), owner (403), the request's If-Match
    (428/400/412), expectedNoteETag (412), 409 request_not_open, 409 note_not_active, 422 /finalContent,
    409 approval_required, 409 merge_conflict, then the atomic write. The response ETag is the request's."""
    body = parse_body(request, "MergeEditRequest")
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "merge_edit_request"):
        rv = edit_requests.inspect(session, caller=user, request_id=requestId, now=now, lock=True)
        notes.require_owner(rv.note)
        edit_requests.require_version(rv, read_if_match(request.headers))
        edit_requests.require_note_version(rv, body["expectedNoteETag"])
        final = body.get("finalContent")
        rv = edit_requests.merge(
            session,
            rv=rv,
            merger=user,
            final=Content(final["title"], final["body"]) if final is not None else None,
            now=now,
        )
        payload, etag = serializers.merge_result(rv), rv.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def approve_edit_request(user: CurrentUser, request: Request, requestId: uuid.UUID) -> JSONResponse:
    """No request body. The proposer is 403 with the self-approval detail before any other rule, then a
    non-owner is 403, then If-Match (428/400), 412, 409 request_not_open, 409 note_not_active, the write."""
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "approve_edit_request"):
        rv = edit_requests.inspect(session, caller=user, request_id=requestId, now=now, lock=True)
        approvals.require_not_proposer(rv, user, detail=approvals.SELF_APPROVAL_DETAIL)
        notes.require_owner(rv.note)
        edit_requests.require_version(rv, read_if_match(request.headers))
        rv = approvals.approve(session, rv=rv, approver=user, now=now)
        payload, etag = serializers.edit_request(rv), rv.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def revoke_edit_request_approval(user: CurrentUser, request: Request, requestId: uuid.UUID) -> JSONResponse:
    """No request body; the same ladder as approve with the default 403 detail for the proposer."""
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "revoke_edit_request_approval"):
        rv = edit_requests.inspect(session, caller=user, request_id=requestId, now=now, lock=True)
        approvals.require_not_proposer(rv, user, detail=None)
        notes.require_owner(rv.note)
        edit_requests.require_version(rv, read_if_match(request.headers))
        rv = approvals.revoke(session, rv=rv, caller=user, now=now)
        payload, etag = serializers.edit_request(rv), rv.etag
    return serializers.json_response(payload, headers={"ETag": etag})
