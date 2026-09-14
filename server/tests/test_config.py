"""Configuration fails fast and safely.

A process without DATABASE_URL or the OIDC settings does not start, blank variables count as unset, exactly
one key source is allowed, and the error a container logs names the variables but never their values.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from notes_api.config import ConfigurationError, Settings, load_settings
from notes_api.contract import DEFAULT_CONTRACT_PATH

PASSWORD = "s3cret-db-password"
POSTGRES_URL = f"postgresql+psycopg://notes:{PASSWORD}@db:5432/notes"
VARIABLES = ("DATABASE_URL", "CONTRACT_PATH", "OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS", "OIDC_JWKS_URL")
COMPLETE = {
    "DATABASE_URL": POSTGRES_URL,
    "OIDC_ISSUER": "https://issuer.example",
    "OIDC_AUDIENCE": "notes-api",
    "OIDC_JWKS": '{"keys": []}',
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A complete environment; tests remove or override variables from it."""
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in COMPLETE.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def failing_fields(excinfo: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return {str(error["loc"][0]) for error in excinfo.value.errors() if error["loc"]}


def test_a_complete_environment_loads(env: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    assert settings.database_url == POSTGRES_URL
    assert settings.oidc_issuer == "https://issuer.example"
    assert settings.oidc_audience == "notes-api"
    assert settings.oidc_jwks == '{"keys": []}'
    assert settings.oidc_jwks_url is None


@pytest.mark.parametrize("name", ["DATABASE_URL", "OIDC_ISSUER", "OIDC_AUDIENCE"])
def test_each_required_variable_is_required(env: pytest.MonkeyPatch, name: str) -> None:
    env.delenv(name)
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert failing_fields(excinfo) == {name.lower()}


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_values_do_not_satisfy_a_required_variable(env: pytest.MonkeyPatch, value: str) -> None:
    env.setenv("OIDC_ISSUER", value)
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert failing_fields(excinfo) == {"oidc_issuer"}


def test_an_empty_optional_variable_counts_as_unset(env: pytest.MonkeyPatch) -> None:
    env.setenv("OIDC_JWKS_URL", "")
    assert load_settings().oidc_jwks_url is None


def test_exactly_one_key_source_is_allowed(env: pytest.MonkeyPatch) -> None:
    env.setenv("OIDC_JWKS_URL", "https://issuer.example/jwks")
    with pytest.raises(ValidationError, match="exactly one of OIDC_JWKS_URL or OIDC_JWKS"):
        Settings()
    env.delenv("OIDC_JWKS_URL")
    env.delenv("OIDC_JWKS")
    with pytest.raises(ConfigurationError, match="exactly one of OIDC_JWKS_URL or OIDC_JWKS"):
        load_settings()
    env.setenv("OIDC_JWKS_URL", "https://issuer.example/jwks")
    assert load_settings().oidc_jwks_url == "https://issuer.example/jwks"


def test_the_configuration_error_names_variables_but_never_values(env: pytest.MonkeyPatch) -> None:
    env.delenv("OIDC_ISSUER")
    env.setenv("OIDC_AUDIENCE", " ")
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "OIDC_ISSUER: Field required" in message
    assert "OIDC_AUDIENCE" in message
    assert PASSWORD not in message and POSTGRES_URL not in message
    # The pydantic error (whose text embeds the input values) must not be attached as cause or context.
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


def test_unrelated_variables_are_ignored(env: pytest.MonkeyPatch) -> None:
    env.setenv("NOTES_DEV_ISSUER_KEY", "not-a-setting")
    load_settings()


def test_contract_path_defaults_to_the_checkout_and_the_environment_overrides_it(
    env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert Settings().contract_path == DEFAULT_CONTRACT_PATH
    env.setenv("CONTRACT_PATH", str(tmp_path / "openapi.yaml"))
    assert Settings().contract_path == tmp_path / "openapi.yaml"
