"""Teams and memberships: nine operations, all unconditional (no ETag), all behind the team lock.

Bodies are parsed before the transaction (the ladder is 401, request shape, then 404 and 403), so no lock
is held while a body is read and a schema error reveals nothing about the team.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from notes_api import serializers, uow
from notes_api.generated.schemas import TeamScope
from notes_api.http.bodies import parse_body
from notes_api.http.deps import CurrentUser
from notes_api.routers import API_PREFIX, Cursor, Limit, add_route, clock, sessions
from notes_api.services import teams


def install_team_routes(app: FastAPI) -> None:
    add_route(app, "POST", "/teams", create_team)
    add_route(app, "GET", "/teams", list_teams)
    add_route(app, "GET", "/teams/{teamId}", get_team)
    add_route(app, "PATCH", "/teams/{teamId}", update_team)
    add_route(app, "DELETE", "/teams/{teamId}", delete_team)
    add_route(app, "GET", "/teams/{teamId}/members", list_memberships)
    add_route(app, "POST", "/teams/{teamId}/members", add_membership)
    add_route(app, "PATCH", "/teams/{teamId}/members/{userId}", update_membership)
    add_route(app, "DELETE", "/teams/{teamId}/members/{userId}", remove_membership)


def create_team(user: CurrentUser, request: Request) -> JSONResponse:
    body = parse_body(request, "CreateTeam")
    with sessions(request)() as session, uow.transaction(session, "create_team"):
        team = teams.create_team(session, creator=user, name=body["name"], clock=clock(request))
        payload = serializers.team(team)
    location = f"{API_PREFIX}/teams/{payload['id']}"
    return serializers.json_response(payload, status=201, headers={"Location": location})


def list_teams(
    user: CurrentUser,
    request: Request,
    scope: Annotated[TeamScope, Query()] = TeamScope.all,
    limit: Limit = 25,
    cursor: Cursor = None,
) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "list_teams"):
        page = teams.list_teams(session, caller=user, scope=scope.value, limit=limit, cursor=cursor)
        body = serializers.page([serializers.team(row) for row in page.items], page.next_cursor)
    return serializers.json_response(body)


def get_team(user: CurrentUser, request: Request, teamId: uuid.UUID) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "get_team"):
        body = serializers.team(teams.get_team(session, teamId))
    return serializers.json_response(body)


def update_team(user: CurrentUser, request: Request, teamId: uuid.UUID) -> JSONResponse:
    body = parse_body(request, "UpdateTeam")
    with sessions(request)() as session, uow.transaction(session, "update_team"):
        team = teams.lock_team_as_admin(session, caller=user, team_id=teamId)
        payload = serializers.team(
            teams.rename_team(session, team=team, name=body["name"], clock=clock(request))
        )
    return serializers.json_response(payload)


def delete_team(user: CurrentUser, request: Request, teamId: uuid.UUID) -> Response:
    with sessions(request)() as session, uow.transaction(session, "delete_team"):
        team = teams.lock_team_as_admin(session, caller=user, team_id=teamId)
        teams.delete_team(session, team=team)
    return Response(status_code=204)


def list_memberships(
    user: CurrentUser, request: Request, teamId: uuid.UUID, limit: Limit = 25, cursor: Cursor = None
) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "list_memberships"):
        page = teams.list_members(session, caller=user, team_id=teamId, limit=limit, cursor=cursor)
        body = serializers.page([serializers.membership(row) for row in page.items], page.next_cursor)
    return serializers.json_response(body)


def add_membership(user: CurrentUser, request: Request, teamId: uuid.UUID) -> JSONResponse:
    body = parse_body(request, "AddMembership")
    with sessions(request)() as session, uow.transaction(session, "add_membership"):
        team = teams.lock_team_as_admin(session, caller=user, team_id=teamId)
        membership = teams.add_member(
            session,
            team=team,
            user_id=uuid.UUID(body["userId"]),
            role=body.get("role", teams.MEMBER),
            clock=clock(request),
        )
        payload = serializers.membership(membership)
    location = f"{API_PREFIX}/teams/{payload['teamId']}/members/{payload['userId']}"
    return serializers.json_response(payload, status=201, headers={"Location": location})


def update_membership(
    user: CurrentUser, request: Request, teamId: uuid.UUID, userId: uuid.UUID
) -> JSONResponse:
    body = parse_body(request, "UpdateMembership")
    with sessions(request)() as session, uow.transaction(session, "update_membership"):
        team = teams.lock_team_as_admin(session, caller=user, team_id=teamId)
        membership = teams.set_role(
            session, team=team, user_id=userId, role=body["role"], clock=clock(request)
        )
        payload = serializers.membership(membership)
    return serializers.json_response(payload)


def remove_membership(user: CurrentUser, request: Request, teamId: uuid.UUID, userId: uuid.UUID) -> Response:
    with sessions(request)() as session, uow.transaction(session, "remove_membership"):
        teams.remove_member(session, caller=user, team_id=teamId, user_id=userId)
    return Response(status_code=204)
