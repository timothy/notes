"""Acceptance row "Directory and teams", the identity half.

Row text: "External identities provision once under concurrent access; `GET /me` returns the same profile.
Nonmembers cannot list membership. Concurrent removal/demotion cannot leave an existing team without an
admin. Team deletion preserves authored content. `scope=mine` lists only the caller's teams." The team
statements are covered by ``test_teams.py`` and ``test_memberships.py``.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from notes_api import uow
from notes_api.auth.provisioning import PROVISION_OP
from notes_api.models import User
from tests.contract_client import ContractClient
from tests.support import FakeClock, LocalIssuer, Persona

pytestmark = pytest.mark.acceptance("Directory and teams")

CHALLENGE = 'Bearer realm="notes-api"'


def user_count(app: FastAPI, subject: str) -> int:
    with app.state.session_factory() as session:
        count: int = session.execute(
            select(func.count()).select_from(User).where(User.subject == subject)
        ).scalar_one()
        return count


def test_me_returns_the_provisioned_profile_and_it_is_stable(
    client: ContractClient, ada: Persona, clock: FakeClock
) -> None:
    first = client.get("/v1/me", auth=ada)
    assert first.status_code == 200
    body = first.json()
    assert body["displayName"] == "Ada Okafor"
    assert body["createdAt"] == "2026-09-13T12:00:00.000000Z"
    assert uuid.UUID(body["id"]).version == 4
    clock.advance(timedelta(hours=1))
    assert client.get("/v1/me", auth=ada).json() == body


def test_personas_are_distinct_users(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    ids = {client.get("/v1/me", auth=persona).json()["id"] for persona in (ada, ben, cara, dan)}
    assert len(ids) == 4


def test_the_display_name_is_taken_at_provisioning_and_never_refreshed(
    client: ContractClient, issuer: LocalIssuer
) -> None:
    old = issuer.persona("renamed", "Old Name")
    new = issuer.persona("renamed", "New Name")
    assert client.get("/v1/me", auth=old).json()["displayName"] == "Old Name"
    assert client.get("/v1/me", auth=new).json()["displayName"] == "Old Name"


def test_a_missing_name_claim_falls_back_to_the_subject(client: ContractClient, issuer: LocalIssuer) -> None:
    anonymous = Persona("anonymous-subject", "", issuer.token("anonymous-subject"))
    assert client.get("/v1/me", auth=anonymous).json()["displayName"] == "user-anonymou"


def test_me_requires_a_token_and_exists_only_for_get(client: ContractClient, ada: Persona) -> None:
    response = client.get("/v1/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == CHALLENGE
    assert client.post("/v1/me", auth=ada).status_code == 404
    assert client.delete("/v1/me", auth=ada).status_code == 404


def test_an_identity_inserted_between_the_miss_and_the_insert_is_reused(
    client: ContractClient, app: FastAPI, issuer: LocalIssuer, restore_hooks: None
) -> None:
    """The deterministic race: a competitor commits the same identity right before the insert transaction."""
    competitor_id = uuid.uuid4()
    calls: list[str] = []

    def competitor(op: str) -> None:
        calls.append(op)
        if op != PROVISION_OP:
            return
        with app.state.session_factory() as session, uow.transaction(session, "competitor"):
            session.add(
                User(
                    id=competitor_id,
                    issuer=issuer.issuer,
                    subject="raced",
                    display_name="Competitor",
                    created_at=datetime(2026, 9, 13, 11, 0, tzinfo=UTC),
                )
            )

    uow.hooks.before_begin = competitor
    response = client.get("/v1/me", auth=issuer.persona("raced", "Primary"))
    assert response.status_code == 200
    assert response.json()["id"] == str(competitor_id)
    assert response.json()["displayName"] == "Competitor"
    assert calls == ["find_user", PROVISION_OP]
    assert user_count(app, "raced") == 1


def test_eight_concurrent_first_requests_provision_one_user(app: FastAPI, issuer: LocalIssuer) -> None:
    persona = issuer.persona("stampede", "Stampede")
    barrier = threading.Barrier(8)
    results: list[tuple[int, str | None]] = []
    lock = threading.Lock()

    def attempt() -> None:
        client = ContractClient(app)
        barrier.wait()
        response = client.get("/v1/me", auth=persona)
        with lock:
            results.append((response.status_code, response.json().get("id")))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [status for status, _ in results] == [200] * 8
    assert len({user_id for _, user_id in results}) == 1
    assert user_count(app, "stampede") == 1


# -- the directory --------------------------------------------------------------------------------------


def provision(client: ContractClient, clock: FakeClock, *personas: Persona, step: timedelta) -> list[str]:
    """Provision each persona one clock step apart; returns their ids in creation order."""
    ids = []
    for persona in personas:
        ids.append(client.get("/v1/me", auth=persona).json()["id"])
        clock.advance(step)
    return ids


def test_the_directory_is_readable_by_every_user_and_sorted_newest_first(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona
) -> None:
    ids = provision(client, clock, ada, ben, cara, step=timedelta(seconds=1))
    response = client.get("/v1/users", auth=cara)
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == list(reversed(ids))
    assert body["nextCursor"] is None
    assert body["items"][0]["displayName"] == "Cara Nakamura"
    assert body["items"][0]["createdAt"] == "2026-09-13T12:00:02.000000Z"


def test_the_directory_pages_with_cursors(
    client: ContractClient,
    clock: FakeClock,
    issuer: LocalIssuer,
    ada: Persona,
    ben: Persona,
    cara: Persona,
    dan: Persona,
) -> None:
    eve = issuer.persona("eve", "Eve Lindqvist")
    ids = provision(client, clock, ada, ben, cara, dan, eve, step=timedelta(minutes=1))
    newest_first = list(reversed(ids))
    first = client.get("/v1/users", auth=ada, params={"limit": 2}).json()
    assert [item["id"] for item in first["items"]] == newest_first[:2]
    second = client.get("/v1/users", auth=ada, params={"limit": 2, "cursor": first["nextCursor"]}).json()
    assert [item["id"] for item in second["items"]] == newest_first[2:4]
    third = client.get("/v1/users", auth=ada, params={"limit": 2, "cursor": second["nextCursor"]}).json()
    assert [item["id"] for item in third["items"]] == newest_first[4:]
    assert third["nextCursor"] is None


def test_ties_on_created_at_page_deterministically_by_id(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    ids = sorted(
        provision(client, FakeClock(datetime(2026, 9, 13, 12, 0, tzinfo=UTC)), ada, ben, step=timedelta(0))
    )
    first = client.get("/v1/users", auth=ada, params={"limit": 1}).json()
    assert [item["id"] for item in first["items"]] == [ids[1]]
    second = client.get("/v1/users", auth=ada, params={"limit": 1, "cursor": first["nextCursor"]}).json()
    assert [item["id"] for item in second["items"]] == [ids[0]]
    assert second["nextCursor"] is None


@pytest.mark.parametrize("limit", ["0", "101", "abc", "-1", "1.5"])
def test_an_out_of_range_limit_is_422_naming_the_parameter(
    client: ContractClient, ada: Persona, limit: str
) -> None:
    response = client.get("/v1/users", auth=ada, params={"limit": limit})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert [(e["location"], e["pointer"]) for e in response.json()["errors"]] == [("query", "limit")]


def test_a_cursor_reused_with_other_settings_or_by_another_caller_is_400(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona
) -> None:
    provision(client, clock, ada, ben, cara, step=timedelta(seconds=1))
    cursor = client.get("/v1/users", auth=ada, params={"limit": 2}).json()["nextCursor"]
    assert cursor is not None
    for params, persona in (
        ({"limit": 3, "cursor": cursor}, ada),
        ({"limit": 2, "cursor": cursor}, ben),
        ({"limit": 2, "cursor": "garbage"}, ada),
        ({"limit": 2, "cursor": cursor[:-4] + "AAAA"}, ada),
    ):
        response = client.get("/v1/users", auth=persona, params=params)
        assert response.status_code == 400, params
        assert response.json()["code"] == "invalid_cursor"
    assert client.get("/v1/users", auth=ada, params={"limit": 2, "cursor": cursor}).status_code == 200


@pytest.mark.parametrize("cursor", ["", "x" * 4097], ids=["empty", "oversize"])
def test_a_cursor_outside_the_parameter_bounds_is_422(
    client: ContractClient, ada: Persona, cursor: str
) -> None:
    response = client.get("/v1/users", auth=ada, params={"cursor": cursor})
    assert response.status_code == 422
    assert [(e["location"], e["pointer"]) for e in response.json()["errors"]] == [("query", "cursor")]


def test_a_user_can_be_fetched_by_id(client: ContractClient, ada: Persona, ben: Persona) -> None:
    me = client.get("/v1/me", auth=ada).json()
    assert client.get(f"/v1/users/{me['id']}", auth=ben).json() == me
    assert client.get(f"/v1/users/{uuid.uuid4()}", auth=ben).status_code == 404
    response = client.get("/v1/users/not-a-uuid", auth=ben)
    assert response.status_code == 422
    assert [(e["location"], e["pointer"]) for e in response.json()["errors"]] == [("path", "userId")]
    assert client.get(f"/v1/users/{me['id']}").status_code == 401
