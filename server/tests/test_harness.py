"""The ContractClient fails any response that violates the contract; the doubles supply time and identity."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from notes_api.config import Settings
from notes_api.contract import Contract
from notes_api.main import create_app
from tests.contract_client import ContractClient, ContractViolation
from tests.support import FakeClock, LocalIssuer

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
NOTE_ID = "44444444-4444-4444-8444-444444444444"
SHARE_ID = "55555555-5555-4555-8555-555555555555"


def example_body(contract: Contract, path: str, method: str, status: str) -> Any:
    """The first documented example body of an operation's response, following example references."""
    response = contract.document["paths"][path][method]["responses"][status]
    if "$ref" in response:
        response = contract.document["components"]["responses"][response["$ref"].split("/")[-1]]
    examples = next(iter(response["content"].values()))["examples"]
    example = next(iter(examples.values()))
    if "$ref" in example:
        example = contract.document["components"]["examples"][example["$ref"].split("/")[-1]]
    return example["value"]


def stub_app(path: str, methods: list[str], endpoint: Callable[..., Any]) -> FastAPI:
    app = create_app(Settings(database_url="sqlite://"))
    app.add_api_route(path, endpoint, methods=methods)
    return app


@pytest.fixture(scope="module")
def contract() -> Contract:
    return Contract.load()


def test_a_valid_declared_response_passes(contract: Contract) -> None:
    body = example_body(contract, "/me", "get", "200")
    client = ContractClient(stub_app("/v1/me", ["GET"], lambda: body))
    assert client.get("/v1/me").json() == body


def test_a_body_that_violates_the_schema_fails(contract: Contract) -> None:
    client = ContractClient(stub_app("/v1/me", ["GET"], lambda: {"nope": 1}))
    with pytest.raises(ContractViolation, match="body"):
        client.get("/v1/me")


def test_an_undeclared_status_fails(contract: Contract) -> None:
    body = example_body(contract, "/me", "get", "200")
    client = ContractClient(stub_app("/v1/me", ["GET"], lambda: JSONResponse(body, status_code=418)))
    with pytest.raises(ContractViolation, match="418"):
        client.get("/v1/me")


def test_a_missing_required_header_fails(contract: Contract) -> None:
    body = example_body(contract, "/notes/{noteId}", "get", "200")

    def without_etag(noteId: str) -> Any:
        return body

    def with_etag(noteId: str) -> Response:
        return JSONResponse(body, headers={"ETag": '"note-v1"'})

    with pytest.raises(ContractViolation, match="ETag"):
        ContractClient(stub_app("/v1/notes/{noteId}", ["GET"], without_etag)).get(f"/v1/notes/{NOTE_ID}")
    assert (
        ContractClient(stub_app("/v1/notes/{noteId}", ["GET"], with_etag))
        .get(f"/v1/notes/{NOTE_ID}")
        .status_code
        == 200
    )


def test_a_malformed_etag_header_fails(contract: Contract) -> None:
    body = example_body(contract, "/notes/{noteId}", "get", "200")

    def weak_etag(noteId: str) -> Response:
        return JSONResponse(body, headers={"ETag": 'W/"note-v1"'})

    with pytest.raises(ContractViolation, match="ETag"):
        ContractClient(stub_app("/v1/notes/{noteId}", ["GET"], weak_etag)).get(f"/v1/notes/{NOTE_ID}")


def test_an_undeclared_media_type_fails() -> None:
    client = ContractClient(
        stub_app("/v1/me", ["GET"], lambda: Response('{"id": 1}', media_type="text/plain"))
    )
    with pytest.raises(ContractViolation, match="media type"):
        client.get("/v1/me")


def test_a_no_content_response_must_have_no_body() -> None:
    path = "/v1/notes/{noteId}/shares/{shareId}"

    def with_body(noteId: str, shareId: str) -> Response:
        return Response("gone", status_code=204)

    def empty(noteId: str, shareId: str) -> Response:
        return Response(status_code=204)

    with pytest.raises(ContractViolation, match="body"):
        ContractClient(stub_app(path, ["DELETE"], with_body)).delete(f"/v1/notes/{NOTE_ID}/shares/{SHARE_ID}")
    assert (
        ContractClient(stub_app(path, ["DELETE"], empty))
        .delete(f"/v1/notes/{NOTE_ID}/shares/{SHARE_ID}")
        .status_code
        == 204
    )


def test_unknown_routes_must_answer_with_the_404_problem() -> None:
    client = ContractClient(create_app(Settings(database_url="sqlite://")))
    assert client.get("/v1/nope").status_code == 404
    extra = ContractClient(stub_app("/v1/extra", ["GET"], lambda: {"ok": True}))
    with pytest.raises(ContractViolation, match="not in the contract"):
        extra.get("/v1/extra")


def test_the_issuer_mints_verifiable_tokens_for_distinct_personas() -> None:
    issuer = LocalIssuer()
    ada, ben = issuer.persona("ada", "Ada Okafor"), issuer.persona("ben", "Ben Ortiz")
    claims = jwt.decode(ada.token, issuer.public_key, algorithms=["RS256"], audience=issuer.audience)
    assert claims["iss"] == issuer.issuer
    assert claims["sub"] == "ada" and claims["name"] == "Ada Okafor"
    assert ada.headers["Authorization"].startswith("Bearer ")
    assert ada.token != ben.token
    assert issuer.jwks["keys"][0]["kid"] == jwt.get_unverified_header(ada.token)["kid"]


def test_the_fake_clock_only_moves_when_told() -> None:
    clock = FakeClock(START)
    assert clock.now() == START
    clock.advance(timedelta(days=30))
    assert clock.now() == START + timedelta(days=30)
    clock.set(START)
    assert clock.now() == START
