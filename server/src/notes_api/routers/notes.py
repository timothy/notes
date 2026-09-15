"""Notes: create, read, and (from slice 4 onwards) the conditional mutations."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import StringConstraints

from notes_api import serializers, uow
from notes_api.contract import FieldError
from notes_api.etags import read_if_match
from notes_api.generated.schemas import NoteScope, NoteState
from notes_api.http.bodies import parse_body
from notes_api.http.deps import CurrentUser
from notes_api.http.problems import ValidationFailed
from notes_api.routers import API_PREFIX, Cursor, Limit, add_route, clock, sessions
from notes_api.services import notes
from notes_api.services.notes import ListFilters, NoteView

# The contract's Tag rule per value; the list's `maxItems: 10` is the Query's max_length. Duplicates and
# NUL characters, which pydantic cannot express here, are rejected by the service.
TagItem = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^\S(?:[^\r\n]*\S)?$")]


def install_note_routes(app: FastAPI) -> None:
    add_route(app, "POST", "/notes", create_note)
    add_route(app, "GET", "/notes", list_notes)
    add_route(app, "GET", "/notes/{noteId}", get_note)
    add_route(app, "PATCH", "/notes/{noteId}", update_note)
    add_route(app, "DELETE", "/notes/{noteId}", trash_note)
    add_route(app, "POST", "/notes/{noteId}/restore", restore_note)


def create_note(user: CurrentUser, request: Request) -> JSONResponse:
    body = parse_body(request, "CreateNote")
    with sessions(request)() as session, uow.transaction(session, "create_note"):
        view = notes.create(
            session,
            author=user,
            title=body["title"],
            body=body.get("body", ""),
            tags=list(body.get("tags", [])),
            clock=clock(request),
        )
        payload, etag = _payload(view), view.etag
    location = f"{API_PREFIX}/notes/{payload['id']}"
    return serializers.json_response(payload, status=201, headers={"Location": location, "ETag": etag})


def list_notes(
    user: CurrentUser,
    request: Request,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    tag: Annotated[list[TagItem] | None, Query(max_length=10)] = None,
    scope: NoteScope = NoteScope.all,
    state: NoteState = NoteState.active,
    teamId: uuid.UUID | None = None,
    limit: Limit = 25,
    cursor: Cursor = None,
) -> JSONResponse:
    if len(request.query_params.getlist("q")) > 1:
        raise ValidationFailed(
            [FieldError("query", "q", "must not be repeated")], detail="A query parameter is invalid."
        )
    filters = ListFilters(scope=scope.value, state=state.value, q=q, tags=tuple(tag or ()), team_id=teamId)
    with sessions(request)() as session, uow.transaction(session, "list_notes"):
        page = notes.list_notes(
            session,
            caller=user,
            filters=filters,
            limit=limit,
            cursor=cursor,
            codec=request.app.state.cursor_codec,
            now=clock(request).now(),
        )
        items = [serializers.note_summary(v.note, v.owner_ids, v.tags, v.access) for v in page.items]
        body = serializers.page(items, page.next_cursor)
    return serializers.json_response(body)


def get_note(user: CurrentUser, request: Request, noteId: uuid.UUID) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "get_note"):
        view = notes.read(session, caller=user, note_id=noteId, now=clock(request).now())
        payload, etag = _payload(view), view.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def update_note(user: CurrentUser, request: Request, noteId: uuid.UUID) -> JSONResponse:
    """The ladder: body shape, lock and visibility (404), owner (403), If-Match (428/400), version (412),
    lifecycle and protection (409), then the write."""
    body = parse_body(request, "UpdateNote")
    with sessions(request)() as session, uow.transaction(session, "update_note"):
        view = notes.lock(session, caller=user, note_id=noteId, now=clock(request).now())
        notes.require_owner(view)
        notes.require_version(view, read_if_match(request.headers))
        view = notes.update(
            session,
            view=view,
            title=body.get("title"),
            body=body.get("body"),
            tags=list(body["tags"]) if "tags" in body else None,
            clock=clock(request),
        )
        payload, etag = _payload(view), view.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def trash_note(user: CurrentUser, request: Request, noteId: uuid.UUID) -> Response:
    """204 with the trash ETag; a repeat with that ETag is 204 again and changes nothing."""
    with sessions(request)() as session, uow.transaction(session, "trash_note"):
        view = notes.lock(session, caller=user, note_id=noteId, now=clock(request).now())
        notes.require_owner(view)
        notes.require_version(view, read_if_match(request.headers))
        etag = notes.trash(session, view=view, clock=clock(request)).etag
    return Response(status_code=204, headers={"ETag": etag})


def restore_note(user: CurrentUser, request: Request, noteId: uuid.UUID) -> JSONResponse:
    """No request body: whatever arrives is ignored. Expired notes are 404 through the visibility rule."""
    with sessions(request)() as session, uow.transaction(session, "restore_note"):
        view = notes.lock(session, caller=user, note_id=noteId, now=clock(request).now())
        notes.require_owner(view)
        notes.require_version(view, read_if_match(request.headers))
        view = notes.restore(session, view=view, clock=clock(request))
        payload, etag = _payload(view), view.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def _payload(view: NoteView) -> dict[str, Any]:
    return serializers.note(view.note, view.owner_ids, view.tags, view.access)
