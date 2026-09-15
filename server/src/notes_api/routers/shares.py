"""Shares: owners only, unconditional (no ETag), all under the note lock for mutations."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from notes_api import serializers, uow
from notes_api.http.bodies import parse_body
from notes_api.http.deps import CurrentUser
from notes_api.routers import API_PREFIX, Cursor, Limit, add_route, clock, sessions
from notes_api.services import notes, shares


def install_share_routes(app: FastAPI) -> None:
    add_route(app, "GET", "/notes/{noteId}/shares", list_shares)
    add_route(app, "POST", "/notes/{noteId}/shares", create_share)
    add_route(app, "GET", "/notes/{noteId}/shares/{shareId}", get_share)
    add_route(app, "PATCH", "/notes/{noteId}/shares/{shareId}", update_share)
    add_route(app, "DELETE", "/notes/{noteId}/shares/{shareId}", delete_share)


def list_shares(
    user: CurrentUser, request: Request, noteId: uuid.UUID, limit: Limit = 25, cursor: Cursor = None
) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "list_shares"):
        view = notes.read(session, caller=user, note_id=noteId, now=clock(request).now())
        notes.require_owner(view)
        page = shares.list_shares(
            session, caller=user, view=view, limit=limit, cursor=cursor, codec=request.app.state.cursor_codec
        )
        body = serializers.page([serializers.share(row) for row in page.items], page.next_cursor)
    return serializers.json_response(body)


def create_share(user: CurrentUser, request: Request, noteId: uuid.UUID) -> JSONResponse:
    body = parse_body(request, "CreateShare")
    with sessions(request)() as session, uow.transaction(session, "create_share"):
        view = notes.lock(session, caller=user, note_id=noteId, now=clock(request).now())
        notes.require_owner(view)
        share = shares.create_share(
            session,
            view=view,
            recipient_type=body["recipient"]["type"],
            recipient_id=uuid.UUID(body["recipient"]["id"]),
            granted=list(body["permissions"]),
            now=clock(request).now(),
        )
        payload = serializers.share(share)
    location = f"{API_PREFIX}/notes/{payload['noteId']}/shares/{payload['id']}"
    return serializers.json_response(payload, status=201, headers={"Location": location})


def get_share(user: CurrentUser, request: Request, noteId: uuid.UUID, shareId: uuid.UUID) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "get_share"):
        view = notes.read(session, caller=user, note_id=noteId, now=clock(request).now())
        notes.require_owner(view)
        payload = serializers.share(shares.get_share(session, view=view, share_id=shareId))
    return serializers.json_response(payload)


def update_share(user: CurrentUser, request: Request, noteId: uuid.UUID, shareId: uuid.UUID) -> JSONResponse:
    body = parse_body(request, "UpdateShare")
    with sessions(request)() as session, uow.transaction(session, "update_share"):
        view = notes.lock(session, caller=user, note_id=noteId, now=clock(request).now())
        notes.require_owner(view)
        share = shares.update_share(
            session, view=view, share_id=shareId, granted=list(body["permissions"]), now=clock(request).now()
        )
        payload = serializers.share(share)
    return serializers.json_response(payload)


def delete_share(user: CurrentUser, request: Request, noteId: uuid.UUID, shareId: uuid.UUID) -> Response:
    with sessions(request)() as session, uow.transaction(session, "delete_share"):
        view = notes.lock(session, caller=user, note_id=noteId, now=clock(request).now())
        notes.require_owner(view)
        shares.delete_share(session, view=view, share_id=shareId)
    return Response(status_code=204)
