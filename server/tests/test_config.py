"""Configuration fails fast: a process without DATABASE_URL does not start, and the image's overrides work."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from notes_api.config import Settings
from notes_api.contract import DEFAULT_CONTRACT_PATH

POSTGRES_URL = "postgresql+psycopg://notes:notes@db:5432/notes"


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert "database_url" in str(excinfo.value)


def test_database_url_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    assert Settings().database_url == POSTGRES_URL


def test_contract_path_defaults_to_the_checkout_and_the_environment_overrides_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.delenv("CONTRACT_PATH", raising=False)
    assert Settings().contract_path == DEFAULT_CONTRACT_PATH
    monkeypatch.setenv("CONTRACT_PATH", str(tmp_path / "openapi.yaml"))
    assert Settings().contract_path == tmp_path / "openapi.yaml"
