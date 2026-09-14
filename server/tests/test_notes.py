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


# -- update: the ladder ---------------------------------------------------------------------------------


def snapshot(app: FastAPI, note_id: str) -> tuple[Any, ...]:
    """Every stored field of the note and its tags, to prove a failed request wrote nothing."""
    from sqlalchemy import inspect as sa_inspect

    from notes_api.models import Note, NoteTag

    with app.state.session_factory() as session:
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        columns = tuple(getattr(note, column.key) for column in sa_inspect(Note).columns)
        tags = tuple(
            session.execute(
                __import__("sqlalchemy")
                .select(NoteTag.position, NoteTag.tag)
                .where(NoteTag.note_id == note.id)
            ).all()
        )
        return columns, tags


def test_an_effective_patch_advances_the_etag_and_a_no_op_does_not(
    client: ContractClient, ada: Persona, clock: Any
) -> None:
    from datetime import timedelta

    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    clock.advance(timedelta(minutes=1))
    retitled = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"title": "Release checklist (2026)"}
    )
    assert retitled.status_code == 200
    assert retitled.json()["title"] == "Release checklist (2026)"
    assert retitled.json()["body"] == CREATE_NOTE_REQUEST["body"] and retitled.json()["tags"] == ["release"]
    assert retitled.json()["updatedAt"] == "2026-09-13T12:01:00.000000Z"
    assert retitled.json()["createdAt"] == START_TS
    assert ETAG.match(retitled.headers["ETag"]) and retitled.headers["ETag"] != etag
    etag = retitled.headers["ETag"]
    clock.advance(timedelta(minutes=1))
    same = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"title": "Release checklist (2026)"}
    )
    assert same.status_code == 200 and same.headers["ETag"] == etag
    assert same.json() == retitled.json()
    rebodied = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"body": "new body", "tags": ["a", "b"]}
    )
    assert rebodied.status_code == 200 and rebodied.headers["ETag"] != etag
    assert rebodied.json()["body"] == "new body" and rebodied.json()["tags"] == ["a", "b"]
    assert rebodied.json()["updatedAt"] == "2026-09-13T12:02:00.000000Z"
    assert client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"] == rebodied.headers["ETag"]


def test_tags_are_replaced_wholesale_in_the_supplied_order(client: ContractClient, ada: Persona) -> None:
    created = create(client, ada, tags=["a", "b", "c"])
    note_id, etag = created.json()["id"], created.headers["ETag"]
    for tags in (["c", "a", "b"], ["b"], ["b", "x", "a"], [], ["z"]):
        response = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"tags": tags})
        assert response.status_code == 200, response.text
        assert response.json()["tags"] == tags
        etag = response.headers["ETag"]
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json()["tags"] == ["z"]


def test_update_validates_the_body_before_anything_else(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    empty = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={})
    assert empty.status_code == 422 and errors(empty) == [("body", "")]
    assert errors(client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"title": None})) == [
        ("body", "/title")
    ]
    assert errors(client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"tags": None})) == [
        ("body", "/tags")
    ]
    assert errors(client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"nope": 1})) == [
        ("body", "/nope")
    ]
    malformed = client.patch(
        f"/v1/notes/{note_id}",
        auth=ada,
        if_match=etag,
        content="{",
        headers={"Content-Type": "application/json"},
    )
    assert malformed.status_code == 400
    assert client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, content="{").status_code == 415
    # Shape precedes visibility and authorization: a stranger's malformed body is 422, not 404.
    assert client.patch(f"/v1/notes/{note_id}", auth=ben, json={}).status_code == 422
    assert client.patch(f"/v1/notes/{uuid.uuid4()}", auth=ada, json={}).status_code == 422


def test_visibility_and_authorization_precede_the_preconditions(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, cara: Persona
) -> None:
    created = create(client, ada)
    note_id = created.json()["id"]
    grant(app, note_id, me(client, ben), comment=True, propose=True)
    body = {"title": "Taken over"}
    # A reader is 403 with or without a valid If-Match; a stranger is 404; neither learns about versions.
    assert client.patch(f"/v1/notes/{note_id}", auth=ben, json=body).status_code == 403
    assert (
        client.patch(
            f"/v1/notes/{note_id}", auth=ben, if_match=created.headers["ETag"], json=body
        ).status_code
        == 403
    )
    assert client.patch(f"/v1/notes/{note_id}", auth=ben, if_match='W/"x"', json=body).status_code == 403
    assert client.patch(f"/v1/notes/{note_id}", auth=cara, json=body).status_code == 404
    assert client.patch(f"/v1/notes/{uuid.uuid4()}", auth=ada, json=body).status_code == 404
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json()["title"] == "Release checklist"


def test_preconditions_are_checked_in_order_and_a_stale_version_writes_nothing(
    client: ContractClient, app: FastAPI, ada: Persona
) -> None:
    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    before = snapshot(app, note_id)
    missing = client.patch(f"/v1/notes/{note_id}", auth=ada, json={"title": "x"})
    assert missing.status_code == 428 and missing.json()["code"] == "precondition_required"
    for malformed in ('W/"x"', "*", '"a", "b"', "note-v1", ""):
        response = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=malformed, json={"title": "x"})
        assert response.status_code == 400, malformed
        assert response.json()["code"] == "malformed_request"
        assert errors(response) == [("header", "If-Match")]
    stale = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match='"0123456789abcdef0123456789abcdef"', json={"title": "x"}
    )
    assert stale.status_code == 412 and stale.json()["code"] == "precondition_failed"
    assert snapshot(app, note_id) == before
    assert client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"] == etag


def test_a_trashed_note_refuses_updates_with_the_current_etag(
    client: ContractClient, app: FastAPI, ada: Persona
) -> None:
    from datetime import timedelta

    from notes_api.models import Note

    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    trashed_at = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with app.state.session_factory() as session, session.begin():
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.deleted_at, note.expires_at = trashed_at, trashed_at + timedelta(hours=720)
    response = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"title": "x"})
    assert response.status_code == 409 and response.json()["code"] == "note_not_active"
    stale = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match='"0123456789abcdef0123456789abcdef"', json={"title": "x"}
    )
    assert stale.status_code == 412  # the version check still precedes the lifecycle check
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json()["deletedAt"] == START_TS


def test_a_competing_update_committed_first_makes_the_second_stale(
    client: ContractClient, app: FastAPI, ada: Persona, restore_hooks: None
) -> None:
    from notes_api import uow

    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    competitor = ContractClient(app)
    fired: list[str] = []

    def before_begin(op: str) -> None:
        if op == "update_note" and not fired:
            fired.append(op)
            assert (
                competitor.patch(
                    f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"title": "First"}
                ).status_code
                == 200
            )

    uow.hooks.before_begin = before_begin
    second = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"title": "Second"})
    assert second.status_code == 412 and fired == ["update_note"]
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json()["title"] == "First"


def test_an_owner_added_between_read_and_write_protects_the_note(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, restore_hooks: None
) -> None:
    from notes_api import uow
    from notes_api.models import NoteOwner

    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    ben_id = uuid.UUID(me(client, ben))
    added: list[str] = []

    def before_begin(op: str) -> None:
        if op == "update_note" and not added:
            added.append(op)
            with app.state.session_factory() as session, session.begin():
                now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
                session.add(NoteOwner(note_id=uuid.UUID(note_id), user_id=ben_id, position=1, added_at=now))

    uow.hooks.before_begin = before_begin
    refused = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"body": "direct"})
    assert refused.status_code == 409 and refused.json()["code"] == "direct_edit_not_allowed"
    assert added == ["update_note"]
    tagged = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"tags": ["ops"]})
    assert tagged.status_code == 200 and tagged.json()["tags"] == ["ops"]
    assert tagged.json()["ownerIds"] == [me(client, ada), str(ben_id)]
    titled = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match=tagged.headers["ETag"], json={"title": "x", "tags": []}
    )
    assert titled.status_code == 409 and client.get(f"/v1/notes/{note_id}", auth=ada).json()["tags"] == [
        "ops"
    ]
    assert (
        client.patch(
            f"/v1/notes/{note_id}", auth=ben, if_match=tagged.headers["ETag"], json={"tags": []}
        ).status_code
        == 200
    )
