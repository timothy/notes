"""Test doubles: a controllable clock, the local token issuer with named personas, and settings builders."""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from notes_api.config import Settings
from notes_api.dev_issuer import DevIssuer


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


class LocalIssuer(DevIssuer):
    """The dev issuer with named personas. Persona tokens outlive any test run (eight hours)."""

    def __init__(
        self, issuer: str = "https://issuer.example", audience: str = "notes-api", key_id: str = "test-key-1"
    ) -> None:
        super().__init__(issuer=issuer, audience=audience, key_id=key_id)

    def persona(self, subject: str, display_name: str) -> Persona:
        return Persona(
            subject, display_name, self.token(subject, display_name, expires_in=timedelta(hours=8))
        )


@functools.lru_cache(maxsize=1)
def default_issuer() -> LocalIssuer:
    """One issuer for tests that need valid settings but never verify a token."""
    return LocalIssuer()


def settings_for(database_url: str, issuer: LocalIssuer | None = None, **overrides: Any) -> Settings:
    """Complete settings for tests: the database plus the issuer's inline JWKS."""
    issuer = issuer or default_issuer()
    values: dict[str, Any] = {
        "database_url": database_url,
        "oidc_issuer": issuer.issuer,
        "oidc_audience": issuer.audience,
        "oidc_jwks": json.dumps(issuer.jwks),
    }
    values.update(overrides)
    return Settings(**values)
