"""Every request leaves one JSON line that says what happened and nothing that could leak."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest

from notes_api.http.request_log import LOGGER_NAME
from tests.contract_client import ContractClient
from tests.support import Persona

FIELDS = ["time", "request_id", "method", "route", "path", "status", "code", "user", "duration_ms"]


@pytest.fixture
def lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Request log records as parsed JSON, in order."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    records: list[dict[str, Any]] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(json.loads(record.getMessage()))

    logging.getLogger(LOGGER_NAME).addHandler(Collect())
    return records


def test_an_authenticated_request_logs_route_status_user_and_id(
    client: ContractClient, ada: Persona, lines: list[dict[str, Any]]
) -> None:
    response = client.get("/v1/me", auth=ada)
    assert len(lines) == 1
    line = lines[0]
    assert list(line) == FIELDS
    assert line["method"] == "GET" and line["route"] == "/v1/me" and line["path"] == "/v1/me"
    assert line["status"] == 200 and line["code"] is None
    assert line["user"] == response.json()["id"]
    assert line["request_id"] == response.headers["X-Request-Id"]
    assert len(line["request_id"]) == 32
    assert line["duration_ms"] >= 0
    assert line["time"].endswith("Z")


def test_problems_log_their_code(client: ContractClient, ada: Persona, lines: list[dict[str, Any]]) -> None:
    client.get("/v1/me")
    client.get("/v1/nope", auth=ada)
    client.get("/v1/users", auth=ada, params={"limit": 0})
    client.get(f"/v1/users/{uuid.uuid4()}", auth=ada)
    assert [(line["status"], line["code"], line["route"]) for line in lines] == [
        (401, "unauthenticated", "/v1/me"),
        (404, "not_found", None),
        (422, "validation_failed", "/v1/users"),
        (404, "not_found", "/v1/users/{userId}"),
    ]
    # Unknown routes never reach authentication, so no user is recorded for them.
    assert lines[0]["user"] is None and lines[1]["user"] is None and lines[2]["user"] is not None


def test_probes_are_not_logged(client: ContractClient, lines: list[dict[str, Any]]) -> None:
    from fastapi.testclient import TestClient

    plain = TestClient(client.app)
    assert plain.get("/healthz").status_code == 200
    assert plain.get("/readyz").status_code == 200
    assert lines == []


def test_a_valid_incoming_request_id_is_kept_and_an_invalid_one_replaced(
    client: ContractClient, ada: Persona, lines: list[dict[str, Any]]
) -> None:
    kept = client.get("/v1/me", auth=ada, headers={"X-Request-Id": "trace-1.2_3"})
    assert kept.headers["X-Request-Id"] == "trace-1.2_3" and lines[-1]["request_id"] == "trace-1.2_3"
    for bad in ("has space", "semi;colon", "x" * 129, ""):
        response = client.get("/v1/me", auth=ada, headers={"X-Request-Id": bad})
        assert response.headers["X-Request-Id"] != bad and len(response.headers["X-Request-Id"]) == 32
        assert lines[-1]["request_id"] == response.headers["X-Request-Id"]


def test_nothing_sensitive_reaches_the_log(
    client: ContractClient, ada: Persona, lines: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    client.get("/v1/users", auth=ada, params={"limit": 1, "cursor": "SECRET-CURSOR-VALUE"})
    client.post("/v1/teams", auth=ada, json={"name": "TopSecretTeamName"})
    text = "\n".join(json.dumps(line) for line in lines) + caplog.text
    assert ada.token not in text
    assert "SECRET-CURSOR-VALUE" not in text
    assert "TopSecretTeamName" not in text
    assert [line["status"] for line in lines] == [400, 201]
    assert lines[0]["path"] == "/v1/users"
