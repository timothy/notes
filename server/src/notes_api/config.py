"""Runtime configuration, read from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from notes_api.contract import DEFAULT_CONTRACT_PATH


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    contract_path: Path = DEFAULT_CONTRACT_PATH
    database_url: str = "sqlite:///./notes.sqlite"

    # Bearer access tokens are verified against this issuer's signing keys. Either a JWKS URL or an
    # inline JWKS document (JSON, used by tests) must be configured before authentication can succeed.
    oidc_issuer: str = "https://issuer.example"
    oidc_audience: str = "notes-api"
    oidc_jwks_url: str | None = None
    oidc_jwks: str | None = None
