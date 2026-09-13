"""Application factory.

The server exposes exactly the operations in ``openapi.yaml`` under ``/v1`` and nothing else: FastAPI's
own OpenAPI document and docs pages are disabled, because the frozen contract is the only contract.
"""

from __future__ import annotations

from fastapi import FastAPI

from notes_api import CONTRACT_VERSION
from notes_api.config import Settings
from notes_api.contract import Contract
from notes_api.http.middleware import NoStoreMiddleware
from notes_api.http.problems import install_problem_handlers


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(
        title="Notes API", version=CONTRACT_VERSION, openapi_url=None, docs_url=None, redoc_url=None
    )
    app.state.settings = settings
    app.state.contract = Contract.load(settings.contract_path)
    app.add_middleware(NoStoreMiddleware)
    install_problem_handlers(app)
    return app
