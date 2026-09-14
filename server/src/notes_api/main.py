"""Application factory.

The server exposes the operations in ``openapi.yaml`` under ``/v1`` plus two operational probes outside it
(``/healthz`` and ``/readyz``, see ``notes_api.http.health``) and nothing else: FastAPI's own OpenAPI
document and docs pages are disabled, because the frozen contract is the only contract.
"""

from __future__ import annotations

from fastapi import FastAPI

from notes_api import CONTRACT_VERSION
from notes_api.auth.jwt import TokenVerifier
from notes_api.clock import Clock, SystemClock
from notes_api.config import Settings, load_settings
from notes_api.contract import load_contract
from notes_api.db import make_engine, make_session_factory
from notes_api.http.health import install_health_routes
from notes_api.http.middleware import NoStoreMiddleware
from notes_api.http.problems import install_problem_handlers
from notes_api.http.request_log import RequestLogMiddleware, configure_request_logging
from notes_api.routers import install_routes


def create_app(settings: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    """The server: the base application plus every implemented operation."""
    app = create_base_app(settings, clock)
    install_routes(app)
    return app


def create_base_app(settings: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    """Everything but the contract's operations: state, middleware, error handlers, and the probes.

    The harness tests build on this so they can register stand-in routes at contract paths.
    """
    settings = settings or load_settings()
    app = FastAPI(
        title="Notes API", version=CONTRACT_VERSION, openapi_url=None, docs_url=None, redoc_url=None
    )
    app.state.settings = settings
    app.state.contract = load_contract(settings.contract_path)
    app.state.engine = make_engine(settings.database_url)
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.clock = clock or SystemClock()
    app.state.verifier = TokenVerifier(settings)
    app.add_middleware(NoStoreMiddleware)
    app.add_middleware(RequestLogMiddleware)  # added last, so it is outermost and times everything
    configure_request_logging()
    install_problem_handlers(app)
    install_health_routes(app)
    return app
