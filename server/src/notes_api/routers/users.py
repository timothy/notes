"""Users: the caller's own profile and the directory."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from notes_api import serializers, uow
from notes_api.http.deps import CurrentUser
from notes_api.routers import Cursor, Limit, add_route
from notes_api.services import users


def install_user_routes(app: FastAPI) -> None:
    add_route(app, "GET", "/me", get_current_user)
    add_route(app, "GET", "/users", list_users)
    add_route(app, "GET", "/users/{userId}", get_user)


def get_current_user(user: CurrentUser) -> JSONResponse:
    return serializers.json_response(serializers.user(user))


def list_users(user: CurrentUser, request: Request, limit: Limit = 25, cursor: Cursor = None) -> JSONResponse:
    with request.app.state.session_factory() as session, uow.transaction(session, "list_users"):
        page = users.list_users(
            session, caller=user, limit=limit, cursor=cursor, codec=request.app.state.cursor_codec
        )
        body = serializers.page([serializers.user(row) for row in page.items], page.next_cursor)
    return serializers.json_response(body)


def get_user(user: CurrentUser, request: Request, userId: uuid.UUID) -> JSONResponse:
    with request.app.state.session_factory() as session, uow.transaction(session, "get_user"):
        body = serializers.user(users.get_user(session, userId))
    return serializers.json_response(body)
