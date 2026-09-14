"""Users: the caller's own profile and the directory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from notes_api import serializers
from notes_api.http.deps import CurrentUser
from notes_api.routers import add_route


def install_user_routes(app: FastAPI) -> None:
    add_route(app, "GET", "/me", get_current_user)


def get_current_user(user: CurrentUser) -> JSONResponse:
    return serializers.json_response(serializers.user(user))
