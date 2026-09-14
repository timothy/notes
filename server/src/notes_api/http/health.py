"""Operational probes outside the contract: liveness at ``/healthz`` and readiness at ``/readyz``.

Neither path is part of ``openapi.yaml``; they exist for the container runtime and must not be exposed
through the ingress. Liveness has no dependencies, so a database incident never restarts the fleet.
Readiness pings the database, so a replica whose connections are dead stops receiving traffic until they
recover. Both answers carry ``Cache-Control: no-store`` through the same middleware as every other
response, and a method other than GET is the contract's ``404 not_found`` like any other undeclared route.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger(__name__)


def install_health_routes(app: FastAPI) -> None:
    """Register the probes on the app itself, like the Problem handlers, so the test harness can see them."""
    app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    app.add_api_route("/readyz", readyz, methods=["GET"], include_in_schema=False)


def healthz() -> dict[str, str]:
    return {"status": "ok"}


def readyz(request: Request) -> JSONResponse:
    engine: Engine = request.app.state.engine
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # The class is enough to diagnose; the message could carry the connection string.
        log.warning("readiness: database unavailable (%s)", type(exc).__name__)
        return JSONResponse({"status": "unavailable", "checks": {"database": "unavailable"}}, status_code=503)
    return JSONResponse({"status": "ok", "checks": {"database": "ok"}})
