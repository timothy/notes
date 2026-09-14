"""Acceptance rows "Overlapping grants" and "Isolation".

"Overlapping grants": "Removing a direct grant preserves capabilities from a team; removing membership
preserves a remaining direct grant. Losing the final read path hides the note and the proposer's request."
"Isolation": "Lists/search/inbox and all cursor pages reveal only authorized entries. Swapping a nested
comment/share ID cannot access another note. Third-party note readers cannot inspect another person's
proposal." (Requests and comments arrive in later slices; their parts are asserted there.)
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.contract_client import ContractClient
from tests.support import Persona
from tests.test_notes import create, me
from tests.test_shares import share, team, user

pytestmark = [pytest.mark.acceptance("Overlapping grants"), pytest.mark.acceptance("Isolation")]


def permissions_of(client: ContractClient, persona: Persona, note_id: str) -> list[str] | None:
    response = client.get(f"/v1/notes/{note_id}", auth=persona)
    if response.status_code == 404:
        return None
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return list(body["effectivePermissions"])


def listed(client: ContractClient, persona: Persona) -> list[str]:
    return [item["id"] for item in client.get("/v1/notes", auth=persona).json()["items"]]


def test_removing_the_direct_grant_keeps_the_team_capabilities(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    platform = team(client, ada, ben)
    direct = share(client, ada, note_id, user(client, ben), "comment").json()["id"]
    share(client, ada, note_id, platform, "propose_edit")
    assert permissions_of(client, ben, note_id) == ["read", "comment", "propose_edit"]
    assert client.delete(f"/v1/notes/{note_id}/shares/{direct}", auth=ada).status_code == 204
    assert permissions_of(client, ben, note_id) == ["read", "propose_edit"]
    assert listed(client, ben) == [note_id]
    assert client.delete(f"/v1/teams/{platform['id']}/members/{me(client, ben)}", auth=ada).status_code == 204
    assert permissions_of(client, ben, note_id) is None
    assert listed(client, ben) == []


def test_removing_the_membership_keeps_the_direct_grant(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    platform = team(client, ada, ben)
    share(client, ada, note_id, user(client, ben), "comment")
    share(client, ada, note_id, platform, "propose_edit")
    assert client.delete(f"/v1/teams/{platform['id']}/members/{me(client, ben)}", auth=ada).status_code == 204
    assert permissions_of(client, ben, note_id) == ["read", "comment"]
    assert listed(client, ben) == [note_id]


def test_membership_removal_and_team_deletion_affect_only_the_team_path(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    platform = team(client, ada, ben, cara)
    share(client, ada, note_id, platform, "read")
    share(client, ada, note_id, user(client, dan), "read")
    assert client.delete(f"/v1/teams/{platform['id']}/members/{me(client, ben)}", auth=ada).status_code == 204
    assert permissions_of(client, ben, note_id) is None
    assert permissions_of(client, cara, note_id) == ["read"]
    assert client.delete(f"/v1/teams/{platform['id']}", auth=ada).status_code == 204
    assert permissions_of(client, cara, note_id) is None
    assert permissions_of(client, dan, note_id) == ["read"]
    assert permissions_of(client, ada, note_id) == ["read", "comment", "propose_edit"]
    remaining = client.get(f"/v1/notes/{note_id}/shares", auth=ada).json()["items"]
    assert [item["recipient"]["type"] for item in remaining] == ["user"]


def test_team_admins_gain_nothing_beyond_the_share(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    platform = team(client, ben)  # ben is the admin of a team ada may share with
    assert permissions_of(client, ben, note_id) is None
    share(client, ada, note_id, platform, "read")
    assert permissions_of(client, ben, note_id) == ["read"]
    assert (
        client.patch(f"/v1/notes/{note_id}", auth=ben, if_match=etag, json={"title": "x"}).status_code == 403
    )
    assert client.delete(f"/v1/notes/{note_id}", auth=ben, if_match=etag).status_code == 403
    assert client.get(f"/v1/notes/{note_id}/shares", auth=ben).status_code == 403
    assert share(client, ben, note_id, user(client, ben), "comment").status_code == 403
    assert client.post(f"/v1/notes/{note_id}/restore", auth=ben, if_match=etag).status_code == 403
    assert client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"] == etag


def test_lists_and_nested_ids_reveal_only_what_the_caller_may_see(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    mine = create(client, ada, title="mine").json()["id"]
    shared = create(client, ada, title="shared").json()["id"]
    theirs = create(client, cara, title="theirs").json()["id"]
    share_id = share(client, ada, shared, user(client, ben), "read").json()["id"]
    assert listed(client, ben) == [shared]
    assert set(listed(client, ada)) == {shared, mine}  # same createdAt: the order is the id tie-break
    assert listed(client, cara) == [theirs]
    assert client.get("/v1/notes", auth=ben, params={"q": "mine"}).json()["items"] == []
    assert client.get("/v1/notes", auth=ben, params={"q": "shared"}).json()["items"][0]["id"] == shared
    # The share id exists, but not under a note the caller owns, and never under another note.
    assert client.get(f"/v1/notes/{mine}/shares/{share_id}", auth=ada).status_code == 404
    assert client.get(f"/v1/notes/{theirs}/shares/{share_id}", auth=cara).status_code == 404
    assert client.get(f"/v1/notes/{shared}/shares/{share_id}", auth=ben).status_code == 403
    assert client.get(f"/v1/notes/{shared}/shares/{share_id}", auth=cara).status_code == 404


def test_every_caller_sees_only_their_own_rights(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    share(client, ada, note_id, user(client, ben), "comment")
    share(client, ada, note_id, user(client, cara), "propose_edit")
    views = {
        persona.subject: client.get(f"/v1/notes/{note_id}", auth=persona).json()
        for persona in (ada, ben, cara)
    }
    assert (views["ada"]["isOwner"], views["ada"]["effectivePermissions"]) == (
        True,
        ["read", "comment", "propose_edit"],
    )
    assert (views["ben"]["isOwner"], views["ben"]["effectivePermissions"]) == (False, ["read", "comment"])
    assert (views["cara"]["isOwner"], views["cara"]["effectivePermissions"]) == (
        False,
        ["read", "propose_edit"],
    )
    for view in views.values():
        assert view["ownerIds"] == [me(client, ada)]
