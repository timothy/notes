"""Acceptance row "Permission combinations": "Exercise read-only, comment-only input, proposal-only input,
and both. Implied read is returned; proposal-only comment creation fails. Unknown/empty/duplicate permission
entries fail schema validation. `effectivePermissions` and `isOwner` match the caller's actual rights."
(Proposal-only comment creation is asserted in ``tests/test_comments.py``.)"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from notes_api.models import NoteOwner
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_notes import create, me

pytestmark = pytest.mark.acceptance("Permission combinations")

START_TS = "2026-09-13T12:00:00.000000Z"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def share(
    client: ContractClient, owner: Persona, note_id: str, recipient: dict[str, str], *permissions: str
) -> httpx.Response:
    body = {"recipient": recipient, "permissions": list(permissions)}
    return client.post(f"/v1/notes/{note_id}/shares", auth=owner, json=body)


def user(client: ContractClient, persona: Persona) -> dict[str, str]:
    return {"type": "user", "id": me(client, persona)}


def team(client: ContractClient, admin: Persona, *members: Persona) -> dict[str, str]:
    team_id = str(client.post("/v1/teams", auth=admin, json={"name": "Platform"}).json()["id"])
    for member in members:
        client.post(f"/v1/teams/{team_id}/members", auth=admin, json={"userId": me(client, member)})
    return {"type": "team", "id": team_id}


def errors(response: httpx.Response) -> list[tuple[str, str]]:
    return [(e["location"], e["pointer"]) for e in response.json()["errors"]]


def note_state(client: ContractClient, owner: Persona, note_id: str) -> tuple[str, str]:
    response = client.get(f"/v1/notes/{note_id}", auth=owner)
    return response.headers["ETag"], response.json()["updatedAt"]


@pytest.mark.parametrize(
    ("granted", "canonical"),
    [
        (["read"], ["read"]),
        (["comment"], ["read", "comment"]),
        (["propose_edit"], ["read", "propose_edit"]),
        (["comment", "propose_edit"], ["read", "comment", "propose_edit"]),
        (["propose_edit", "comment"], ["read", "comment", "propose_edit"]),
        (["propose_edit", "read"], ["read", "propose_edit"]),
        (["read", "comment", "propose_edit"], ["read", "comment", "propose_edit"]),
    ],
    ids=["read", "comment", "propose", "both", "both reversed", "propose+read", "all three"],
)
def test_implied_read_and_canonical_order_on_create(
    client: ContractClient, ada: Persona, ben: Persona, granted: list[str], canonical: list[str]
) -> None:
    note_id = create(client, ada).json()["id"]
    response = share(client, ada, note_id, user(client, ben), *granted)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["permissions"] == canonical
    assert body["recipient"] == user(client, ben) and body["noteId"] == note_id
    assert UUID.match(body["id"]) and body["createdAt"] == body["updatedAt"] == START_TS
    assert response.headers["Location"] == f"/v1/notes/{note_id}/shares/{body['id']}"
    assert "ETag" not in response.headers
    seen = client.get(f"/v1/notes/{note_id}", auth=ben).json()
    assert seen["isOwner"] is False and seen["effectivePermissions"] == canonical


def test_a_share_grants_updates_and_revokes_access_without_touching_the_note(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    created = create(client, ada)
    note_id = created.json()["id"]
    before = note_state(client, ada, note_id)
    assert client.get(f"/v1/notes/{note_id}", auth=ben).status_code == 404
    share_id = share(client, ada, note_id, user(client, ben), "propose_edit").json()["id"]
    assert client.get(f"/v1/notes/{note_id}", auth=ben).json()["effectivePermissions"] == [
        "read",
        "propose_edit",
    ]
    clock.advance(timedelta(minutes=1))
    updated = client.patch(
        f"/v1/notes/{note_id}/shares/{share_id}", auth=ada, json={"permissions": ["comment"]}
    )
    assert updated.status_code == 200
    assert updated.json()["permissions"] == ["read", "comment"]
    assert (
        updated.json()["updatedAt"] == "2026-09-13T12:01:00.000000Z"
        and updated.json()["createdAt"] == START_TS
    )
    assert client.get(f"/v1/notes/{note_id}", auth=ben).json()["effectivePermissions"] == ["read", "comment"]
    clock.advance(timedelta(minutes=1))
    same = client.patch(
        f"/v1/notes/{note_id}/shares/{share_id}", auth=ada, json={"permissions": ["comment", "read"]}
    )
    assert same.json() == updated.json()  # a no-op keeps updatedAt
    fetched = client.get(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada)
    assert fetched.status_code == 200 and fetched.json() == updated.json()
    revoked = client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada)
    assert revoked.status_code == 204 and revoked.content == b""
    assert client.get(f"/v1/notes/{note_id}", auth=ben).status_code == 404
    assert client.get(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 404
    assert client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 404
    assert note_state(client, ada, note_id) == before


def test_team_shares_reach_current_members_only(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    platform = team(client, ada, ben)
    response = share(client, ada, note_id, platform, "comment")
    assert response.status_code == 201 and response.json()["recipient"] == platform
    assert client.get(f"/v1/notes/{note_id}", auth=ben).json()["effectivePermissions"] == ["read", "comment"]
    assert client.get(f"/v1/notes/{note_id}", auth=cara).status_code == 404
    client.post(f"/v1/teams/{platform['id']}/members", auth=ada, json={"userId": me(client, cara)})
    assert client.get(f"/v1/notes/{note_id}", auth=cara).status_code == 200
    client.delete(f"/v1/teams/{platform['id']}/members/{me(client, ben)}", auth=ada)
    assert client.get(f"/v1/notes/{note_id}", auth=ben).status_code == 404
    # The owner may share with a team they do not belong to.
    other = team(client, ben)
    assert share(client, ada, note_id, other, "read").status_code == 201
    assert client.get(f"/v1/notes/{note_id}", auth=ben).json()["effectivePermissions"] == ["read"]


@pytest.mark.parametrize(
    ("body", "pointers"),
    [
        ({"recipient": {"type": "user", "id": "USER"}, "permissions": []}, ["/permissions"]),
        (
            {"recipient": {"type": "user", "id": "USER"}, "permissions": ["comment", "comment"]},
            ["/permissions"],
        ),
        ({"recipient": {"type": "user", "id": "USER"}, "permissions": ["write"]}, ["/permissions/0"]),
        ({"recipient": {"type": "group", "id": "USER"}, "permissions": ["read"]}, ["/recipient/type"]),
        ({"recipient": {"type": "user", "id": "ben"}, "permissions": ["read"]}, ["/recipient/id"]),
        ({"permissions": ["read"]}, ["/recipient"]),
        ({}, ["/permissions", "/recipient"]),
        ({"recipient": {"type": "user", "id": "USER"}, "permissions": ["read"], "noteId": "x"}, ["/noteId"]),
        (
            {"recipient": {"type": "user", "id": "USER", "name": "Ben"}, "permissions": ["read"]},
            ["/recipient/name"],
        ),
    ],
    ids=[
        "empty permissions",
        "duplicate permission",
        "unknown permission",
        "bad recipient type",
        "recipient id not a uuid",
        "recipient missing",
        "empty body",
        "unknown field",
        "unknown recipient field",
    ],
)
def test_create_validates_the_body_against_the_contract(
    client: ContractClient, ada: Persona, ben: Persona, body: dict[str, Any], pointers: list[str]
) -> None:
    note_id = create(client, ada).json()["id"]
    if body.get("recipient", {}).get("id") == "USER":
        body["recipient"]["id"] = me(client, ben)
    response = client.post(f"/v1/notes/{note_id}/shares", auth=ada, json=body)
    assert response.status_code == 422, response.text
    assert [pointer for _, pointer in errors(response)] == pointers


def test_owners_and_unknown_recipients_are_422_and_repeats_are_409(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    to_owner = share(client, ada, note_id, user(client, ada), "read")
    assert to_owner.status_code == 422
    assert to_owner.json()["errors"] == [
        {"location": "body", "pointer": "/recipient/id", "detail": "recipient is the note owner"}
    ]
    assert (
        to_owner.json()["detail"]
        == "The note owner already has every permission; sharing with them is redundant."
    )
    with app.state.session_factory() as session, session.begin():
        now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        session.add(
            NoteOwner(
                note_id=uuid.UUID(note_id), user_id=uuid.UUID(me(client, cara)), position=1, added_at=now
            )
        )
    assert errors(share(client, ada, note_id, user(client, cara), "read")) == [("body", "/recipient/id")]
    unknown_user = share(client, ada, note_id, {"type": "user", "id": str(uuid.uuid4())}, "read")
    assert (
        unknown_user.status_code == 422 and unknown_user.json()["errors"][0]["detail"] == "unknown recipient"
    )
    unknown_team = share(client, ada, note_id, {"type": "team", "id": str(uuid.uuid4())}, "read")
    assert unknown_team.status_code == 422 and errors(unknown_team) == [("body", "/recipient/id")]
    assert share(client, ada, note_id, user(client, ben), "read").status_code == 201
    duplicate = share(client, ada, note_id, user(client, ben), "comment")
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "duplicate_share"
    platform = team(client, ada)
    assert share(client, ada, note_id, platform, "read").status_code == 201
    assert share(client, ada, note_id, platform, "read").status_code == 409


def test_shares_are_owner_only_and_never_leak_across_notes(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_a = create(client, ada, title="A").json()["id"]
    note_b = create(client, ada, title="B").json()["id"]
    share_a = share(client, ada, note_a, user(client, ben), "comment").json()["id"]
    share_b = share(client, ada, note_b, user(client, cara), "read").json()["id"]
    # ben can read note A but is not an owner: every share operation is 403.
    assert client.get(f"/v1/notes/{note_a}/shares", auth=ben).status_code == 403
    assert client.get(f"/v1/notes/{note_a}/shares/{share_a}", auth=ben).status_code == 403
    assert share(client, ben, note_a, user(client, cara), "read").status_code == 403
    assert (
        client.patch(
            f"/v1/notes/{note_a}/shares/{share_a}", auth=ben, json={"permissions": ["read"]}
        ).status_code
        == 403
    )
    assert client.delete(f"/v1/notes/{note_a}/shares/{share_a}", auth=ben).status_code == 403
    # cara cannot see note A at all.
    assert client.get(f"/v1/notes/{note_a}/shares", auth=cara).status_code == 404
    assert client.get(f"/v1/notes/{note_a}/shares/{share_a}", auth=cara).status_code == 404
    assert share(client, cara, note_a, user(client, ben), "read").status_code == 404
    # A share reached through the wrong note is 404 for the owner too.
    assert client.get(f"/v1/notes/{note_a}/shares/{share_b}", auth=ada).status_code == 404
    assert (
        client.patch(
            f"/v1/notes/{note_a}/shares/{share_b}", auth=ada, json={"permissions": ["read"]}
        ).status_code
        == 404
    )
    assert client.delete(f"/v1/notes/{note_a}/shares/{share_b}", auth=ada).status_code == 404
    assert client.get(f"/v1/notes/{note_b}/shares/{share_b}", auth=ada).status_code == 200
    assert client.get(f"/v1/notes/{note_a}/shares").status_code == 401
    assert errors(client.get(f"/v1/notes/{note_a}/shares/not-a-uuid", auth=ada)) == [("path", "shareId")]


def test_update_replaces_permissions_and_cannot_change_the_recipient(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    share_id = share(client, ada, note_id, user(client, ben), "comment", "propose_edit").json()["id"]
    url = f"/v1/notes/{note_id}/shares/{share_id}"
    assert client.patch(url, auth=ada, json={"permissions": ["read"]}).json()["permissions"] == ["read"]
    assert errors(client.patch(url, auth=ada, json={"permissions": []})) == [("body", "/permissions")]
    assert errors(client.patch(url, auth=ada, json={})) == [("body", "/permissions")]
    moved = client.patch(url, auth=ada, json={"recipient": user(client, cara), "permissions": ["read"]})
    assert errors(moved) == [("body", "/recipient")]
    assert client.patch(url, auth=ada, content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.get(url, auth=ada).json()["recipient"] == user(client, ben)


def test_a_trashed_note_has_no_shares(client: ContractClient, ada: Persona, ben: Persona) -> None:
    created = create(client, ada)
    note_id = created.json()["id"]
    share_id = share(client, ada, note_id, user(client, ben), "read").json()["id"]
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=created.headers["ETag"]).headers[
        "ETag"
    ]
    listed = client.get(f"/v1/notes/{note_id}/shares", auth=ada)
    assert listed.status_code == 200 and listed.json() == {"items": [], "nextCursor": None}
    refused = share(client, ada, note_id, user(client, ben), "read")
    assert refused.status_code == 409 and refused.json()["code"] == "note_not_active"
    assert client.get(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 404
    assert (
        client.patch(
            f"/v1/notes/{note_id}/shares/{share_id}", auth=ada, json={"permissions": ["read"]}
        ).status_code
        == 404
    )
    assert client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 404
    assert client.get(f"/v1/notes/{note_id}/shares", auth=ben).status_code == 404
    client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag)
    assert client.get(f"/v1/notes/{note_id}/shares", auth=ada).json()["items"] == []
    assert share(client, ada, note_id, user(client, ben), "read").status_code == 201


def test_shares_list_newest_first_with_cursors_bound_to_the_note(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    other = create(client, ada, title="other").json()["id"]
    ids = []
    for persona in (ben, cara, dan):
        ids.append(share(client, ada, note_id, user(client, persona), "read").json()["id"])
        clock.advance(timedelta(seconds=1))
    first = client.get(f"/v1/notes/{note_id}/shares", auth=ada, params={"limit": 2})
    assert [item["id"] for item in first.json()["items"]] == [ids[2], ids[1]]
    second = client.get(
        f"/v1/notes/{note_id}/shares", auth=ada, params={"limit": 2, "cursor": first.json()["nextCursor"]}
    )
    assert [item["id"] for item in second.json()["items"]] == [ids[0]] and second.json()["nextCursor"] is None
    reused = client.get(
        f"/v1/notes/{other}/shares", auth=ada, params={"limit": 2, "cursor": first.json()["nextCursor"]}
    )
    assert reused.status_code == 400 and reused.json()["code"] == "invalid_cursor"
    assert errors(client.get(f"/v1/notes/{note_id}/shares", auth=ada, params={"limit": 0})) == [
        ("query", "limit")
    ]
