"""Ownership: the author adds co-owners and sets the review policy; a co-owner may leave.

Conditional note mutations under the note lock. The ladder is 404 (the note; for a removal also a target
who is not an owner), 403, If-Match (428/400), 412, 409 note_not_active, the operation's own 422s and 409s,
then the write; each answers with the note and its new ETag.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from notes_api import serializers, uow
from notes_api.etags import read_if_match
from notes_api.http.bodies import parse_body
from notes_api.http.deps import CurrentUser
from notes_api.routers import add_route, clock, sessions
from notes_api.services import notes, ownership
from notes_api.services.notes import NoteView


def install_ownership_routes(app: FastAPI) -> None:
    add_route(app, "POST", "/notes/{noteId}/owners", add_owner)
    add_route(app, "DELETE", "/notes/{noteId}/owners/{userId}", remove_owner)
    add_route(app, "PATCH", "/notes/{noteId}/review-policy", update_review_policy)


def add_owner(user: CurrentUser, request: Request, noteId: uuid.UUID) -> JSONResponse:
    body = parse_body(request, "AddOwner")
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "add_owner"):
        view = notes.lock(session, caller=user, note_id=noteId, now=now)
        ownership.require_author(view, user)
        notes.require_version(view, read_if_match(request.headers))
        view = ownership.add_owner(session, view=view, user_id=uuid.UUID(body["userId"]), now=now)
        payload, etag = _payload(view), view.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def remove_owner(user: CurrentUser, request: Request, noteId: uuid.UUID, userId: uuid.UUID) -> JSONResponse:
    """No request body: whatever arrives is ignored."""
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "remove_owner"):
        view = notes.lock(session, caller=user, note_id=noteId, now=now)
        ownership.authorize_removal(view, user, userId)
        notes.require_version(view, read_if_match(request.headers))
        view = ownership.remove_owner(session, view=view, caller=user, user_id=userId, now=now)
        payload, etag = _payload(view), view.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def update_review_policy(user: CurrentUser, request: Request, noteId: uuid.UUID) -> JSONResponse:
    body = parse_body(request, "ReviewPolicy")
    now = clock(request).now()
    with sessions(request)() as session, uow.transaction(session, "update_review_policy"):
        view = notes.lock(session, caller=user, note_id=noteId, now=now)
        ownership.require_author(view, user)
        notes.require_version(view, read_if_match(request.headers))
        view = ownership.set_review_policy(
            session, view=view, mode=body["mode"], required=body["requiredApprovals"], now=now
        )
        payload, etag = _payload(view), view.etag
    return serializers.json_response(payload, headers={"ETag": etag})


def _payload(view: NoteView) -> dict[str, Any]:
    return serializers.note(view.note, view.owner_ids, view.tags, view.access)
