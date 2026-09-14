"""Bearer access token verification.

A token is accepted when its signature verifies against a key from the configured source (an inline JWKS
document or a JWKS URL), its ``iss`` and ``aud`` match the settings, its time claims hold with a small
leeway, and its ``typ`` header, when present, names an access token. Every other outcome is
``InvalidToken``; the HTTP layer turns that into the contract's 401 without echoing the reason. Nothing
here reads the clock the application injects: PyJWT checks ``exp`` and ``nbf`` against system time.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from jwt import PyJWK, PyJWKClient, PyJWKSet
from jwt.exceptions import PyJWTError

from notes_api.config import ConfigurationError, Settings

ALGORITHMS = ["RS256", "ES256"]
ACCESS_TOKEN_TYPES = frozenset({"at+jwt", "jwt"})
LEEWAY_SECONDS = 60
DISPLAY_NAME_MAX_LENGTH = 200
JWKS_CACHE_SECONDS = 300
JWKS_TIMEOUT_SECONDS = 5


class InvalidToken(Exception):
    """The token was rejected. The message names the reason for logs; responses never carry it."""


@dataclass(frozen=True, slots=True)
class Identity:
    issuer: str
    subject: str
    display_name: str


class KeySource(Protocol):
    def signing_key(self, token: str) -> PyJWK: ...


class InlineKeys:
    """Keys from the ``OIDC_JWKS`` document; an unusable document fails at startup."""

    def __init__(self, document: str) -> None:
        try:
            self._keys = PyJWKSet.from_dict(json.loads(document))
        except (ValueError, PyJWTError) as exc:
            raise ConfigurationError(
                f"OIDC_JWKS is not a usable JWKS document ({type(exc).__name__})"
            ) from None

    def signing_key(self, token: str) -> PyJWK:
        kid = jwt.get_unverified_header(token).get("kid")
        if not isinstance(kid, str):
            raise InvalidToken("token has no kid header")
        try:
            return self._keys[kid]
        except KeyError:
            raise InvalidToken("unknown kid") from None


class RemoteKeys:
    """Keys fetched from ``OIDC_JWKS_URL`` on demand and cached in memory.

    A failed fetch rejects the token; nothing is fetched at startup.
    """

    def __init__(self, url: str) -> None:
        self._client = PyJWKClient(
            url, cache_keys=True, lifespan=JWKS_CACHE_SECONDS, timeout=JWKS_TIMEOUT_SECONDS
        )

    def signing_key(self, token: str) -> PyJWK:
        return self._client.get_signing_key_from_jwt(token)


class TokenVerifier:
    def __init__(self, settings: Settings) -> None:
        self._issuer = settings.oidc_issuer
        self._audience = settings.oidc_audience
        self._keys: KeySource
        if settings.oidc_jwks is not None:
            self._keys = InlineKeys(settings.oidc_jwks)
        else:
            assert settings.oidc_jwks_url is not None  # Settings enforces exactly one key source
            self._keys = RemoteKeys(settings.oidc_jwks_url)

    def verify(self, token: str) -> Identity:
        try:
            token_type = jwt.get_unverified_header(token).get("typ")
            if token_type is not None and (
                not isinstance(token_type, str) or token_type.lower() not in ACCESS_TOKEN_TYPES
            ):
                raise InvalidToken("typ header is not an access token type")
            key = self._keys.signing_key(token)
            # Passing the PyJWK (not its raw key) also binds the header's alg to the key's algorithm.
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "sub"]},
            )
        except PyJWTError as exc:
            raise InvalidToken(type(exc).__name__) from None
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject:
            raise InvalidToken("sub is not a non-empty string")
        return Identity(self._issuer, subject, display_name_from(claims))


def display_name_from(claims: Mapping[str, Any]) -> str:
    """``name``, else ``preferred_username``, else ``user-`` plus the start of ``sub``.

    Whitespace-only values fall through; the result is at most 200 code points.
    """
    for claim in ("name", "preferred_username"):
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()[:DISPLAY_NAME_MAX_LENGTH].rstrip()
    return f"user-{str(claims['sub'])[:8]}"
