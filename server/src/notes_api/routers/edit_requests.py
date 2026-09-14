"""Edit requests: submission under the note lock; inspection, lists, and transitions follow in this slice."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from notes_api import serializers, uow
from notes_api.http.bodies import parse_body
from notes_api.http.deps import CurrentUser
from notes_api.merge.three_way import Content
from notes_api.routers import API_PREFIX, add_route, clock, sessions
from notes_api.services import edit_requests, notes


def install_edit_request_routes(app: FastAPI) -> None:
    add_route(app, "POST", "/notes/{noteId}/edit-requests", create_edit_request)


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
