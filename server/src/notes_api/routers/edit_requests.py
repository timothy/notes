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
from notes_api.etags import parse_if_match
from notes_api.generated.schemas import EditRequestStatus, InboxView, NoteState
from notes_api.http.bodies import parse_body
from notes_api.http.deps import CurrentUser
from notes_api.merge.three_way import Content
from notes_api.routers import API_PREFIX, Cursor, Limit, add_route, clock, sessions
from notes_api.services import edit_requests, notes


def install_edit_request_routes(app: FastAPI) -> None:
    add_route(app, "POST", "/notes/{noteId}/edit-requests", create_edit_request)
    add_route(app, "GET", "/notes/{noteId}/edit-requests", list_note_edit_requests)
    add_route(app, "GET", "/edit-requests", list_edit_requests)
    add_route(app, "GET", "/edit-requests/{requestId}", get_edit_request)
    add_route(app, "PATCH", "/edit-requests/{requestId}", revise_edit_request)
    add_route(app, "POST", "/edit-requests/{requestId}/withdraw", withdraw_edit_request)
    add_route(app, "POST", "/edit-requests/{requestId}/reject", reject_edit_request)


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
            session, caller=user, view=view, status=status.value, limit=limit, cursor=cursor
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
        edit_requests.require_version(rv, parse_if_match(request.headers.get("if-match")))
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
        edit_requests.require_version(rv, parse_if_match(request.headers.get("if-match")))
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
        edit_requests.require_version(rv, parse_if_match(request.headers.get("if-match")))
        rv = edit_requests.reject(session, rv=rv, rejecter=user, reason=body.get("reason"), now=now)
        payload, etag = serializers.edit_request(rv), rv.etag
    return serializers.json_response(payload, headers={"ETag": etag})
