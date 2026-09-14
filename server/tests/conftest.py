"""Shared fixtures: a fresh database per test, a pinned clock, a local token issuer, personas, and the client.

The database is a file-based SQLite database in the test's temporary directory, or the PostgreSQL
database named by NOTES_API_TEST_DATABASE_URL (the CI job sets it). The schema is created from the
models; ``tests/test_schema.py`` proves the migration produces the same schema.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine

from notes_api import uow
from notes_api.config import Settings
from notes_api.main import create_app
from notes_api.models import Base
from tests.contract_client import ContractClient
from tests.support import FakeClock, LocalIssuer, Persona, settings_for

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def issuer() -> LocalIssuer:
    return LocalIssuer()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def settings(tmp_path: Path, issuer: LocalIssuer) -> Settings:
    url = os.environ.get("NOTES_API_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.sqlite'}"
    return settings_for(url, issuer)


@pytest.fixture
def app(settings: Settings, clock: FakeClock) -> Iterator[FastAPI]:
    app = create_app(settings, clock=clock)
    engine: Engine = app.state.engine
    if engine.dialect.name != "sqlite":
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield app
    if engine.dialect.name != "sqlite":
        Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(app: FastAPI) -> ContractClient:
    return ContractClient(app)


@pytest.fixture(scope="session")
def ada(issuer: LocalIssuer) -> Persona:
    return issuer.persona("ada", "Ada Okafor")


@pytest.fixture(scope="session")
def ben(issuer: LocalIssuer) -> Persona:
    return issuer.persona("ben", "Ben Ortiz")


@pytest.fixture(scope="session")
def cara(issuer: LocalIssuer) -> Persona:
    return issuer.persona("cara", "Cara Nakamura")


@pytest.fixture(scope="session")
def dan(issuer: LocalIssuer) -> Persona:
    return issuer.persona("dan", "Dan Whitfield")


@pytest.fixture
def restore_hooks() -> Iterator[None]:
    """Let a test replace ``uow.hooks.before_begin`` and put the no-op back afterwards."""
    original = uow.hooks.before_begin
    yield
    uow.hooks.before_begin = original
