"""RFC 9457 Problem Details in the contract's shape, and the handlers that make every error one.

``PROBLEMS`` maps each ``ErrorCode`` to its status, title, and default detail, taken from the contract's
own Problem examples (``tests/test_problems.py`` keeps them in step). Handlers cover the three ways an
error surfaces in FastAPI: a raised ``Problem``, a parameter validation error, and Starlette's routing
exceptions. The last-resort handler turns anything else into a 500 that leaks nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from notes_api.contract import FieldError, json_pointer

log = logging.getLogger("notes_api")

PROBLEM_JSON = "application/problem+json"
TYPE_PREFIX = "https://notes-api.example.com/problems/"
BEARER_REALM = 'Bearer realm="notes-api"'


@dataclass(frozen=True, slots=True)
class ProblemType:
    status: int
    title: str
    detail: str


PROBLEMS: dict[str, ProblemType] = {
    "unauthenticated": ProblemType(
        401, "Unauthenticated", "The request did not carry a valid bearer access token."
    ),
    "forbidden": ProblemType(
        403, "Forbidden", "You can see this resource but are not allowed to perform this operation on it."
    ),
    "not_found": ProblemType(
        404, "Not found", "The requested resource does not exist or is not visible to you."
    ),
    "malformed_request": ProblemType(400, "Malformed request", "The request body is not well-formed JSON."),
    "invalid_cursor": ProblemType(
        400, "Invalid cursor", "The cursor is invalid, expired, or was issued for different query settings."
    ),
    "unsupported_media_type": ProblemType(
        415, "Unsupported media type", "Request bodies must use application/json."
    ),
    "validation_failed": ProblemType(
        422, "Validation failed", "The request body does not satisfy the schema."
    ),
    "precondition_required": ProblemType(
        428,
        "Precondition required",
        "This operation requires an If-Match header carrying the resource's current ETag.",
    ),
    "precondition_failed": ProblemType(
        412,
        "Precondition failed",
        "The resource version you supplied no longer matches. Fetch it again and reconcile.",
    ),
    "note_not_active": ProblemType(
        409, "Note is in the trash", "The note is in the trash. Restore it before making changes."
    ),
    "note_already_active": ProblemType(
        409, "Note is already active", "The note is not in the trash, so there is nothing to restore."
    ),
    "request_not_open": ProblemType(
        409,
        "Edit request is closed",
        "The edit request has already been merged, rejected, or withdrawn. Closed records are immutable.",
    ),
    "duplicate_membership": ProblemType(
        409, "Already a member", "This user is already a member of the team."
    ),
    "duplicate_share": ProblemType(
        409,
        "Duplicate share",
        "A share for this recipient already exists. Update or revoke the existing share instead.",
    ),
    "last_admin": ProblemType(
        409, "Last admin", "Every team needs at least one admin. Promote another member first."
    ),
    "merge_conflict": ProblemType(
        409,
        "Merge conflict",
        "The proposal conflicts with the current note. Preview the request and resolve the conflicts.",
    ),
    "direct_edit_not_allowed": ProblemType(
        409,
        "Direct edits are not allowed on a protected note",
        "This note has more than one owner. Submit an edit request; only tags may be changed directly.",
    ),
    "approval_required": ProblemType(
        409,
        "Approval required",
        "The request does not yet have the approvals its note's review policy requires. Nothing was changed.",
    ),
    "duplicate_owner": ProblemType(409, "Already an owner", "This user already owns the note."),
    "author_cannot_be_removed": ProblemType(
        409,
        "The author cannot be removed",
        "The original author is a permanent owner. Ownership does not transfer in this version.",
    ),
}


class Problem(Exception):
    """An error response. Raise it anywhere in a request; the installed handler renders it."""

    def __init__(
        self,
        code: str,
        *,
        detail: str | None = None,
        errors: Sequence[FieldError] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        problem_type = PROBLEMS[code]
        self.code = code
        self.status = problem_type.status
        self.title = problem_type.title
        self.detail = detail or problem_type.detail
        self.errors = list(errors) if errors else None
        self.headers = dict(headers or {})
        super().__init__(f"{self.status} {code}: {self.detail}")

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": TYPE_PREFIX + self.code,
            "title": self.title,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
        }
        if self.errors:
            body["errors"] = [error.as_dict() for error in self.errors]
        return body

    def response(self) -> Response:
        return JSONResponse(
            self.body(), status_code=self.status, media_type=PROBLEM_JSON, headers=self.headers
        )


class Unauthenticated(Problem):
    def __init__(self, *, token_present: bool, detail: str | None = None) -> None:
        challenge = BEARER_REALM + (', error="invalid_token"' if token_present else "")
        super().__init__("unauthenticated", detail=detail, headers={"WWW-Authenticate": challenge})


class Forbidden(Problem):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__("forbidden", detail=detail)


class NotFound(Problem):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__("not_found", detail=detail)


class MalformedRequest(Problem):
    def __init__(self, *, detail: str | None = None, errors: Sequence[FieldError] | None = None) -> None:
        super().__init__("malformed_request", detail=detail, errors=errors)


class InvalidCursor(Problem):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__("invalid_cursor", detail=detail)


class UnsupportedMediaType(Problem):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__("unsupported_media_type", detail=detail)


class ValidationFailed(Problem):
    def __init__(self, errors: Sequence[FieldError], *, detail: str | None = None) -> None:
        super().__init__("validation_failed", detail=detail, errors=errors)


class PreconditionRequired(Problem):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__("precondition_required", detail=detail)


class PreconditionFailed(Problem):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__("precondition_failed", detail=detail)


class Conflict(Problem):
    """A 409 with one of the contract's lifecycle or business codes."""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        if PROBLEMS[code].status != 409:
            raise ValueError(f"{code} is not a 409 error code")
        super().__init__(code, detail=detail)


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(Problem, _problem_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unexpected_handler)


def respond(request: Request, problem: Problem) -> Response:
    """Render a Problem and record its code for the request log."""
    request.state.problem_code = problem.code
    return problem.response()


async def _problem_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, Problem)
    return respond(request, exc)


async def _request_validation_handler(request: Request, exc: Exception) -> Response:
    """Parameter errors are 422 naming the parameter; a body that is not JSON is 400."""
    assert isinstance(exc, RequestValidationError)
    errors: list[FieldError] = []
    for error in exc.errors():
        loc = tuple(error.get("loc", ()))
        if error.get("type") == "json_invalid":
            return respond(request, MalformedRequest())
        location = str(loc[0]) if loc else "body"
        pointer = json_pointer(loc[1:]) if location == "body" else (str(loc[1]) if len(loc) > 1 else "")
        errors.append(FieldError(location, pointer, str(error.get("msg", "is invalid"))))
    return respond(request, ValidationFailed(errors, detail="A query, header, or path parameter is invalid."))


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    """Starlette's routing errors: an unknown route or an undeclared method is the contract's 404."""
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code in (404, 405):
        return respond(request, NotFound())
    if 400 <= exc.status_code < 500:
        return respond(request, MalformedRequest(detail=str(exc.detail)))
    return _internal_error()


async def _unexpected_handler(request: Request, exc: Exception) -> Response:
    log.error("unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return _internal_error()


def _internal_error() -> Response:
    """A 500 that says nothing about the failure. Starlette's error layer bypasses the middleware, so the
    cache header is set here."""
    body = {"type": "about:blank", "title": "Internal Server Error", "status": 500}
    return JSONResponse(body, status_code=500, media_type=PROBLEM_JSON, headers={"Cache-Control": "no-store"})
