"""Acceptance row "Atomicity/errors" for notes: "Invalid final content, stale versions, missing
preconditions, conflicts, and failed authorization leave note/request unchanged and use the documented
Problem Details status and `code`. No private content leaks in errors." Plus create and read."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from notes_api.models import Share
from tests.contract_client import ContractClient
from tests.support import Persona

pytestmark = pytest.mark.acceptance("Atomicity/errors")

START_TS = "2026-09-13T12:00:00.000000Z"
ETAG = re.compile(r'^"[0-9a-f]{32}"$')
CREATE_NOTE_REQUEST = {
    "title": "Release checklist",
    "body": "## Release\n\n- Run tests\n- Deploy\n",
    "tags": ["release"],
}


def create(client: ContractClient, persona: Persona, **overrides: Any) -> httpx.Response:
    body = {**CREATE_NOTE_REQUEST, **overrides}
    return client.post("/v1/notes", auth=persona, json=body)


def me(client: ContractClient, persona: Persona) -> str:
    return str(client.get("/v1/me", auth=persona).json()["id"])


def errors(response: httpx.Response) -> list[tuple[str, str]]:
    return [(e["location"], e["pointer"]) for e in response.json()["errors"]]


def grant(app: FastAPI, note_id: str, user_id: str, *, comment: bool = False, propose: bool = False) -> None:
    """Seed a direct share row until slice 6 brings the share operations."""
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with app.state.session_factory() as session, session.begin():
        session.add(
            Share(
                id=uuid.uuid4(),
                note_id=uuid.UUID(note_id),
                recipient_type="user",
                recipient_id=uuid.UUID(user_id),
                can_comment=comment,
                can_propose=propose,
                created_at=now,
                updated_at=now,
            )
        )


# -- create and read ------------------------------------------------------------------------------------


def test_the_contracts_create_request_round_trips_with_a_stable_etag(
    client: ContractClient, ada: Persona
) -> None:
    ada_id = me(client, ada)
    created = create(client, ada)
    assert created.status_code == 201
    note = created.json()
    assert created.headers["Location"] == f"/v1/notes/{note['id']}"
    assert ETAG.match(created.headers["ETag"])
    assert note["authorId"] == ada_id and note["ownerIds"] == [ada_id]
    assert note["reviewPolicy"] == {"mode": "self_merge", "requiredApprovals": None}
    assert note["title"] == "Release checklist" and note["body"] == CREATE_NOTE_REQUEST["body"]
    assert note["tags"] == ["release"]
    assert note["createdAt"] == note["updatedAt"] == START_TS
    assert note["deletedAt"] is None and note["expiresAt"] is None
    assert note["isOwner"] is True and note["effectivePermissions"] == ["read", "comment", "propose_edit"]
    fetched = client.get(f"/v1/notes/{note['id']}", auth=ada)
    assert fetched.status_code == 200
    assert fetched.json() == note
    assert fetched.headers["ETag"] == created.headers["ETag"]


def test_omitted_body_and_tags_default(client: ContractClient, ada: Persona) -> None:
    response = client.post("/v1/notes", auth=ada, json={"title": "Bare"})
    assert response.status_code == 201
    assert response.json()["body"] == "" and response.json()["tags"] == []


def test_tags_keep_the_order_they_were_supplied_in(client: ContractClient, ada: Persona) -> None:
    response = create(client, ada, tags=["zeta", "alpha", "Release Notes", "release notes"])
    assert response.json()["tags"] == ["zeta", "alpha", "Release Notes", "release notes"]


@pytest.mark.parametrize(
    ("body", "pointers"),
    [
        ({"title": "   "}, ["/title"]),
        ({"title": "t" * 201}, ["/title"]),
        ({"body": "no title"}, ["/title"]),
        ({"title": "x", "tags": [f"t{i}" for i in range(51)]}, ["/tags"]),
        ({"title": "x", "tags": ["release", "release"]}, ["/tags"]),
        ({"title": "x", "tags": ["rel\nease"]}, ["/tags/0"]),
        ({"title": "x", "tags": [" release"]}, ["/tags/0"]),
        ({"title": "x", "tags": ["a" * 65]}, ["/tags/0"]),
        ({"title": "x", "body": None}, ["/body"]),
        ({"title": "x", "id": str(uuid.uuid4())}, ["/id"]),
        ({"title": "x", "authorId": str(uuid.uuid4())}, ["/authorId"]),
        ({"title": "x", "createdAt": START_TS, "isOwner": True}, ["/createdAt", "/isOwner"]),
    ],
    ids=[
        "blank title",
        "title too long",
        "title missing",
        "51 tags",
        "duplicate tags",
        "newline in tag",
        "leading space in tag",
        "tag too long",
        "null body",
        "id supplied",
        "authorId supplied",
        "unknown fields",
    ],
)
def test_create_validates_the_body_against_the_contract(
    client: ContractClient, ada: Persona, body: dict[str, Any], pointers: list[str]
) -> None:
    response = client.post("/v1/notes", auth=ada, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"
    assert [pointer for _, pointer in errors(response)] == pointers
    assert all(location == "body" for location, _ in errors(response))


def test_create_checks_the_request_shape_in_order(client: ContractClient, ada: Persona) -> None:
    assert client.post("/v1/notes", json=CREATE_NOTE_REQUEST).status_code == 401
    assert (
        client.post(
            "/v1/notes", auth=ada, content="title=x", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/v1/notes", auth=ada, content="{", headers={"Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert client.post("/v1/notes", auth=ada).status_code == 400


def test_lengths_count_unicode_code_points(client: ContractClient, ada: Persona) -> None:
    assert create(client, ada, title="é" * 200).status_code == 201
    assert create(client, ada, title="é" * 201).status_code == 422
    assert create(client, ada, tags=["🙂" * 64]).status_code == 201
    assert create(client, ada, tags=["🙂" * 65]).status_code == 422


def test_a_stranger_gets_404_and_a_reader_200(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    hidden = client.get(f"/v1/notes/{note_id}", auth=ben)
    assert hidden.status_code == 404 and hidden.json()["code"] == "not_found"
    grant(app, note_id, me(client, ben), propose=True)
    seen = client.get(f"/v1/notes/{note_id}", auth=ben)
    assert seen.status_code == 200
    assert seen.json()["isOwner"] is False
    assert seen.json()["effectivePermissions"] == ["read", "propose_edit"]
    assert seen.json()["ownerIds"] == [me(client, ada)]
    assert seen.headers["ETag"] == client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"]
    assert client.get(f"/v1/notes/{note_id}", auth=cara).status_code == 404


def test_get_validates_the_path_and_needs_a_token(client: ContractClient, ada: Persona) -> None:
    note_id = create(client, ada).json()["id"]
    assert errors(client.get("/v1/notes/not-a-uuid", auth=ada)) == [("path", "noteId")]
    assert client.get(f"/v1/notes/{uuid.uuid4()}", auth=ada).status_code == 404
    assert client.get(f"/v1/notes/{note_id}").status_code == 401
    assert client.get("/v1/notes/not-a-uuid").status_code == 401
