"""Serializers render the contract's shapes: UTC timestamps with microseconds, users, and pages."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from notes_api.contract import Contract
from notes_api.models import User
from notes_api.serializers import json_response, page, timestamp, user


def test_timestamps_are_utc_with_six_fractional_digits() -> None:
    assert timestamp(datetime(2026, 9, 13, 12, 0, 0, 123456, tzinfo=UTC)) == "2026-09-13T12:00:00.123456Z"
    plus_two = timezone(timedelta(hours=2))
    assert timestamp(datetime(2026, 9, 13, 14, 0, tzinfo=plus_two)) == "2026-09-13T12:00:00.000000Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        timestamp(datetime(2026, 9, 13, 12, 0))


def test_user_and_page_bodies_validate_against_the_contract() -> None:
    contract = Contract.load()
    row = User(
        id=uuid.uuid4(),
        issuer="https://issuer.example",
        subject="ada",
        display_name="Ada Okafor",
        created_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    )
    body = user(row)
    assert body == {
        "id": str(row.id),
        "displayName": "Ada Okafor",
        "createdAt": "2026-09-13T12:00:00.000000Z",
    }
    assert contract.validate_instance(contract.schemas["User"], body) == []
    assert contract.validate_instance(contract.schemas["UserPage"], page([body], None)) == []
    assert contract.validate_instance(contract.schemas["UserPage"], page([], "next")) == []


def test_json_response_carries_status_and_headers() -> None:
    response = json_response({"a": 1}, status=201, headers={"Location": "/v1/teams/x"})
    assert response.status_code == 201
    assert response.headers["location"] == "/v1/teams/x"
    assert response.body == b'{"a":1}'
