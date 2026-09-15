"""Runtime configuration, read from environment variables.

Every setting without a default is required, and a process without one refuses to start. ``load_settings``
and ``load_database_settings`` are the API and maintenance entry points. They turn validation errors
(which embed input values, including secrets) into a ``ConfigurationError`` naming only the variables
and what is wrong with them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import SecretStr, StringConstraints, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from notes_api.contract import DEFAULT_CONTRACT_PATH

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ConfigurationError(RuntimeError):
    """The environment does not configure the server. The message names variables, never values."""


class DatabaseSettings(BaseSettings):
    # env_ignore_empty: an empty variable counts as unset, so a compose default of ``${OIDC_JWKS_URL:-}``
    # leaves the field at None and a required field stays required.
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    # Required. Tests pass a SQLite URL explicitly and containers get PostgreSQL from the environment, so a
    # process started without DATABASE_URL fails here instead of quietly writing a SQLite file somewhere.
    database_url: str


class Settings(DatabaseSettings):
    contract_path: Path = DEFAULT_CONTRACT_PATH
    cursor_signing_key: SecretStr

    @field_validator("cursor_signing_key")
    @classmethod
    def _valid_cursor_key(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r"[0-9a-fA-F]{64}", value.get_secret_value()) is None:
            raise ValueError("must contain exactly 64 hexadecimal characters (32 random bytes)")
        return value

    # Bearer access tokens are verified against this issuer's signing keys: the token's ``iss`` and ``aud``
    # must match, and the key comes from the JWKS URL or from an inline JWKS document (JSON; the dev issuer
    # and the tests use the inline form). Exactly one key source must be set.
    oidc_issuer: NonBlank
    oidc_audience: NonBlank
    oidc_jwks_url: NonBlank | None = None
    oidc_jwks: NonBlank | None = None

    @model_validator(mode="after")
    def _exactly_one_key_source(self) -> Self:
        if (self.oidc_jwks_url is None) == (self.oidc_jwks is None):
            raise ValueError("set exactly one of OIDC_JWKS_URL or OIDC_JWKS")
        return self


def load_settings(**overrides: Any) -> Settings:
    """``Settings`` from the environment, or a ``ConfigurationError`` that is safe to log."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        raise ConfigurationError(describe(exc)) from None


def load_database_settings() -> DatabaseSettings:
    """Operator commands need database access, not authentication or cursor signing secrets."""
    try:
        return DatabaseSettings()
    except ValidationError as exc:
        raise ConfigurationError(describe(exc)) from None


def describe(exc: ValidationError) -> str:
    lines = ["invalid configuration; set these environment variables and restart:"]
    for error in exc.errors():
        loc = error.get("loc", ())
        message = str(error.get("msg", "is invalid"))
        lines.append(f"  {str(loc[0]).upper()}: {message}" if loc else f"  {message}")
    return "\n".join(lines)
