"""Runtime configuration, read from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from notes_api.contract import DEFAULT_CONTRACT_PATH


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    contract_path: Path = DEFAULT_CONTRACT_PATH
    # Required. Tests pass a SQLite URL explicitly and containers get PostgreSQL from the environment, so a
    # process started without DATABASE_URL fails here instead of quietly writing a SQLite file somewhere.
    database_url: str

    # Bearer access tokens are verified against this issuer's signing keys. Either a JWKS URL or an
    # inline JWKS document (JSON, used by tests) must be configured before authentication can succeed.
    oidc_issuer: str = "https://issuer.example"
    oidc_audience: str = "notes-api"
    oidc_jwks_url: str | None = None
    oidc_jwks: str | None = None
