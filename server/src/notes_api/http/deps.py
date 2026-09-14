"""Request-scoped dependencies: who is calling.

``bearer_identity`` is the first rung of every operation's check ladder. FastAPI resolves dependencies
before it validates path and query parameters, so a missing or invalid token is ``401`` even when the
rest of the request is malformed.
"""

from __future__ import annotations

import logging

from fastapi import Request

from notes_api.auth.jwt import Identity, InvalidToken, TokenVerifier
from notes_api.http.problems import Unauthenticated

log = logging.getLogger(__name__)

MISSING_TOKEN_DETAIL = "Provide a bearer access token."
INVALID_TOKEN_DETAIL = "The access token could not be verified."


def bearer_token(request: Request) -> str | None:
    """The token of an ``Authorization: Bearer`` header, or None when there is no bearer token at all."""
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def bearer_identity(request: Request) -> Identity:
    token = bearer_token(request)
    if token is None:
        raise Unauthenticated(token_present=False, detail=MISSING_TOKEN_DETAIL)
    verifier: TokenVerifier = request.app.state.verifier
    try:
        return verifier.verify(token)
    except InvalidToken as exc:
        log.info("rejected bearer token: %s", exc)
        raise Unauthenticated(token_present=True, detail=INVALID_TOKEN_DETAIL) from None
