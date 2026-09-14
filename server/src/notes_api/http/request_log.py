"""One JSON line per request on stdout, and a request id on every response.

The line carries what an operator needs to correlate and diagnose (request id, method, matched route
template, path, status, Problem code, caller id, duration) and nothing that could leak: never the query
string (search text), never headers (tokens), never bodies (note content). Probes are not logged. An
incoming ``X-Request-Id`` is honoured when it is short and simple, otherwise a fresh id is generated; the
id is echoed on the response so a client and the log can be matched.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
UNLOGGED_PATHS = frozenset({"/healthz", "/readyz"})
LOGGER_NAME = "notes_api.request"

log = logging.getLogger(LOGGER_NAME)


def configure_request_logging() -> None:
    """Send request lines to stdout as they are. Idempotent, so tests and factories may call it freely."""
    if not any(getattr(handler, "_notes_api_request_log", False) for handler in log.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._notes_api_request_log = True  # type: ignore[attr-defined]
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def request_id_from(header: str | None) -> str:
    if header is not None and REQUEST_ID_PATTERN.match(header):
        return header
    return uuid.uuid4().hex


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = request_id_from(Headers(scope=scope).get(REQUEST_ID_HEADER))
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id
        started = time.perf_counter()
        status = 500  # what the outermost error handler will send if the app never starts a response

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            if scope["path"] not in UNLOGGED_PATHS:
                log.info(_line(scope, state, status, time.perf_counter() - started))


def _line(scope: Scope, state: dict[str, Any], status: int, elapsed: float) -> str:
    route = scope.get("route")
    record = {
        "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "request_id": state.get("request_id"),
        "method": scope["method"],
        "route": getattr(route, "path", None),
        "path": scope["path"],
        "status": status,
        "code": state.get("problem_code"),
        "user": state.get("user_id"),
        "duration_ms": round(elapsed * 1000, 3),
    }
    return json.dumps(record)
