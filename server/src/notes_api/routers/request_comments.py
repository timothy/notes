"""Request comments: the inspectors of an edit request (its note's owners and its proposer) talk about it.

The request is resolved first through ``edit_requests.inspect`` (404 for anyone else), then the comment
(404 when it is not on this request), then the author or owner rule (403), If-Match (428/400), 412, and
409 note_not_active; closed requests still take comments.
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
from notes_api.services import edit_requests, request_comments


def install_request_comment_routes(app: FastAPI) -> None:
    add_route(app, "GET", "/edit-requests/{requestId}/comments", list_edit_request_comments)
    add_route(app, "POST", "/edit-requests/{requestId}/comments", create_edit_request_comment)
    add_route(app, "GET", "/edit-requests/{requestId}/comments/{commentId}", get_edit_request_comment)
    add_route(app, "PATCH", "/edit-requests/{requestId}/comments/{commentId}", update_edit_request_comment)
    add_route(app, "DELETE", "/edit-requests/{requestId}/comments/{commentId}", delete_edit_request_comment)


def list_edit_request_comments(
    user: CurrentUser, request: Request, requestId: uuid.UUID, limit: Limit = 25, cursor: Cursor = None
) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "list_edit_request_comments"):
        rv = edit_requests.inspect(
            session, caller=user, request_id=requestId, now=clock(request).now(), lock=False
        )
        page = request_comments.list_comments(session, caller=user, rv=rv, limit=limit, cursor=cursor)
        items = [serializers.request_comment(row) for row in page.items]
        body = serializers.page(items, page.next_cursor)
    return serializers.json_response(body)


def create_edit_request_comment(user: CurrentUser, request: Request, requestId: uuid.UUID) -> JSONResponse:
    body = parse_body(request, "CreateComment")
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "create_edit_request_comment"):
        rv = edit_requests.inspect(session, caller=user, request_id=requestId, now=now, lock=True)
        comment = request_comments.create_comment(session, rv=rv, author=user, body=body["body"], now=now)
        payload, etag = serializers.request_comment(comment), request_comments.etag(comment)
    location = f"{API_PREFIX}/edit-requests/{payload['requestId']}/comments/{payload['id']}"
    return serializers.json_response(payload, status=201, headers={"Location": location, "ETag": etag})


def get_edit_request_comment(
    user: CurrentUser, request: Request, requestId: uuid.UUID, commentId: uuid.UUID
) -> JSONResponse:
    with sessions(request)() as session, uow.transaction(session, "get_edit_request_comment"):
        rv = edit_requests.inspect(
            session, caller=user, request_id=requestId, now=clock(request).now(), lock=False
        )
        comment = request_comments.get_comment(session, rv=rv, comment_id=commentId)
        payload, etag = serializers.request_comment(comment), request_comments.etag(comment)
    return serializers.json_response(payload, headers={"ETag": etag})


def update_edit_request_comment(
    user: CurrentUser, request: Request, requestId: uuid.UUID, commentId: uuid.UUID
) -> JSONResponse:
    body = parse_body(request, "UpdateComment")
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "update_edit_request_comment"):
        rv = edit_requests.inspect(session, caller=user, request_id=requestId, now=now, lock=True)
        comment = request_comments.for_edit(session, rv=rv, caller=user, comment_id=commentId)
        request_comments.require_version(comment, parse_if_match(request.headers.get("if-match")))
        comment = request_comments.update_comment(session, rv=rv, comment=comment, body=body["body"], now=now)
        payload, etag = serializers.request_comment(comment), request_comments.etag(comment)
    return serializers.json_response(payload, headers={"ETag": etag})


def delete_edit_request_comment(
    user: CurrentUser, request: Request, requestId: uuid.UUID, commentId: uuid.UUID
) -> Response:
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "delete_edit_request_comment"):
        rv = edit_requests.inspect(session, caller=user, request_id=requestId, now=now, lock=True)
        comment = request_comments.for_delete(session, rv=rv, caller=user, comment_id=commentId)
        request_comments.require_version(comment, parse_if_match(request.headers.get("if-match")))
        request_comments.delete_comment(session, rv=rv, comment=comment)
    return Response(status_code=204)
