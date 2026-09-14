"""The probes: /healthz needs nothing, /readyz needs the database, and neither is part of the contract."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notes_api.main import create_app
from tests.contract_client import ContractClient, ContractViolation
from tests.support import settings_for

PROBLEM_JSON = "application/problem+json"
# Nothing listens on port 1, so connecting is refused at once; the password must never appear in a response.
UNREACHABLE_URL = "postgresql+psycopg://notes:very-secret-password@127.0.0.1:1/notes"


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """The conftest app (SQLite, or PostgreSQL under NOTES_API_TEST_DATABASE_URL) behind a plain client."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def unreachable_client() -> Iterator[TestClient]:
    app = create_app(settings_for(UNREACHABLE_URL))
    yield TestClient(app, raise_server_exceptions=False)
    app.state.engine.dispose()


def test_healthz_is_ok_and_uncacheable(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"status": "ok"}


def test_readyz_is_ok_when_the_database_answers(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}


def test_readyz_is_503_when_the_database_is_unreachable(unreachable_client: TestClient) -> None:
    response = unreachable_client.get("/readyz")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"status": "unavailable", "checks": {"database": "unavailable"}}
    for secret in ("very-secret-password", "127.0.0.1", "psycopg", "Traceback"):
        assert secret not in response.text


def test_healthz_stays_ok_when_the_database_is_unreachable(unreachable_client: TestClient) -> None:
    assert unreachable_client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/healthz"),
        ("DELETE", "/readyz"),
        ("GET", "/v1/healthz"),
        ("GET", "/v1/readyz"),
        ("GET", "/health"),
    ],
)
def test_other_methods_and_paths_are_the_contract_404(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "not_found"


def test_the_probes_are_outside_the_contract(app: FastAPI) -> None:
    with pytest.raises(ContractViolation, match="not in the contract"):
        ContractClient(app).get("/healthz")
