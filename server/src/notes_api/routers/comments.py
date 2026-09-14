"""Comments: readers list and read, commenters add, authors edit, owners and authors delete.

The note is resolved before the comment, and the comment before the author rule, so the ladder is 404
(note, then a comment that is not on this note), 403, If-Match (428/400), 412, 409, write.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from notes_api import serializers, uow
from notes_api.etags import parse_if_match
from notes_api.http.bodies import parse_body
from notes_api.http.deps import CurrentUser
from notes_api.routers import API_PREFIX, Cursor, Limit, add_route, clock, sessions
from notes_api.services import comments, notes


def install_comment_routes(app: FastAPI) -> None:
    add_route(app, "GET", "/notes/{noteId}/comments", list_comments)
    add_route(app, "POST", "/notes/{noteId}/comments", create_comment)
    add_route(app, "GET", "/notes/{noteId}/comments/{commentId}", get_comment)
    add_route(app, "PATCH", "/notes/{noteId}/comments/{commentId}", update_comment)
    add_route(app, "DELETE", "/notes/{noteId}/comments/{commentId}", delete_comment)


def list_comments(
    user: CurrentUser, request: Request, noteId: uuid.UUID, limit: Limit = 25, cursor: Cursor = None
) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "list_comments"):
        view = notes.read(session, caller=user, note_id=noteId, now=clock(request).now())
        page = comments.list_comments(session, caller=user, view=view, limit=limit, cursor=cursor)
        body = serializers.page([serializers.comment(row) for row in page.items], page.next_cursor)
    return serializers.json_response(body)


def create_comment(user: CurrentUser, request: Request, noteId: uuid.UUID) -> JSONResponse:
    body = parse_body(request, "CreateComment")
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "create_comment"):
        view = notes.lock(session, caller=user, note_id=noteId, now=now, lock_access=True)
        comment = comments.create_comment(session, view=view, author=user, body=body["body"], now=now)
        payload, etag = serializers.comment(comment), comments.etag(comment)
    location = f"{API_PREFIX}/notes/{payload['noteId']}/comments/{payload['id']}"
    return serializers.json_response(payload, status=201, headers={"Location": location, "ETag": etag})


def get_comment(user: CurrentUser, request: Request, noteId: uuid.UUID, commentId: uuid.UUID) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "get_comment"):
        view = notes.read(session, caller=user, note_id=noteId, now=clock(request).now())
        comment = comments.get_comment(session, view=view, comment_id=commentId)
        payload, etag = serializers.comment(comment), comments.etag(comment)
    return serializers.json_response(payload, headers={"ETag": etag})


def update_comment(
    user: CurrentUser, request: Request, noteId: uuid.UUID, commentId: uuid.UUID
) -> JSONResponse:
    body = parse_body(request, "UpdateComment")
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "update_comment"):
        view = notes.lock(session, caller=user, note_id=noteId, now=now, lock_access=True)
        comment = comments.for_edit(session, view=view, caller=user, comment_id=commentId)
        comments.require_version(comment, parse_if_match(request.headers.get("if-match")))
        comment = comments.update_comment(session, view=view, comment=comment, body=body["body"], now=now)
        payload, etag = serializers.comment(comment), comments.etag(comment)
    return serializers.json_response(payload, headers={"ETag": etag})


def delete_comment(user: CurrentUser, request: Request, noteId: uuid.UUID, commentId: uuid.UUID) -> Response:
    with sessions(request)() as session, uow.transaction(session, "delete_comment"):
        view = notes.lock(session, caller=user, note_id=noteId, now=clock(request).now(), lock_access=True)
        comment = comments.for_delete(session, view=view, caller=user, comment_id=commentId)
        comments.require_version(comment, parse_if_match(request.headers.get("if-match")))
        comments.delete_comment(session, view=view, comment=comment)
    return Response(status_code=204)
