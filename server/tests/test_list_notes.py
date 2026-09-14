"""Acceptance row "Lists": "AND filters, title-or-body search, repeated exact tags, overlapping-share
deduplication, timestamp ties, limit boundaries, cursor misuse, revoked access between pages, and empty
shared+trash/team+trash results. Inbox summaries carry the note's current title." (The inbox is slice 8.)"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, event

from notes_api.models import Share
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_notes import create, grant, me

pytestmark = pytest.mark.acceptance("Lists")


def team_grant(app: FastAPI, note_id: str, team_id: str) -> None:
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with app.state.session_factory() as session, session.begin():
        session.add(
            Share(
                id=uuid.uuid4(),
                note_id=uuid.UUID(note_id),
                recipient_type="team",
                recipient_id=uuid.UUID(team_id),
                can_comment=False,
                can_propose=True,
                created_at=now,
                updated_at=now,
            )
        )


def revoke_all(app: FastAPI, note_id: str) -> None:
    with app.state.session_factory() as session, session.begin():
        session.execute(delete(Share).where(Share.note_id == uuid.UUID(note_id)))


def team_with(client: ContractClient, admin: Persona, *members: Persona) -> str:
    team_id = str(client.post("/v1/teams", auth=admin, json={"name": "Platform"}).json()["id"])
    for member in members:
        client.post(f"/v1/teams/{team_id}/members", auth=admin, json={"userId": me(client, member)})
    return team_id


def listing(client: ContractClient, persona: Persona, **params: Any) -> httpx.Response:
    query = [
        (key, value)
        for key, values in params.items()
        for value in (values if isinstance(values, list) else [values])
    ]
    return client.get("/v1/notes", auth=persona, params=query)


def ids(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def errors(response: httpx.Response) -> list[tuple[str, str]]:
    return [(e["location"], e["pointer"]) for e in response.json()["errors"]]


def test_a_note_shared_along_several_paths_appears_once_without_its_body(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    grant(app, note_id, me(client, ben), comment=True)
    team_grant(app, note_id, team_with(client, ada, ben))
    team_grant(app, note_id, team_with(client, ada, ben))
    response = listing(client, ben)
    assert ids(response) == [note_id]
    item = response.json()["items"][0]
    assert "body" not in item
    assert item["isOwner"] is False and item["effectivePermissions"] == ["read", "comment", "propose_edit"]
    assert item["title"] == "Release checklist" and item["tags"] == ["release"]


def test_scope_partitions_owned_and_shared_notes_newest_first(
    client: ContractClient, app: FastAPI, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona
) -> None:
    a1 = create(client, ada, title="A1").json()["id"]
    clock.advance(timedelta(seconds=1))
    a2 = create(client, ada, title="A2").json()["id"]
    clock.advance(timedelta(seconds=1))
    b1 = create(client, ben, title="B1").json()["id"]
    grant(app, b1, me(client, ada))
    create(client, cara, title="C1")  # nobody shared this with ada
    assert ids(listing(client, ada)) == [b1, a2, a1]
    assert ids(listing(client, ada, scope="all")) == [b1, a2, a1]
    assert ids(listing(client, ada, scope="mine")) == [a2, a1]
    assert ids(listing(client, ada, scope="shared")) == [b1]
    assert ids(listing(client, cara, scope="shared")) == []
    assert listing(client, ada).json()["items"][0]["isOwner"] is False
    assert listing(client, ada).json()["items"][1]["isOwner"] is True


def test_trashed_notes_are_listed_only_for_their_owner_and_only_under_state_trashed(
    client: ContractClient, app: FastAPI, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    created = create(client, ada)
    note_id = created.json()["id"]
    grant(app, note_id, me(client, ben))
    kept = create(client, ada, title="kept").json()["id"]
    assert ids(listing(client, ben)) == [note_id]
    client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=created.headers["ETag"])
    assert ids(listing(client, ada)) == [kept]
    assert ids(listing(client, ada, state="trashed")) == [note_id]
    assert ids(listing(client, ada, state="trashed", scope="mine")) == [note_id]
    assert ids(listing(client, ada, state="trashed", scope="shared")) == []
    assert ids(listing(client, ben)) == []
    assert ids(listing(client, ben, state="trashed")) == []
    clock.advance(timedelta(hours=720))
    assert ids(listing(client, ada, state="trashed")) == []  # expired: gone for everyone, purge or not


def test_ties_page_by_id_and_limits_are_enforced(client: ContractClient, ada: Persona) -> None:
    created = sorted(
        create(client, ada, title=f"n{i}").json()["id"] for i in range(3)
    )  # one FakeClock instant
    first = listing(client, ada, limit=1)
    assert ids(first) == [created[2]]
    second = listing(client, ada, limit=1, cursor=first.json()["nextCursor"])
    assert ids(second) == [created[1]]
    third = listing(client, ada, limit=1, cursor=second.json()["nextCursor"])
    assert ids(third) == [created[0]] and third.json()["nextCursor"] is None
    assert errors(listing(client, ada, limit=0)) == [("query", "limit")]
    assert errors(listing(client, ada, limit=101)) == [("query", "limit")]
    assert len(ids(listing(client, ada, limit=100))) == 3


def test_a_revocation_between_pages_hides_the_note_and_the_cursor_still_works(
    client: ContractClient, app: FastAPI, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    shared = []
    for title in ("one", "two", "three"):
        note_id = create(client, ada, title=title).json()["id"]
        grant(app, note_id, me(client, ben))
        shared.append(note_id)
        clock.advance(timedelta(seconds=1))
    first = listing(client, ben, limit=2)
    assert ids(first) == [shared[2], shared[1]]
    revoke_all(app, shared[0])
    second = listing(client, ben, limit=2, cursor=first.json()["nextCursor"])
    assert ids(second) == [] and second.json()["nextCursor"] is None


def test_cursors_are_bound_to_every_filter(client: ContractClient, clock: FakeClock, ada: Persona) -> None:
    for title in ("one", "two", "three"):
        create(client, ada, title=title, tags=["t"])
        clock.advance(timedelta(seconds=1))
    cursor = listing(client, ada, limit=2, tag="t").json()["nextCursor"]
    assert cursor is not None
    assert listing(client, ada, limit=2, tag="t", cursor=cursor).status_code == 200
    for params in (
        {"limit": 3},
        {"scope": "mine"},
        {"state": "trashed"},
        {"q": "one"},
        {"tag": ["t", "u"]},
        {"teamId": str(uuid.uuid4())},
        {},
    ):
        response = listing(client, ada, cursor=cursor, **{"limit": 2, **params})
        assert response.status_code == 400 and response.json()["code"] == "invalid_cursor", params


def test_search_folds_case_matches_title_or_body_and_treats_wildcards_literally(
    client: ContractClient, ada: Persona
) -> None:
    strasse = create(client, ada, title="Straße", body="").json()["id"]
    upper = create(client, ada, title="Other", body="Notes on STRASSE").json()["id"]
    create(client, ada, title="Unrelated", body="")
    percent = create(client, ada, title="50% off", body="").json()["id"]
    create(client, ada, title="50x off", body="")
    underscore = create(client, ada, title="a_b", body="").json()["id"]
    create(client, ada, title="axb", body="")
    slash = create(client, ada, title="a/b", body="").json()["id"]
    assert set(ids(listing(client, ada, q="strasse"))) == {strasse, upper}
    assert set(ids(listing(client, ada, q="STRASSE"))) == {strasse, upper}
    assert set(ids(listing(client, ada, q="straße"))) == {strasse, upper}
    assert ids(listing(client, ada, q="50%")) == [percent]
    assert ids(listing(client, ada, q="a_b")) == [underscore]
    assert ids(listing(client, ada, q="a/b")) == [slash]
    assert ids(listing(client, ada, q="nothing matches")) == []


def test_tags_filter_exactly_and_combine_with_and(client: ContractClient, ada: Persona) -> None:
    both = create(client, ada, tags=["release", "ops"]).json()["id"]
    release = create(client, ada, tags=["release"]).json()["id"]
    create(client, ada, tags=["Release"])
    assert set(ids(listing(client, ada, tag="release"))) == {both, release}
    assert ids(listing(client, ada, tag=["release", "ops"])) == [both]
    assert ids(listing(client, ada, tag=["ops", "release"])) == [both]
    assert ids(listing(client, ada, tag="Release")) != ids(listing(client, ada, tag="release"))
    assert ids(listing(client, ada, tag="nobody")) == []
    assert ids(listing(client, ada, tag="release", q="checklist")) == [both, release] or set(
        ids(listing(client, ada, tag="release", q="checklist"))
    ) == {both, release}


@pytest.mark.parametrize(
    ("params", "pointer"),
    [
        ({"tag": [f"t{i}" for i in range(11)]}, "tag"),
        ({"tag": ["dup", "dup"]}, "tag"),
        ({"tag": [" lead"]}, "tag"),
        ({"tag": ["a" * 65]}, "tag"),
        ({"tag": [""]}, "tag"),
        ({"q": ""}, "q"),
        ({"q": "x" * 201}, "q"),
        ({"q": ["a", "b"]}, "q"),
        ({"q": "nul\x00"}, "q"),
        ({"tag": ["nul\x00"]}, "tag"),
        ({"teamId": "not-a-uuid"}, "teamId"),
        ({"scope": "theirs"}, "scope"),
        ({"state": "gone"}, "state"),
    ],
    ids=[
        "11 tags",
        "duplicate tag",
        "leading space",
        "tag too long",
        "empty tag",
        "empty q",
        "q too long",
        "repeated q",
        "NUL in q",
        "NUL in tag",
        "bad teamId",
        "bad scope",
        "bad state",
    ],
)
def test_invalid_query_parameters_are_422_naming_the_parameter(
    client: ContractClient, ada: Persona, params: dict[str, Any], pointer: str
) -> None:
    response = listing(client, ada, **params)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"
    assert {p for _, p in errors(response)} == {pointer}
    assert all(location == "query" for location, _ in errors(response))


def test_team_id_selects_notes_shared_to_that_team_and_grants_nothing(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    team_id = team_with(client, ada, ben)
    created = create(client, ada, title="team note")
    note_id = created.json()["id"]
    team_grant(app, note_id, team_id)
    grant(app, note_id, me(client, dan))  # dan reads directly and is not a member
    other = create(client, ada, title="private").json()["id"]
    assert ids(listing(client, ben, teamId=team_id)) == [note_id]
    assert ids(listing(client, dan, teamId=team_id)) == [note_id]
    assert ids(listing(client, ada, teamId=team_id)) == [note_id]
    assert ids(listing(client, cara, teamId=team_id)) == []
    assert ids(listing(client, ada, teamId=str(uuid.uuid4()))) == []
    assert other not in ids(listing(client, ada, teamId=team_id))
    client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=created.headers["ETag"])
    assert ids(listing(client, ada, teamId=team_id, state="trashed")) == []
    assert ids(listing(client, ben, teamId=team_id)) == []


def test_a_page_costs_a_bounded_number_of_statements(
    client: ContractClient, app: FastAPI, ada: Persona
) -> None:
    for i in range(20):
        create(client, ada, title=f"n{i}", tags=["a", "b"])
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", record)
    try:
        assert len(ids(listing(client, ada, limit=20))) == 20
    finally:
        event.remove(app.state.engine, "before_cursor_execute", record)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 8, selects  # the caller lookup, the page, and the five batch loads
