"""Notes: create, read, and (from slice 4 onwards) the conditional mutations."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from notes_api import serializers, uow
from notes_api.etags import parse_if_match
from notes_api.http.bodies import parse_body
from notes_api.http.deps import CurrentUser
from notes_api.routers import API_PREFIX, add_route, clock, sessions
from notes_api.services import notes
from notes_api.services.notes import NoteView


def install_note_routes(app: FastAPI) -> None:
    add_route(app, "POST", "/notes", create_note)
    add_route(app, "GET", "/notes/{noteId}", get_note)
    add_route(app, "PATCH", "/notes/{noteId}", update_note)


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
        notes.require_version(view, parse_if_match(request.headers.get("if-match")))
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


def _payload(view: NoteView) -> dict[str, Any]:
    return serializers.note(view.note, view.owner_ids, view.tags, view.access)
