"""Run inside the production image: key removal and cursor expiry, using controlled clocks.

Only standard-library tooling and production dependencies are used. All keys are disposable test keys.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from notes_api.auth.jwt import InvalidToken, TokenVerifier
from notes_api.config import Settings
from notes_api.cursors import CursorCodec, Key
from notes_api.dev_issuer import DevIssuer
from notes_api.http.problems import InvalidCursor


def check_rotation() -> None:
    old = DevIssuer(key_id="old")
    new = DevIssuer(key_id="new")
    document = old.jwks
    fetches = 0
    available = True

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonlocal fetches
            fetches += 1
            if not available:
                self.send_error(503)
                return
            raw = json.dumps(document).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        verifier = TokenVerifier(
            Settings(
                database_url="sqlite://",
                oidc_issuer=old.issuer,
                oidc_audience=old.audience,
                oidc_jwks=None,
                oidc_jwks_url=f"http://127.0.0.1:{server.server_port}/jwks",
                cursor_signing_key="01" * 32,
            )
        )
        old_token = old.token("rotation")
        assert verifier.verify(old_token).subject == "rotation"
        assert fetches == 1
        document = new.jwks
        future = time.monotonic() + 301
        with patch("jwt.jwk_set_cache.time.monotonic", return_value=future):
            try:
                verifier.verify(old_token)
            except InvalidToken:
                pass
            else:
                raise AssertionError("removed signing key was still accepted")
            new_token = new.token("rotation")
            assert verifier.verify(new_token).subject == "rotation"
        available = False
        with patch("jwt.jwk_set_cache.time.monotonic", return_value=future + 301):
            try:
                verifier.verify(new_token)
            except InvalidToken:
                pass
            else:
                raise AssertionError("expired JWKS cache survived failed refresh")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class TestClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 9, 15, 0, 0, 0, 123456, tzinfo=UTC)

    def now(self) -> datetime:
        return self.moment


def check_cursors() -> None:
    clock = TestClock()
    codec = CursorCodec(b"x" * 32, clock)
    key = Key(clock.now(), uuid.uuid4())
    token = codec.encode(key, "test-view")
    assert CursorCodec(b"x" * 32, clock).decode(token, "test-view") == key
    clock.moment += timedelta(hours=24, microseconds=-1)
    assert codec.decode(token, "test-view") == key
    clock.moment += timedelta(microseconds=1)
    try:
        codec.decode(token, "test-view")
    except InvalidCursor:
        pass
    else:
        raise AssertionError("cursor survived its exact 24-hour expiry")


if __name__ == "__main__":
    check_rotation()
    check_cursors()
    print("image boundary checks OK: key removal, failed refresh, replica cursors, exact 24-hour expiry")
