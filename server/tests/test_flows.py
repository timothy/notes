"""The design guide's section 5 walkthroughs, driven with the contract's own example payloads."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from notes_api.contract import Contract
from tests.contract_client import ContractClient
from tests.support import Persona
from tests.test_notes import me


@pytest.fixture(scope="module")
def examples() -> dict[str, Any]:
    document = Contract.load().document
    values: dict[str, Any] = {
        name: example["value"] for name, example in document["components"]["examples"].items()
    }
    return values


def test_create_and_share(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona, cara: Persona
) -> None:
    """Section 5, "Create and share": create, grant proposal-only access, comment once the grant allows it,
    widen it, revoke it. The independent team grant proves that revoking one share leaves other paths
    intact."""
    # 1. The owner sends POST /notes.
    created = client.post("/v1/notes", auth=ada, json=examples["CreateNoteRequest"])
    assert created.status_code == 201
    note_id = created.json()["id"]
    assert created.headers["Location"] == f"/v1/notes/{note_id}"
    assert created.json()["title"] == "Release checklist" and created.json()["tags"] == ["release"]
    etag = created.headers["ETag"]

    # 2. Proposal-only access for Ben, using the example with Ben's real id.
    grant = copy.deepcopy(examples["CreateShareRequest"])
    grant["recipient"]["id"] = me(client, ben)
    share = client.post(f"/v1/notes/{note_id}/shares", auth=ada, json=grant)
    assert share.status_code == 201
    assert share.json()["permissions"] == ["read", "propose_edit"]
    share_id = share.json()["id"]
    assert share.headers["Location"] == f"/v1/notes/{note_id}/shares/{share_id}"
    seen = client.get(f"/v1/notes/{note_id}", auth=ben)
    assert seen.status_code == 200 and seen.json()["effectivePermissions"] == ["read", "propose_edit"]
    # Ben can read existing comments but receives 403 if he tries to add one.
    assert client.get(f"/v1/notes/{note_id}/comments", auth=ben).json() == {"items": [], "nextCursor": None}
    refused = client.post(f"/v1/notes/{note_id}/comments", auth=ben, json=examples["CreateCommentRequest"])
    assert refused.status_code == 403

    # 3. Widening the grant replaces the set and returns the canonical three.
    widened = client.patch(
        f"/v1/notes/{note_id}/shares/{share_id}", auth=ada, json=examples["UpdateShareRequest"]
    )
    assert widened.status_code == 200
    assert widened.json()["permissions"] == ["read", "comment", "propose_edit"]
    assert client.get(f"/v1/notes/{note_id}", auth=ben).json()["effectivePermissions"] == [
        "read",
        "comment",
        "propose_edit",
    ]
    # A now-authorized recipient can POST to the note's /comments collection.
    posted = client.post(f"/v1/notes/{note_id}/comments", auth=ben, json=examples["CreateCommentRequest"])
    assert posted.status_code == 201 and posted.json()["body"] == examples["CreateCommentRequest"]["body"]
    assert posted.headers["Location"] == f"/v1/notes/{note_id}/comments/{posted.json()['id']}"

    # 4. An independent team grant, then revocation of the direct share: the team path continues to apply.
    team_id = client.post("/v1/teams", auth=cara, json={"name": "Reviewers"}).json()["id"]
    client.post(f"/v1/teams/{team_id}/members", auth=cara, json={"userId": me(client, ben)})
    client.post(
        f"/v1/notes/{note_id}/shares",
        auth=ada,
        json={"recipient": {"type": "team", "id": team_id}, "permissions": ["read"]},
    )
    assert client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 204
    assert client.get(f"/v1/notes/{note_id}", auth=ben).json()["effectivePermissions"] == ["read"]

    # Sharing and commenting changed nothing about the note itself.
    unchanged = client.get(f"/v1/notes/{note_id}", auth=ada)
    assert unchanged.headers["ETag"] == etag and unchanged.json()["updatedAt"] == created.json()["updatedAt"]
