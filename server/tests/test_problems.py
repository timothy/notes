"""Every error is a Problem, every response says no-store, and bodies are parsed in the contract's order."""

from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.testclient import TestClient

from notes_api.contract import Contract
from notes_api.etags import parse_if_match
from notes_api.http.bodies import parse_body
from notes_api.http.problems import PROBLEMS, Unauthenticated
from notes_api.main import create_app

PROBLEM_JSON = "application/problem+json"
TYPE_PREFIX = "https://notes-api.example.com/problems/"


def _test_routes() -> APIRouter:
    router = APIRouter()

    @router.post("/echo")
    def echo(request: Request) -> dict[str, Any]:
        return {"body": parse_body(request, "CreateNote")}

    @router.post("/optional")
    def optional(request: Request) -> dict[str, Any]:
        return {"body": parse_body(request, "PreviewEditRequest", required=False)}

    @router.patch("/conditional")
    def conditional(request: Request) -> dict[str, str]:
        return {"etag": parse_if_match(request.headers.get("If-Match"))}

    @router.get("/params")
    def params(limit: int = Query(default=25, ge=1, le=100)) -> dict[str, int]:
        return {"limit": limit}

    @router.get("/auth")
    def auth(request: Request) -> None:
        raise Unauthenticated(token_present="Authorization" in request.headers)

    @router.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret detail that must not leak")

    return router


@pytest.fixture(scope="module")
def client() -> TestClient:
    app: FastAPI = create_app()
    app.include_router(_test_routes(), prefix="/v1/_test")
    return TestClient(app, raise_server_exceptions=False)


def _problem(response: Any) -> dict[str, Any]:
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body: dict[str, Any] = response.json()
    assert body["type"] == TYPE_PREFIX + body["code"]
    assert body["status"] == response.status_code
    return body


def test_malformed_json_is_400_malformed_request(client: TestClient) -> None:
    response = client.post(
        "/v1/_test/echo", content=b'{"title":', headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert _problem(response)["code"] == "malformed_request"


def test_missing_required_body_is_400_malformed_request(client: TestClient) -> None:
    response = client.post("/v1/_test/echo", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert _problem(response)["code"] == "malformed_request"


def test_wrong_media_type_is_415(client: TestClient) -> None:
    response = client.post("/v1/_test/echo", content=b"title=x", headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
    assert _problem(response)["code"] == "unsupported_media_type"


def test_body_without_content_type_is_415(client: TestClient) -> None:
    response = client.post("/v1/_test/echo", content=b'{"title": "x"}')
    assert response.status_code == 415


def test_json_with_charset_parameter_is_accepted(client: TestClient) -> None:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    response = client.post("/v1/_test/echo", content=b'{"title": "x"}', headers=headers)
    assert response.status_code == 200
    assert response.json() == {"body": {"title": "x"}}


def test_schema_violation_is_422_with_field_errors(client: TestClient) -> None:
    response = client.post("/v1/_test/echo", json={"title": "x", "color": "red"})
    assert response.status_code == 422
    body = _problem(response)
    assert body["code"] == "validation_failed"
    assert body["errors"] == [{"location": "body", "pointer": "/color", "detail": "unknown field"}]


def test_optional_body_may_be_omitted_or_empty_object(client: TestClient) -> None:
    assert client.post("/v1/_test/optional").json() == {"body": {}}
    assert client.post("/v1/_test/optional", json={}).json() == {"body": {}}
    assert client.post("/v1/_test/optional", json={"nope": 1}).status_code == 422


def test_missing_if_match_is_428(client: TestClient) -> None:
    response = client.patch("/v1/_test/conditional")
    assert response.status_code == 428
    assert _problem(response)["code"] == "precondition_required"


@pytest.mark.parametrize("value", ['W/"x"', "*", '"a", "b"', "x", '""'])
def test_malformed_if_match_is_400_naming_the_header(client: TestClient, value: str) -> None:
    response = client.patch("/v1/_test/conditional", headers={"If-Match": value})
    assert response.status_code == 400
    body = _problem(response)
    assert body["code"] == "malformed_request"
    assert body["errors"][0]["location"] == "header"
    assert body["errors"][0]["pointer"] == "If-Match"


def test_valid_if_match_is_returned_verbatim(client: TestClient) -> None:
    response = client.patch("/v1/_test/conditional", headers={"If-Match": '"note-v1"'})
    assert response.status_code == 200
    assert response.json() == {"etag": '"note-v1"'}


def test_invalid_query_parameter_is_422_naming_the_parameter(client: TestClient) -> None:
    for value in ("0", "101", "abc"):
        response = client.get("/v1/_test/params", params={"limit": value})
        assert response.status_code == 422, value
        body = _problem(response)
        assert body["code"] == "validation_failed"
        assert body["errors"][0]["location"] == "query"
        assert body["errors"][0]["pointer"] == "limit"


def test_unknown_route_and_undeclared_method_are_404_not_found(client: TestClient) -> None:
    for response in (client.get("/v1/nope"), client.delete("/v1/_test/echo")):
        assert response.status_code == 404
        assert _problem(response)["code"] == "not_found"


def test_401_carries_a_bearer_challenge(client: TestClient) -> None:
    anonymous = client.get("/v1/_test/auth")
    assert anonymous.status_code == 401
    assert _problem(anonymous)["code"] == "unauthenticated"
    assert anonymous.headers["WWW-Authenticate"] == 'Bearer realm="notes-api"'
    rejected = client.get("/v1/_test/auth", headers={"Authorization": "Bearer nope"})
    assert rejected.headers["WWW-Authenticate"] == 'Bearer realm="notes-api", error="invalid_token"'


def test_unexpected_exception_is_a_500_problem_that_leaks_nothing(client: TestClient) -> None:
    response = client.get("/v1/_test/boom")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert "secret" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_every_response_carries_cache_control_no_store(client: TestClient) -> None:
    responses = [
        client.post("/v1/_test/echo", json={"title": "x"}),
        client.post("/v1/_test/echo", content=b"{", headers={"Content-Type": "application/json"}),
        client.get("/v1/_test/auth"),
        client.get("/v1/nope"),
        client.delete("/v1/_test/echo"),
        client.get("/v1/_test/params", params={"limit": "0"}),
    ]
    assert [r.headers.get("Cache-Control") for r in responses] == ["no-store"] * len(responses)


def test_problem_table_matches_the_contract_examples() -> None:
    examples = Contract.load().document["components"]["examples"]
    checked = 0
    for example in examples.values():
        value = example.get("value", {})
        if isinstance(value, dict) and "code" in value and value["code"] in PROBLEMS:
            assert (PROBLEMS[value["code"]].status, PROBLEMS[value["code"]].title) == (
                value["status"],
                value["title"],
            ), value["code"]
            checked += 1
    assert checked >= 20
    assert set(PROBLEMS) == set(Contract.load().schemas["ErrorCode"]["enum"])
