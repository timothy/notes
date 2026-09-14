"""Route registration for the contract's operations.

Every operation is registered directly on the application with ``add_route`` (a thin wrapper over
``app.add_api_route`` that adds the ``/v1`` prefix). ``APIRouter`` and ``include_router`` are never used:
FastAPI 0.141 wraps included routers in a private route object, and the test harness
(``tests/contract_client.py``) resolves each response back to its route template by scanning
``app.routes`` for plain ``APIRoute`` entries. Each module here exposes ``install_<name>_routes(app)``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import FastAPI, Query

API_PREFIX = "/v1"

# The contract's pagination parameters. FastAPI's validation error becomes the 422 Problem naming the
# parameter (``location: query``); the cursor's meaning is checked later by ``notes_api.cursors``.
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=4096)]


def add_route(app: FastAPI, method: str, path: str, endpoint: Callable[..., Any]) -> None:
    """Register one operation at ``/v1`` + ``path`` for one HTTP method."""
    app.add_api_route(API_PREFIX + path, endpoint, methods=[method])


def install_routes(app: FastAPI) -> None:
    """Register every implemented operation; the list grows with each slice."""
    from notes_api.routers import users

    users.install_user_routes(app)
