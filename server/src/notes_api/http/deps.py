"""Request-scoped dependencies: who is calling.

``current_user`` is the first rung of every operation's check ladder: it verifies the bearer token and
maps the identity to a local user, provisioning one on first contact. FastAPI resolves dependencies before
it validates path and query parameters, so a missing or invalid token is ``401`` even when the rest of the
request is malformed.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Request

from notes_api.auth.jwt import Identity, InvalidToken, TokenVerifier
from notes_api.auth.provisioning import get_or_create_user
from notes_api.http.problems import Unauthenticated
from notes_api.models import User

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


def current_user(request: Request) -> User:
    """The local user behind the bearer token, provisioned on first contact."""
    identity = bearer_identity(request)
    user = get_or_create_user(request.app.state.session_factory, identity, request.app.state.clock)
    request.state.user_id = str(user.id)  # for the request log; never the subject or the token
    return user


CurrentUser = Annotated[User, Depends(current_user)]
