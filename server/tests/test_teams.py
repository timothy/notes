"""Acceptance row "Directory and teams", the teams half.

Row text: "External identities provision once under concurrent access; `GET /me` returns the same profile.
Nonmembers cannot list membership. Concurrent removal/demotion cannot leave an existing team without an
admin. Team deletion preserves authored content. `scope=mine` lists only the caller's teams."
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from notes_api.models import Comment, EditRequest, Membership, Note, NoteOwner, Share
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona

pytestmark = pytest.mark.acceptance("Directory and teams")

START_TS = "2026-09-13T12:00:00.000000Z"


def create_team(client: ContractClient, persona: Persona, name: str = "Platform") -> dict[str, Any]:
    response = client.post("/v1/teams", auth=persona, json={"name": name})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def me(client: ContractClient, persona: Persona) -> str:
    return str(client.get("/v1/me", auth=persona).json()["id"])


def errors(response: Any) -> list[tuple[str, str]]:
    return [(e["location"], e["pointer"]) for e in response.json()["errors"]]


def test_creating_a_team_makes_the_creator_its_admin(client: ContractClient, ada: Persona) -> None:
    ada_id = me(client, ada)
    response = client.post("/v1/teams", auth=ada, json={"name": "Platform"})
    assert response.status_code == 201
    team = response.json()
    assert team["name"] == "Platform"
    assert team["createdAt"] == team["updatedAt"] == START_TS
    assert response.headers["Location"] == f"/v1/teams/{team['id']}"
    members = client.get(f"/v1/teams/{team['id']}/members", auth=ada).json()
    assert members == {
        "items": [
            {
                "teamId": team["id"],
                "userId": ada_id,
                "role": "admin",
                "joinedAt": START_TS,
                "updatedAt": START_TS,
            }
        ],
        "nextCursor": None,
    }


def test_creating_a_team_validates_the_body(client: ContractClient, ada: Persona) -> None:
    assert errors(client.post("/v1/teams", auth=ada, json={"name": "   "})) == [("body", "/name")]
    assert errors(client.post("/v1/teams", auth=ada, json={"name": "t" * 101})) == [("body", "/name")]
    assert errors(client.post("/v1/teams", auth=ada, json={})) == [("body", "/name")]
    assert errors(client.post("/v1/teams", auth=ada, json={"name": "ok", "id": "x"})) == [("body", "/id")]
    assert (
        client.post(
            "/v1/teams", auth=ada, content="name=x", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/v1/teams", auth=ada, content="{", headers={"Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert client.post("/v1/teams", auth=ada).status_code == 400
    assert client.post("/v1/teams", json={"name": "Platform"}).status_code == 401
    assert client.post("/v1/teams", auth=ada, json={"name": "t" * 100}).status_code == 201


def test_team_metadata_is_visible_to_everyone_newest_first(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona
) -> None:
    ids = []
    for persona, name in ((ada, "Platform"), (ben, "Design"), (ada, "Platform")):
        ids.append(create_team(client, persona, name)["id"])
        clock.advance(timedelta(seconds=1))
    listing = client.get("/v1/teams", auth=cara)
    assert listing.status_code == 200
    assert [team["id"] for team in listing.json()["items"]] == list(reversed(ids))
    assert listing.json()["nextCursor"] is None
    assert client.get(f"/v1/teams/{ids[0]}", auth=cara).json()["name"] == "Platform"
    assert client.get(f"/v1/teams/{uuid.uuid4()}", auth=cara).status_code == 404
    assert errors(client.get("/v1/teams/not-a-uuid", auth=cara)) == [("path", "teamId")]
    assert client.get("/v1/teams").status_code == 401


def test_scope_mine_lists_only_the_callers_teams(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona
) -> None:
    ben_id = me(client, ben)
    own = create_team(client, ada, "Ada's")["id"]
    clock.advance(timedelta(seconds=1))
    shared = create_team(client, ada, "Shared")["id"]
    clock.advance(timedelta(seconds=1))
    bens = create_team(client, ben, "Ben's")["id"]
    client.post(f"/v1/teams/{shared}/members", auth=ada, json={"userId": ben_id})

    def mine(persona: Persona) -> list[str]:
        return [
            team["id"]
            for team in client.get("/v1/teams", auth=persona, params={"scope": "mine"}).json()["items"]
        ]

    assert mine(ada) == [shared, own]
    assert mine(ben) == [bens, shared]
    assert mine(cara) == []
    assert [
        team["id"] for team in client.get("/v1/teams", auth=cara, params={"scope": "all"}).json()["items"]
    ] == [
        bens,
        shared,
        own,
    ]
    assert errors(client.get("/v1/teams", auth=cara, params={"scope": "theirs"})) == [("query", "scope")]


def test_teams_page_with_cursors_bound_to_the_scope(
    client: ContractClient, clock: FakeClock, ada: Persona
) -> None:
    ids = []
    for name in ("a", "b", "c"):
        ids.append(create_team(client, ada, name)["id"])
        clock.advance(timedelta(seconds=1))
    first = client.get("/v1/teams", auth=ada, params={"limit": 2}).json()
    assert [team["id"] for team in first["items"]] == [ids[2], ids[1]]
    second = client.get("/v1/teams", auth=ada, params={"limit": 2, "cursor": first["nextCursor"]}).json()
    assert [team["id"] for team in second["items"]] == [ids[0]] and second["nextCursor"] is None
    stale = client.get(
        "/v1/teams", auth=ada, params={"limit": 2, "scope": "mine", "cursor": first["nextCursor"]}
    )
    assert stale.status_code == 400 and stale.json()["code"] == "invalid_cursor"
    assert errors(client.get("/v1/teams", auth=ada, params={"limit": 0})) == [("query", "limit")]


def test_only_admins_rename_or_delete_a_team(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona
) -> None:
    team_id = create_team(client, ada)["id"]
    client.post(f"/v1/teams/{team_id}/members", auth=ada, json={"userId": me(client, ben)})
    for persona in (ben, cara):
        assert client.patch(f"/v1/teams/{team_id}", auth=persona, json={"name": "Taken"}).status_code == 403
        assert client.delete(f"/v1/teams/{team_id}", auth=persona).status_code == 403
        # A non-admin's malformed body is still 403: authorization precedes body validation.
        assert client.patch(f"/v1/teams/{team_id}", auth=persona, json={"name": ""}).status_code == 403
    assert client.patch(f"/v1/teams/{uuid.uuid4()}", auth=ada, json={"name": "x"}).status_code == 404
    assert client.delete(f"/v1/teams/{uuid.uuid4()}", auth=ada).status_code == 404
    assert errors(client.patch("/v1/teams/not-a-uuid", auth=ada, json={"name": "x"})) == [("path", "teamId")]
    clock.advance(timedelta(minutes=5))
    renamed = client.patch(f"/v1/teams/{team_id}", auth=ada, json={"name": "Platform Engineering"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Platform Engineering"
    assert renamed.json()["createdAt"] == START_TS
    assert renamed.json()["updatedAt"] == "2026-09-13T12:05:00.000000Z"
    assert client.get(f"/v1/teams/{team_id}", auth=cara).json() == renamed.json()


def test_renaming_validates_the_body_and_an_unchanged_name_changes_nothing(
    client: ContractClient, clock: FakeClock, ada: Persona
) -> None:
    team = create_team(client, ada)
    url = f"/v1/teams/{team['id']}"
    assert errors(client.patch(url, auth=ada, json={"name": " "})) == [("body", "/name")]
    assert errors(client.patch(url, auth=ada, json={})) == [("body", "/name")]
    assert errors(client.patch(url, auth=ada, json={"name": "x", "createdAt": START_TS})) == [
        ("body", "/createdAt")
    ]
    assert client.patch(url, auth=ada, content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.patch(url, auth=ada).status_code == 400
    clock.advance(timedelta(hours=1))
    assert client.patch(url, auth=ada, json={"name": "Platform"}).json() == team


def test_a_deleted_team_is_gone(client: ContractClient, ada: Persona, ben: Persona) -> None:
    team_id = create_team(client, ada)["id"]
    client.post(f"/v1/teams/{team_id}/members", auth=ada, json={"userId": me(client, ben)})
    response = client.delete(f"/v1/teams/{team_id}", auth=ada)
    assert response.status_code == 204 and response.content == b""
    assert client.get(f"/v1/teams/{team_id}", auth=ada).status_code == 404
    assert client.get(f"/v1/teams/{team_id}/members", auth=ada).status_code == 404
    assert client.delete(f"/v1/teams/{team_id}", auth=ada).status_code == 404
    assert client.get("/v1/teams", auth=ben, params={"scope": "mine"}).json()["items"] == []


def count(app: FastAPI, model: type[Any], **where: Any) -> int:
    with app.state.session_factory() as session:
        statement = select(func.count()).select_from(model)
        for column, value in where.items():
            statement = statement.where(getattr(model, column) == value)
        total: int = session.execute(statement).scalar_one()
        return total


def test_deleting_a_team_keeps_authored_content_and_removes_only_its_own_grants(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    """The row says "Team deletion preserves authored content": notes, comments, and edit requests belong to
    people; only the team's memberships and the shares addressed to the team go with it. Seeded at the table
    level until the note slices exist."""
    ada_id, ben_id = uuid.UUID(me(client, ada)), uuid.UUID(me(client, ben))
    team_id = uuid.UUID(create_team(client, ada)["id"])
    client.post(f"/v1/teams/{team_id}/members", auth=ada, json={"userId": str(ben_id)})
    other_team = uuid.UUID(create_team(client, ben, "Other")["id"])
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    note_id, request_id = uuid.uuid4(), uuid.uuid4()
    with app.state.session_factory() as session, session.begin():
        session.add(
            Note(
                id=note_id,
                author_id=ada_id,
                title="Roadmap",
                body="",
                title_fold="roadmap",
                body_fold="",
                review_mode="self_merge",
                review_required_approvals=None,
                created_at=now,
                updated_at=now,
                deleted_at=None,
                expires_at=None,
                version="v1",
            )
        )
        session.flush()  # the note must exist before its children: no ORM relationships order the inserts
        session.add(NoteOwner(note_id=note_id, user_id=ada_id, position=0, added_at=now))
        session.add(
            Comment(
                id=uuid.uuid4(),
                note_id=note_id,
                author_id=ben_id,
                body="Looks good",
                created_at=now,
                updated_at=now,
                version="c1",
            )
        )
        session.add(
            EditRequest(
                id=request_id,
                note_id=note_id,
                proposer_id=ben_id,
                status="open",
                base_title="Roadmap",
                base_body="",
                proposed_title="Roadmap 2027",
                proposed_body="",
                created_at=now,
                updated_at=now,
                version="r1",
            )
        )
        for recipient_type, recipient_id in (("team", team_id), ("team", other_team), ("user", ben_id)):
            session.add(
                Share(
                    id=uuid.uuid4(),
                    note_id=note_id,
                    recipient_type=recipient_type,
                    recipient_id=recipient_id,
                    can_comment=True,
                    can_propose=False,
                    created_at=now,
                    updated_at=now,
                )
            )
    assert count(app, Membership, team_id=team_id) == 2

    assert client.delete(f"/v1/teams/{team_id}", auth=ada).status_code == 204

    assert count(app, Membership, team_id=team_id) == 0
    assert count(app, Share, recipient_type="team", recipient_id=team_id) == 0
    assert count(app, Share, recipient_type="team", recipient_id=other_team) == 1
    assert count(app, Share, recipient_type="user", recipient_id=ben_id) == 1
    assert count(app, Note, id=note_id) == 1
    assert count(app, NoteOwner, note_id=note_id) == 1
    assert count(app, Comment, note_id=note_id) == 1
    assert count(app, EditRequest, id=request_id) == 1
    assert count(app, Membership, team_id=other_team) == 1
