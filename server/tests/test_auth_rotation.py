"""A removed signing key must stop authenticating callers when the JWKS cache expires."""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.contract_client import ContractClient
from tests.support import LocalIssuer
from tests.test_auth import expect_401, stub_app


@dataclass
class KeyEndpoint:
    document: dict[str, Any] = field(default_factory=dict)
    available: bool = True
    fetches: int = 0
    url: str = ""


@pytest.fixture
def endpoint(issuer: LocalIssuer) -> Iterator[KeyEndpoint]:
    state = KeyEndpoint(document=issuer.jwks)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.fetches += 1
            if not state.available:
                self.send_error(503)
                return
            raw = json.dumps(state.document).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}/jwks"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("unavailable", [False, True])
def test_cached_key_cannot_outlive_jwks_expiry(
    issuer: LocalIssuer, endpoint: KeyEndpoint, monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    client = ContractClient(stub_app(issuer, oidc_jwks=None, oidc_jwks_url=endpoint.url))
    headers = {"Authorization": f"Bearer {issuer.token('ada')}"}
    assert client.get("/v1/me", headers=headers).status_code == 200
    assert client.get("/v1/me", headers=headers).status_code == 200
    assert endpoint.fetches == 1
    replacement = LocalIssuer(key_id="replacement")
    endpoint.document = replacement.jwks
    endpoint.available = not unavailable
    future = time.monotonic() + 301
    monkeypatch.setattr("jwt.jwk_set_cache.time.monotonic", lambda: future)
    expect_401(client.get("/v1/me", headers=headers), token_present=True)
    assert endpoint.fetches >= 2
    if not unavailable:
        new_headers = {"Authorization": f"Bearer {replacement.token('ada')}"}
        assert client.get("/v1/me", headers=new_headers).status_code == 200
