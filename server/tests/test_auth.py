"""Every way a bearer token can be wrong is the contract's 401 with the bearer challenge; a valid token
passes; and authentication is checked before anything else about the request."""

from __future__ import annotations

import http.server
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from jwt.algorithms import ECAlgorithm

from notes_api.auth.jwt import Identity, InvalidToken, TokenVerifier, display_name_from
from notes_api.config import ConfigurationError
from notes_api.http.deps import INVALID_TOKEN_DETAIL, MISSING_TOKEN_DETAIL, bearer_identity
from notes_api.main import create_app, create_base_app
from notes_api.routers import add_route
from notes_api.serializers import json_response
from tests.contract_client import ContractClient
from tests.support import LocalIssuer, settings_for

CHALLENGE = 'Bearer realm="notes-api"'
INVALID_CHALLENGE = 'Bearer realm="notes-api", error="invalid_token"'
USER_ID = "11111111-1111-4111-8111-111111111111"
TEAM_ID = "33333333-3333-4333-8333-333333333333"


def whoami(identity: Annotated[Identity, Depends(bearer_identity)]) -> JSONResponse:
    return json_response(
        {"id": USER_ID, "displayName": identity.display_name, "createdAt": "2026-09-13T12:00:00Z"}
    )


def members(
    identity: Annotated[Identity, Depends(bearer_identity)],
    teamId: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> JSONResponse:
    return json_response({"items": [], "nextCursor": None})


def stub_app(issuer: LocalIssuer, **overrides: Any) -> FastAPI:
    """The base app (no real operations) with two stand-ins that only authenticate."""
    app = create_base_app(settings_for("sqlite://", issuer, **overrides))
    add_route(app, "GET", "/me", whoami)
    add_route(app, "GET", "/teams/{teamId}/members", members)
    return app


@pytest.fixture(scope="module")
def issuer() -> LocalIssuer:
    return LocalIssuer()


@pytest.fixture(scope="module")
def client(issuer: LocalIssuer) -> ContractClient:
    return ContractClient(stub_app(issuer))


def expect_401(response: httpx.Response, *, token_present: bool) -> None:
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "unauthenticated"
    assert body["detail"] == (INVALID_TOKEN_DETAIL if token_present else MISSING_TOKEN_DETAIL)
    assert response.headers["WWW-Authenticate"] == (INVALID_CHALLENGE if token_present else CHALLENGE)
    assert response.headers["Cache-Control"] == "no-store"


# -- no usable token ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic YWRhOnB3"}, {"Authorization": "Bearer"}, {"Authorization": "Bearer   "}],
    ids=["no header", "basic scheme", "bare bearer", "blank bearer"],
)
def test_a_request_without_a_bearer_token_gets_the_plain_challenge(
    client: ContractClient, headers: dict[str, str]
) -> None:
    expect_401(client.get("/v1/me", headers=headers), token_present=False)


# -- rejected tokens ------------------------------------------------------------------------------------


def alg_none_token(issuer: LocalIssuer) -> str:
    claims = {"iss": issuer.issuer, "sub": "ada", "aud": issuer.audience, "exp": 4_000_000_000}
    return jwt.encode(claims, "", algorithm="none", headers={"kid": issuer.key_id, "typ": "at+jwt"})


def es256_token_with_the_rsa_kid(issuer: LocalIssuer) -> str:
    claims = {"iss": issuer.issuer, "sub": "ada", "aud": issuer.audience, "exp": 4_000_000_000}
    key = ec.generate_private_key(ec.SECP256R1())
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": issuer.key_id, "typ": "at+jwt"})


REJECTIONS = {
    "malformed": lambda issuer: "not.a.jwt",
    "two segments": lambda issuer: issuer.token("ada").rsplit(".", 1)[0],
    "another key, same kid": lambda issuer: LocalIssuer(key_id=issuer.key_id).token("ada"),
    "expired beyond leeway": lambda issuer: issuer.token("ada", expires_in=timedelta(minutes=-2)),
    "not yet valid": lambda issuer: issuer.token(
        "ada", extra_claims={"nbf": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())}
    ),
    "wrong audience": lambda issuer: issuer.token("ada", audience="another-api"),
    "wrong issuer": lambda issuer: issuer.token("ada", extra_claims={"iss": "https://other.example"}),
    "missing issuer": lambda issuer: issuer.token("ada", extra_claims={"iss": None}),
    "alg none": alg_none_token,
    "ES256 signed for the RSA kid": es256_token_with_the_rsa_kid,
    "unknown kid": lambda issuer: issuer.token("ada", headers={"kid": "rotated-away"}),
    "no kid": lambda issuer: issuer.token("ada", headers={"kid": None}),
    "id token typ": lambda issuer: issuer.token("ada", headers={"typ": "id_token"}),
    "missing exp": lambda issuer: issuer.token("ada", extra_claims={"exp": None}),
    "missing sub": lambda issuer: issuer.token("ada", extra_claims={"sub": None}),
    "numeric sub": lambda issuer: issuer.token("ada", extra_claims={"sub": 42}),
}


@pytest.mark.parametrize("make_token", REJECTIONS.values(), ids=list(REJECTIONS))
def test_a_rejected_token_gets_the_invalid_token_challenge(
    client: ContractClient, issuer: LocalIssuer, make_token: Any
) -> None:
    token = make_token(issuer)
    expect_401(client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}), token_present=True)


# -- accepted tokens ------------------------------------------------------------------------------------


@pytest.mark.parametrize("typ", ["at+jwt", "JWT", "AT+JWT", None], ids=["at+jwt", "JWT", "upper", "absent"])
def test_access_token_types_are_accepted(
    client: ContractClient, issuer: LocalIssuer, typ: str | None
) -> None:
    token = issuer.token("ada", "Ada Okafor", headers={"typ": typ})
    response = client.get("/v1/me", headers={"Authorization": f"bearer {token}"})
    assert response.status_code == 200
    assert response.json()["displayName"] == "Ada Okafor"


def test_a_token_within_the_leeway_is_accepted(client: ContractClient, issuer: LocalIssuer) -> None:
    token = issuer.token("ada", expires_in=timedelta(seconds=-30))
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_an_es256_token_verifies_against_an_ec_key() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "ec-1", "use": "sig", "alg": "ES256"})
    settings = settings_for("sqlite://", oidc_jwks=json.dumps({"keys": [jwk]}))
    verifier = TokenVerifier(settings)
    claims = {"iss": settings.oidc_issuer, "sub": "eve", "aud": settings.oidc_audience, "exp": 4_000_000_000}
    token = jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": "ec-1"})
    assert verifier.verify(token) == Identity(settings.oidc_issuer, "eve", "user-eve")


def test_the_identity_carries_the_configured_issuer_and_the_subject(issuer: LocalIssuer) -> None:
    verifier = TokenVerifier(settings_for("sqlite://", issuer))
    identity = verifier.verify(issuer.token("ada", "Ada Okafor"))
    assert identity == Identity(issuer.issuer, "ada", "Ada Okafor")
    with pytest.raises(InvalidToken):
        verifier.verify("garbage")


# -- key sources ----------------------------------------------------------------------------------------


def test_an_unusable_inline_jwks_fails_at_startup(issuer: LocalIssuer) -> None:
    for document in ['{"keys": []}', "not json", '{"nope": 1}']:
        with pytest.raises(ConfigurationError, match="OIDC_JWKS"):
            create_app(settings_for("sqlite://", issuer, oidc_jwks=document))


def test_an_unreachable_jwks_url_is_401_not_500(issuer: LocalIssuer) -> None:
    client = ContractClient(stub_app(issuer, oidc_jwks=None, oidc_jwks_url="http://127.0.0.1:1/jwks"))
    token = issuer.token("ada")
    expect_401(client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}), token_present=True)


@pytest.fixture
def jwks_server(issuer: LocalIssuer) -> Iterator[str]:
    document = json.dumps(issuer.jwks).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/jwks"
    server.shutdown()
    server.server_close()


def test_keys_are_fetched_from_the_jwks_url(issuer: LocalIssuer, jwks_server: str) -> None:
    client = ContractClient(stub_app(issuer, oidc_jwks=None, oidc_jwks_url=jwks_server))
    token = issuer.token("ada", "Ada Okafor")
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    stranger = LocalIssuer(key_id="not-served").token("ada")
    expect_401(client.get("/v1/me", headers={"Authorization": f"Bearer {stranger}"}), token_present=True)


# -- display names --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"sub": "abcdefghijkl", "name": "Ada Okafor", "preferred_username": "ada"}, "Ada Okafor"),
        ({"sub": "abcdefghijkl", "preferred_username": "ada.o"}, "ada.o"),
        ({"sub": "abcdefghijkl"}, "user-abcdefgh"),
        ({"sub": "abc"}, "user-abc"),
        ({"sub": "abcdefghijkl", "name": "   ", "preferred_username": " ben "}, "ben"),
        ({"sub": "abcdefghijkl", "name": 42, "preferred_username": ["x"]}, "user-abcdefgh"),
        ({"sub": "s", "name": "é" * 250}, "é" * 200),
        ({"sub": "s", "name": "🙂" * 201}, "🙂" * 200),
    ],
)
def test_display_name_precedence_and_truncation(claims: dict[str, Any], expected: str) -> None:
    assert display_name_from(claims) == expected


# -- ordering -------------------------------------------------------------------------------------------


def test_authentication_is_checked_before_path_and_query_validation(
    client: ContractClient, issuer: LocalIssuer
) -> None:
    expect_401(client.get("/v1/teams/not-a-uuid/members", params={"limit": 0}), token_present=False)
    token = issuer.token("ada")
    response = client.get(
        "/v1/teams/not-a-uuid/members", params={"limit": 0}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 422
    assert [(e["location"], e["pointer"]) for e in response.json()["errors"]] == [
        ("path", "teamId"),
        ("query", "limit"),
    ]
    assert (
        client.get(f"/v1/teams/{TEAM_ID}/members", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )
