"""Edit requests: submission under the note lock; inspection, lists, and transitions follow in this slice."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from notes_api import serializers, uow
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
