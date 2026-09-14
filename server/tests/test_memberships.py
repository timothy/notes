"""Acceptance row "Directory and teams", the memberships half: nonmembers cannot list membership, and
concurrent removal or demotion cannot leave an existing team without an admin."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from notes_api import uow
from notes_api.models import Membership
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona

pytestmark = pytest.mark.acceptance("Directory and teams")

START_TS = "2026-09-13T12:00:00.000000Z"


def me(client: ContractClient, persona: Persona) -> str:
    return str(client.get("/v1/me", auth=persona).json()["id"])


def errors(response: Any) -> list[tuple[str, str]]:
    return [(e["location"], e["pointer"]) for e in response.json()["errors"]]


@pytest.fixture
def team(client: ContractClient, ada: Persona) -> str:
    """A team whose only admin is ada."""
    response = client.post("/v1/teams", auth=ada, json={"name": "Platform"})
    assert response.status_code == 201
    return str(response.json()["id"])


def add(
    client: ContractClient, team_id: str, admin: Persona, persona: Persona, role: str | None = None
) -> str:
    body: dict[str, Any] = {"userId": me(client, persona)}
    if role is not None:
        body["role"] = role
    response = client.post(f"/v1/teams/{team_id}/members", auth=admin, json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["userId"])


def roles(client: ContractClient, team_id: str, persona: Persona) -> dict[str, str]:
    page = client.get(f"/v1/teams/{team_id}/members", auth=persona, params={"limit": 100}).json()
    return {item["userId"]: item["role"] for item in page["items"]}


# -- listing ------------------------------------------------------------------------------------------


def test_only_members_list_members_and_a_nonmember_gets_403_not_404(
    client: ContractClient, team: str, ada: Persona, ben: Persona, cara: Persona
) -> None:
    add(client, team, ada, ben)
    assert client.get(f"/v1/teams/{team}/members", auth=ben).status_code == 200
    forbidden = client.get(f"/v1/teams/{team}/members", auth=cara)
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "forbidden"
    assert client.get(f"/v1/teams/{uuid.uuid4()}/members", auth=cara).status_code == 404
    assert client.get(f"/v1/teams/{team}/members").status_code == 401


def test_members_are_sorted_by_joined_at_then_user_id_and_page(
    client: ContractClient,
    clock: FakeClock,
    team: str,
    ada: Persona,
    ben: Persona,
    cara: Persona,
    dan: Persona,
) -> None:
    ada_id = me(client, ada)
    clock.advance(timedelta(seconds=1))
    ben_id = add(client, team, ada, ben)
    clock.advance(timedelta(seconds=1))
    later = sorted([add(client, team, ada, cara), add(client, team, ada, dan)])  # the same joinedAt
    first = client.get(f"/v1/teams/{team}/members", auth=ada, params={"limit": 2}).json()
    assert [item["userId"] for item in first["items"]] == [later[1], later[0]]
    second = client.get(
        f"/v1/teams/{team}/members", auth=ada, params={"limit": 2, "cursor": first["nextCursor"]}
    ).json()
    assert [item["userId"] for item in second["items"]] == [ben_id, ada_id]
    assert second["nextCursor"] is None
    other = client.post("/v1/teams", auth=ada, json={"name": "Other"}).json()["id"]
    reused = client.get(
        f"/v1/teams/{other}/members", auth=ada, params={"limit": 2, "cursor": first["nextCursor"]}
    )
    assert reused.status_code == 400 and reused.json()["code"] == "invalid_cursor"


# -- adding -------------------------------------------------------------------------------------------


def test_admins_add_registered_users_with_member_as_the_default_role(
    client: ContractClient, clock: FakeClock, team: str, ada: Persona, ben: Persona, cara: Persona
) -> None:
    clock.advance(timedelta(minutes=1))
    ben_id = me(client, ben)
    response = client.post(f"/v1/teams/{team}/members", auth=ada, json={"userId": ben_id})
    assert response.status_code == 201
    assert response.json() == {
        "teamId": team,
        "userId": ben_id,
        "role": "member",
        "joinedAt": "2026-09-13T12:01:00.000000Z",
        "updatedAt": "2026-09-13T12:01:00.000000Z",
    }
    assert response.headers["Location"] == f"/v1/teams/{team}/members/{ben_id}"
    cara_id = me(client, cara)
    assert (
        client.post(f"/v1/teams/{team}/members", auth=ada, json={"userId": cara_id, "role": "admin"}).json()[
            "role"
        ]
        == "admin"
    )
    assert roles(client, team, ada) == {me(client, ada): "admin", ben_id: "member", cara_id: "admin"}


def test_adding_checks_the_admin_then_the_body_then_the_user_then_duplicates(
    client: ContractClient, team: str, ada: Persona, ben: Persona, cara: Persona
) -> None:
    ben_id = add(client, team, ada, ben)
    url = f"/v1/teams/{team}/members"
    assert client.post(url, auth=ben, json={"userId": me(client, cara)}).status_code == 403
    assert client.post(url, auth=cara, json={"userId": me(client, cara)}).status_code == 403
    assert client.post(url, auth=ben, json={"userId": "not-a-uuid"}).status_code == 422  # shape before 403
    assert (
        client.post(f"/v1/teams/{uuid.uuid4()}/members", auth=ada, json={"userId": ben_id}).status_code == 404
    )
    assert errors(client.post(url, auth=ada, json={})) == [("body", "/userId")]
    assert errors(client.post(url, auth=ada, json={"userId": ben_id, "role": "owner"})) == [("body", "/role")]
    assert errors(client.post(url, auth=ada, json={"userId": "not-a-uuid"})) == [("body", "/userId")]
    unknown = client.post(url, auth=ada, json={"userId": str(uuid.uuid4())})
    assert unknown.status_code == 422 and errors(unknown) == [("body", "/userId")]
    duplicate = client.post(url, auth=ada, json={"userId": ben_id})
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "duplicate_membership"
    assert client.post(url, auth=ada, json={"userId": me(client, ada)}).status_code == 409
    assert client.post(url, auth=ada, content="x", headers={"Content-Type": "text/plain"}).status_code == 415


# -- roles --------------------------------------------------------------------------------------------


def test_admins_change_roles_and_an_unchanged_role_changes_nothing(
    client: ContractClient, clock: FakeClock, team: str, ada: Persona, ben: Persona, cara: Persona
) -> None:
    ben_id = add(client, team, ada, ben)
    url = f"/v1/teams/{team}/members/{ben_id}"
    clock.advance(timedelta(minutes=1))
    promoted = client.patch(url, auth=ada, json={"role": "admin"})
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"
    assert promoted.json()["joinedAt"] == START_TS
    assert promoted.json()["updatedAt"] == "2026-09-13T12:01:00.000000Z"
    clock.advance(timedelta(minutes=1))
    assert client.patch(url, auth=ada, json={"role": "admin"}).json() == promoted.json()
    demoted = client.patch(url, auth=ada, json={"role": "member"})
    assert demoted.json()["role"] == "member" and demoted.json()["updatedAt"] == "2026-09-13T12:02:00.000000Z"
    assert client.patch(url, auth=ben, json={"role": "admin"}).status_code == 403
    assert client.patch(url, auth=cara, json={"role": "admin"}).status_code == 403
    assert errors(client.patch(url, auth=ada, json={})) == [("body", "/role")]
    assert errors(client.patch(url, auth=ada, json={"role": "owner"})) == [("body", "/role")]
    assert (
        client.patch(
            f"/v1/teams/{team}/members/{me(client, cara)}", auth=ada, json={"role": "admin"}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/v1/teams/{uuid.uuid4()}/members/{ben_id}", auth=ada, json={"role": "admin"}
        ).status_code
        == 404
    )
    assert errors(client.patch(f"/v1/teams/{team}/members/nope", auth=ada, json={"role": "admin"})) == [
        ("path", "userId")
    ]


def test_the_last_admin_cannot_be_demoted_or_leave(
    client: ContractClient, team: str, ada: Persona, ben: Persona
) -> None:
    ada_id = me(client, ada)
    demote = client.patch(f"/v1/teams/{team}/members/{ada_id}", auth=ada, json={"role": "member"})
    assert demote.status_code == 409 and demote.json()["code"] == "last_admin"
    leave = client.delete(f"/v1/teams/{team}/members/{ada_id}", auth=ada)
    assert leave.status_code == 409 and leave.json()["code"] == "last_admin"
    ben_id = add(client, team, ada, ben, role="admin")
    assert (
        client.patch(f"/v1/teams/{team}/members/{ada_id}", auth=ada, json={"role": "member"}).status_code
        == 200
    )
    assert (
        client.patch(f"/v1/teams/{team}/members/{ben_id}", auth=ben, json={"role": "member"}).status_code
        == 409
    )
    assert client.delete(f"/v1/teams/{team}/members/{ben_id}", auth=ben).status_code == 409
    assert roles(client, team, ben) == {ada_id: "member", ben_id: "admin"}


# -- removing -----------------------------------------------------------------------------------------


def test_admins_remove_anyone_and_members_remove_only_themselves(
    client: ContractClient, team: str, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    ben_id = add(client, team, ada, ben)
    cara_id = add(client, team, ada, cara)
    dan_id = me(client, dan)
    assert client.delete(f"/v1/teams/{team}/members/{cara_id}", auth=ben).status_code == 403
    assert client.delete(f"/v1/teams/{team}/members/{ben_id}", auth=dan).status_code == 403
    # A nonmember is refused before the target's existence is revealed.
    assert client.delete(f"/v1/teams/{team}/members/{uuid.uuid4()}", auth=dan).status_code == 403
    assert (
        client.patch(f"/v1/teams/{team}/members/{uuid.uuid4()}", auth=dan, json={"role": "admin"}).status_code
        == 403
    )
    assert client.delete(f"/v1/teams/{team}/members/{dan_id}", auth=dan).status_code == 404
    left = client.delete(f"/v1/teams/{team}/members/{ben_id}", auth=ben)
    assert left.status_code == 204 and left.content == b""
    assert client.delete(f"/v1/teams/{team}/members/{cara_id}", auth=ada).status_code == 204
    assert client.delete(f"/v1/teams/{team}/members/{cara_id}", auth=ada).status_code == 404
    assert client.delete(f"/v1/teams/{uuid.uuid4()}/members/{cara_id}", auth=ada).status_code == 404
    assert roles(client, team, ada) == {me(client, ada): "admin"}
    assert client.get("/v1/teams", auth=ben, params={"scope": "mine"}).json()["items"] == []


# -- races --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["demote", "leave"])
def test_a_competitor_who_steps_down_first_makes_the_primary_the_last_admin(
    client: ContractClient,
    app: FastAPI,
    team: str,
    ada: Persona,
    ben: Persona,
    restore_hooks: None,
    action: str,
) -> None:
    """Deterministic race: ben steps down in his own request, committed just before ada's transaction."""
    ada_id, ben_id = me(client, ada), add(client, team, ada, ben, role="admin")
    competitor = ContractClient(app)
    fired: list[str] = []

    def before_begin(op: str) -> None:
        if op in ("update_membership", "remove_membership") and not fired:
            fired.append(op)
            if action == "demote":
                response = competitor.patch(
                    f"/v1/teams/{team}/members/{ben_id}", auth=ben, json={"role": "member"}
                )
            else:
                response = competitor.delete(f"/v1/teams/{team}/members/{ben_id}", auth=ben)
            assert response.status_code in (200, 204), response.text

    uow.hooks.before_begin = before_begin
    if action == "demote":
        response = client.patch(f"/v1/teams/{team}/members/{ada_id}", auth=ada, json={"role": "member"})
    else:
        response = client.delete(f"/v1/teams/{team}/members/{ada_id}", auth=ada)
    assert response.status_code == 409 and response.json()["code"] == "last_admin"
    assert fired == ["update_membership" if action == "demote" else "remove_membership"]
    expected = {ada_id: "admin", ben_id: "member"} if action == "demote" else {ada_id: "admin"}
    assert roles(client, team, ada) == expected


def admin_count(app: FastAPI, team_id: str) -> int:
    with app.state.session_factory() as session:
        statement = select(Membership).where(
            Membership.team_id == uuid.UUID(team_id), Membership.role == "admin"
        )
        return len(session.execute(statement).scalars().all())


@pytest.mark.parametrize("action", ["demote self", "leave", "demote other"])
def test_two_admins_acting_at_once_leave_exactly_one_admin(
    client: ContractClient, app: FastAPI, team: str, ada: Persona, ben: Persona, action: str
) -> None:
    ada_id, ben_id = me(client, ada), add(client, team, ada, ben, role="admin")
    barrier = threading.Barrier(2)
    outcomes: list[int] = []
    lock = threading.Lock()

    def act(persona: Persona, own: str, other: str) -> None:
        own_client = ContractClient(app)
        barrier.wait()
        if action == "demote self":
            response = own_client.patch(
                f"/v1/teams/{team}/members/{own}", auth=persona, json={"role": "member"}
            )
        elif action == "leave":
            response = own_client.delete(f"/v1/teams/{team}/members/{own}", auth=persona)
        else:
            response = own_client.patch(
                f"/v1/teams/{team}/members/{other}", auth=persona, json={"role": "member"}
            )
        with lock:
            outcomes.append(response.status_code)

    threads = [
        threading.Thread(target=act, args=(ada, ada_id, ben_id)),
        threading.Thread(target=act, args=(ben, ben_id, ada_id)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    expected = {"demote self": {200, 409}, "leave": {204, 409}, "demote other": {200, 403}}[action]
    assert sorted(outcomes) == sorted(expected), outcomes
    assert admin_count(app, team) == 1
