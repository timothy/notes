"""The dev issuer mints tokens its own JWKS verifies, prints .env lines that compose and a shell both
accept, and runs from the image without the server's configuration."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from jwt import PyJWKSet

from notes_api.dev_issuer import KEY_VARIABLE, DevIssuer, main

LINE = re.compile(r"^([A-Z_]+)='([^']*)'$")
NAMES = ["OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS", KEY_VARIABLE, "CURSOR_SIGNING_KEY"]


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = LINE.fullmatch(line)
        assert match, f"not a single-quoted assignment: {line!r}"
        values[match.group(1)] = match.group(2)
    return values


def verify(token: str, values: dict[str, str]) -> dict[str, object]:
    key = PyJWKSet.from_dict(json.loads(values["OIDC_JWKS"]))["dev-key-1"]
    claims: dict[str, object] = jwt.decode(
        token, key, algorithms=["RS256"], audience=values["OIDC_AUDIENCE"], issuer=values["OIDC_ISSUER"]
    )
    return claims


def test_env_lines_round_trip_into_an_issuer_whose_tokens_the_jwks_verifies() -> None:
    issuer = DevIssuer()
    text = issuer.env_lines()
    values = parse_env(text)
    assert list(values) == NAMES
    assert re.fullmatch(r"[0-9a-f]{64}", values["CURSOR_SIGNING_KEY"])
    assert shlex.split(text) == [f"{name}={value}" for name, value in values.items()]
    restored = DevIssuer.from_env(values)
    token = restored.token("ada", "Ada Okafor")
    claims = verify(token, values)
    assert claims["sub"] == "ada" and claims["name"] == "Ada Okafor"
    assert jwt.get_unverified_header(token) == {"alg": "RS256", "typ": "at+jwt", "kid": "dev-key-1"}
    assert restored.jwks == issuer.jwks


def test_token_claims_and_headers_can_be_overridden() -> None:
    issuer = DevIssuer(issuer="https://dev.example", audience="other-api", key_id="k2")
    issued = datetime.now(UTC).replace(microsecond=0)  # PyJWT checks exp against real time, so stay current
    token = issuer.token(
        "ben",
        audience="third",
        expires_in=timedelta(minutes=5),
        issued_at=issued,
        extra_claims={"nbf": 1, "preferred_username": "ben.o"},
        headers={"typ": "JWT"},
    )
    claims = jwt.decode(
        token, issuer.public_key, algorithms=["RS256"], audience="third", issuer="https://dev.example"
    )
    assert claims["iat"] == int(issued.timestamp()) and claims["exp"] == claims["iat"] + 300
    assert claims["nbf"] == 1 and claims["preferred_username"] == "ben.o" and "name" not in claims
    assert jwt.get_unverified_header(token) == {"alg": "RS256", "typ": "JWT", "kid": "k2"}


def test_from_env_requires_the_private_key() -> None:
    with pytest.raises(LookupError, match=KEY_VARIABLE):
        DevIssuer.from_env({})
    with pytest.raises(LookupError, match=KEY_VARIABLE):
        DevIssuer.from_env({KEY_VARIABLE: ""})


def test_the_env_command_prints_the_lines(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["env", "--issuer", "https://dev.example", "--audience", "aud"]) == 0
    values = parse_env(capsys.readouterr().out)
    assert values["OIDC_ISSUER"] == "https://dev.example" and values["OIDC_AUDIENCE"] == "aud"
    assert json.loads(values["OIDC_JWKS"])["keys"][0]["kid"] == "dev-key-1"


def test_the_token_command_mints_with_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    values = parse_env(DevIssuer().env_lines())
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    assert main(["token", "--sub", "smoke", "--name", "Smoke Test", "--expires-in-hours", "2"]) == 0
    token = capsys.readouterr().out.strip()
    assert token.count(".") == 2
    claims = verify(token, values)
    assert claims["sub"] == "smoke" and claims["name"] == "Smoke Test"
    assert int(str(claims["exp"])) - int(str(claims["iat"])) == 7200


def test_the_token_command_fails_with_one_line_without_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    assert main(["token", "--sub", "ada"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1 and KEY_VARIABLE in captured.err


def test_the_module_runs_without_the_servers_configuration() -> None:
    """The image runs ``env`` before any DATABASE_URL or OIDC variable exists."""
    result = subprocess.run(
        [sys.executable, "-m", "notes_api.dev_issuer", "env"],
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert list(parse_env(result.stdout)) == NAMES
