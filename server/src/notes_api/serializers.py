"""Response bodies as plain dicts in the contract's shapes.

Responses are built here as dicts and validated against the contract's JSON Schemas by the test harness on
every request, so the serializer is short and the proof of conformance lives in the tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse

from notes_api.models import Membership, Team, User

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def timestamp(moment: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.ffffffZ``: always UTC, always six fractional digits."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def json_response(body: Any, *, status: int = 200, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=dict(headers or {}))


def page(items: list[dict[str, Any]], next_cursor: str | None) -> dict[str, Any]:
    return {"items": items, "nextCursor": next_cursor}


def user(row: User) -> dict[str, Any]:
    return {"id": str(row.id), "displayName": row.display_name, "createdAt": timestamp(row.created_at)}


def team(row: Team) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "createdAt": timestamp(row.created_at),
        "updatedAt": timestamp(row.updated_at),
    }


def membership(row: Membership) -> dict[str, Any]:
    return {
        "teamId": str(row.team_id),
        "userId": str(row.user_id),
        "role": row.role,
        "joinedAt": timestamp(row.joined_at),
        "updatedAt": timestamp(row.updated_at),
    }
