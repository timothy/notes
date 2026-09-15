"""Boundary regressions reproduced against the original container during the API audit."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona


@pytest.mark.parametrize(
    ("resource", "payload", "pointer"),
    [
        ("notes", {"title": "\ud800"}, "/title"),
        ("notes", {"title": "x", "body": "\udfff"}, "/body"),
        ("notes", {"title": "x", "tags": ["\ud800"]}, "/tags/0"),
        ("notes", {"title": "x", "\ud800": "bad key"}, ""),
        ("notes", {"title": {"\ud800": "bad nested key"}}, ""),
        ("comments", {"body": "\ud800"}, "/body"),
        ("edit-requests", {"proposedContent": {"title": "\ud800", "body": "x"}}, "/proposedContent/title"),
        ("edit-requests", {"proposedContent": {"title": "x", "body": "\ud800"}}, "/proposedContent/body"),
        ("edit-requests", {"explanation": "\ud800"}, "/explanation"),
        ("request-comments", {"body": "\ud800"}, "/body"),
    ],
)
def test_unrepresentable_json_is_422_without_writes(
    app: FastAPI, client: ContractClient, ada: Persona, resource: str, payload: dict[str, Any], pointer: str
) -> None:
    note = client.post("/v1/notes", auth=ada, json={"title": "Original", "body": "Original"})
    note_path = f"/v1/notes/{note.json()['id']}"
    proposal = {
        "baseNoteETag": note.headers["ETag"],
        "proposedContent": {"title": "Proposal", "body": "Original"},
    }
    request = client.post(note_path + "/edit-requests", auth=ada, json=proposal)
    request_path = f"/v1/edit-requests/{request.json()['id']}"
    path = {
        "notes": "/v1/notes",
        "comments": note_path + "/comments",
        "edit-requests": note_path + "/edit-requests",
        "request-comments": request_path + "/comments",
    }[resource]
    body = {**proposal, **payload} if resource == "edit-requests" else payload
    response = TestClient(app, raise_server_exceptions=False).post(
        path, headers={**ada.headers, "Content-Type": "application/json"}, content=json.dumps(body)
    )
    assert response.status_code == 422
    client.check("POST", path, response)
    assert response.json()["errors"] == [
        {"location": "body", "pointer": pointer, "detail": "must not contain lone Unicode surrogates"}
    ]
    assert response.headers["X-Request-Id"]
    assert client.get(note_path, auth=ada).headers["ETag"] == note.headers["ETag"]
    assert len(client.get("/v1/notes", auth=ada).json()["items"]) == 1
    assert len(client.get(note_path + "/edit-requests", auth=ada).json()["items"]) == 1
    assert client.get(note_path + "/comments", auth=ada).json()["items"] == []
    assert client.get(request_path + "/comments", auth=ada).json()["items"] == []


@pytest.mark.parametrize("tags", ["same", "different", "reversed"])
def test_repeated_if_match_headers_are_rejected_without_a_write(
    app: FastAPI, client: ContractClient, ada: Persona, tags: str
) -> None:
    note = client.post("/v1/notes", auth=ada, json={"title": "Original"})
    path = f"/v1/notes/{note.json()['id']}"
    current = note.headers["ETag"]
    values = [current, current if tags == "same" else '"stale"']
    if tags == "reversed":
        values.reverse()
    response = TestClient(app).patch(
        path,
        headers=[*ada.headers.items(), *(("If-Match", value) for value in values)],
        json={"title": "Changed"},
    )
    assert response.status_code == 400
    client.check("PATCH", path, response)
    assert response.json()["errors"][0]["pointer"] == "If-Match"
    assert client.get(path, auth=ada).json() == note.json()


@pytest.mark.parametrize("path", ["/v1/me/", "/v1/notes/", "/healthz/", "/readyz/"])
def test_trailing_slashes_are_not_redirected(app: FastAPI, path: str) -> None:
    response = TestClient(app).get(path, follow_redirects=False)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert "Location" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"


def test_modified_cursor_is_rejected(client: ContractClient, ada: Persona, ben: Persona) -> None:
    client.get("/v1/me", auth=ada)
    client.get("/v1/me", auth=ben)
    cursor = client.get("/v1/users", auth=ada, params={"limit": 1}).json()["nextCursor"]
    parts = cursor.split(".")
    encoded = parts[1] if len(parts) == 3 else cursor
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["k"][0] = 0
    changed = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    forged = f"{parts[0]}.{changed}.{parts[2]}" if len(parts) == 3 else changed
    response = client.get("/v1/users", auth=ada, params={"limit": 1, "cursor": forged})
    assert response.status_code == 400 and response.json()["code"] == "invalid_cursor"


def test_cursor_expires_at_24_hours(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    client.get("/v1/me", auth=ada)
    client.get("/v1/me", auth=ben)
    cursor = client.get("/v1/users", auth=ada, params={"limit": 1}).json()["nextCursor"]
    clock.advance(timedelta(hours=24, microseconds=-1))
    assert client.get("/v1/users", auth=ada, params={"limit": 1, "cursor": cursor}).status_code == 200
    clock.advance(timedelta(microseconds=1))
    response = client.get("/v1/users", auth=ada, params={"limit": 1, "cursor": cursor})
    assert response.status_code == 400 and response.json()["code"] == "invalid_cursor"


@pytest.mark.parametrize("tags", ["same", "different", "reversed"])
def test_every_conditional_operation_rejects_duplicate_fields(
    app: FastAPI, client: ContractClient, ada: Persona, ben: Persona, tags: str
) -> None:
    """Inventory is checked against the contract so new conditional routes join this regression."""
    ben_id = client.get("/v1/me", auth=ben).json()["id"]
    note = client.post("/v1/notes", auth=ada, json={"title": "Original", "body": "Original"})
    np = f"/v1/notes/{note.json()['id']}"
    note = client.post(
        np + "/owners", auth=ada, headers={"If-Match": note.headers["ETag"]}, json={"userId": ben_id}
    )
    request = client.post(
        np + "/edit-requests",
        auth=ada,
        json={
            "baseNoteETag": note.headers["ETag"],
            "proposedContent": {"title": "Proposal", "body": "Original"},
        },
    )
    rp = f"/v1/edit-requests/{request.json()['id']}"
    comment = client.post(np + "/comments", auth=ada, json={"body": "Comment"})
    cp = np + "/comments/" + comment.json()["id"]
    discussion = client.post(rp + "/comments", auth=ada, json={"body": "Discussion"})
    dp = rp + "/comments/" + discussion.json()["id"]
    # The valid payloads reach the precondition stage, before lifecycle/business rules.
    cases: list[tuple[str, str, str, Persona, str, dict[str, Any] | None]] = [
        ("updateNote", "PATCH", np, ada, note.headers["ETag"], {"tags": ["changed"]}),
        ("trashNote", "DELETE", np, ada, note.headers["ETag"], None),
        ("restoreNote", "POST", np + "/restore", ada, note.headers["ETag"], None),
        ("addOwner", "POST", np + "/owners", ada, note.headers["ETag"], {"userId": ben_id}),
        ("removeOwner", "DELETE", np + "/owners/" + ben_id, ada, note.headers["ETag"], None),
        (
            "updateReviewPolicy",
            "PATCH",
            np + "/review-policy",
            ada,
            note.headers["ETag"],
            {"mode": "self_merge", "requiredApprovals": None},
        ),
        ("updateComment", "PATCH", cp, ada, comment.headers["ETag"], {"body": "Changed"}),
        ("deleteComment", "DELETE", cp, ada, comment.headers["ETag"], None),
        ("reviseEditRequest", "PATCH", rp, ada, request.headers["ETag"], {"explanation": "Changed"}),
        ("withdrawEditRequest", "POST", rp + "/withdraw", ada, request.headers["ETag"], None),
        ("rejectEditRequest", "POST", rp + "/reject", ben, request.headers["ETag"], {}),
        (
            "mergeEditRequest",
            "POST",
            rp + "/merge",
            ben,
            request.headers["ETag"],
            {"expectedNoteETag": note.headers["ETag"]},
        ),
        ("approveEditRequest", "POST", rp + "/approve", ben, request.headers["ETag"], None),
        ("revokeEditRequestApproval", "POST", rp + "/revoke-approval", ben, request.headers["ETag"], None),
        ("updateEditRequestComment", "PATCH", dp, ada, discussion.headers["ETag"], {"body": "Changed"}),
        ("deleteEditRequestComment", "DELETE", dp, ada, discussion.headers["ETag"], None),
    ]
    declared = {
        op["operationId"]
        for item in app.state.contract.document["paths"].values()
        for method, op in item.items()
        if method in {"get", "post", "patch", "delete"}
        and any(parameter.get("$ref", "").endswith("/IfMatch") for parameter in op.get("parameters", []))
    }
    assert {case[0] for case in cases} == declared
    raw_client = TestClient(app)
    for operation, method, path, persona, current, body in cases:
        values = [current, current if tags == "same" else '"stale"']
        if tags == "reversed":
            values.reverse()
        response = raw_client.request(
            method,
            path,
            headers=[*persona.headers.items(), *(("If-Match", value) for value in values)],
            json=body,
        )
        assert response.status_code == 400, (operation, response.text)
        client.check(method, path, response)
        assert response.json()["code"] == "malformed_request"
    for path, original in [(np, note), (rp, request), (cp, comment), (dp, discussion)]:
        assert client.get(path, auth=ada).json() == original.json()
        assert client.get(path, auth=ada).headers["ETag"] == original.headers["ETag"]


def test_duplicate_headers_preserve_authorization_precedence(
    app: FastAPI, client: ContractClient, ada: Persona, ben: Persona
) -> None:
    ben_id = client.get("/v1/me", auth=ben).json()["id"]
    note = client.post("/v1/notes", auth=ada, json={"title": "Private"})
    path = "/v1/notes/" + note.json()["id"]
    headers = [("If-Match", note.headers["ETag"])] * 2
    raw_client = TestClient(app)
    assert raw_client.patch(path, headers=headers, json={"title": "Changed"}).status_code == 401
    assert (
        raw_client.patch(
            path, headers=[*ben.headers.items(), *headers], json={"title": "Changed"}
        ).status_code
        == 404
    )
    client.post(
        path + "/shares",
        auth=ada,
        json={"recipient": {"type": "user", "id": ben_id}, "permissions": ["read"]},
    )
    assert (
        raw_client.patch(
            path, headers=[*ben.headers.items(), *headers], json={"title": "Changed"}
        ).status_code
        == 403
    )
