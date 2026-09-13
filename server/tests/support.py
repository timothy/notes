"""Test doubles: a controllable clock and a local token issuer with named personas."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey


class FakeClock:
    """A clock that only moves when a test tells it to."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs an aware start time")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FakeClock needs an aware time")
        self._now = moment


@dataclass(frozen=True)
class Persona:
    subject: str
    display_name: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class LocalIssuer:
    """An identity provider for tests: an RSA key pair, its JWKS, and RS256 access tokens."""

    def __init__(
        self, issuer: str = "https://issuer.example", audience: str = "notes-api", key_id: str = "test-key-1"
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.key_id = key_id
        self._private_key: RSAPrivateKey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key: RSAPublicKey = self._private_key.public_key()
        numbers = self.public_key.public_numbers()
        self.jwks: dict[str, Any] = {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": key_id,
                    "n": _b64url_uint(numbers.n),
                    "e": _b64url_uint(numbers.e),
                }
            ]
        }

    def token(
        self,
        subject: str,
        name: str | None = None,
        *,
        audience: str | None = None,
        expires_in: timedelta = timedelta(hours=1),
        issued_at: datetime | None = None,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        now = issued_at or datetime.now(UTC)
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": subject,
            "aud": audience or self.audience,
            "iat": int(now.timestamp()),
            "exp": int((now + expires_in).timestamp()),
        }
        if name is not None:
            claims["name"] = name
        claims.update(extra_claims or {})
        return jwt.encode(claims, self._private_key, algorithm="RS256", headers={"kid": self.key_id})

    def persona(self, subject: str, display_name: str) -> Persona:
        return Persona(subject, display_name, self.token(subject, display_name))
