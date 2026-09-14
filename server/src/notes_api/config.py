"""Runtime configuration, read from environment variables.

Every setting without a default is required, and a process without one refuses to start. ``load_settings``
is the entry point for the application and for Alembic: it turns pydantic's validation error, whose text
embeds the values it was given (including the database password), into a ``ConfigurationError`` that names
only the variables and what is wrong with them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import StringConstraints, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from notes_api.contract import DEFAULT_CONTRACT_PATH

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ConfigurationError(RuntimeError):
    """The environment does not configure the server. The message names variables, never values."""


class Settings(BaseSettings):
    # env_ignore_empty: an empty variable counts as unset, so a compose default of ``${OIDC_JWKS_URL:-}``
    # leaves the field at None and a required field stays required.
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    contract_path: Path = DEFAULT_CONTRACT_PATH
    # Required. Tests pass a SQLite URL explicitly and containers get PostgreSQL from the environment, so a
    # process started without DATABASE_URL fails here instead of quietly writing a SQLite file somewhere.
    database_url: str

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


def describe(exc: ValidationError) -> str:
    lines = ["invalid configuration; set these environment variables and restart:"]
    for error in exc.errors():
        loc = error.get("loc", ())
        message = str(error.get("msg", "is invalid"))
        lines.append(f"  {str(loc[0]).upper()}: {message}" if loc else f"  {message}")
    return "\n".join(lines)
