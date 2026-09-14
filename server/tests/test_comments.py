"""Acceptance rows "Comments" and "Permission combinations".

"Comments": "Comment authors need current comment permission for edits/deletes; owner may delete others'
comments but cannot edit them. Read-only users can read comments." "Permission combinations", the part
slice 6 left open: "proposal-only comment creation fails".
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from notes_api import uow
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_notes import create, errors, me
from tests.test_shares import share, user

pytestmark = [pytest.mark.acceptance("Comments"), pytest.mark.acceptance("Permission combinations")]

START_TS = "2026-09-13T12:00:00.000000Z"
ETAG = re.compile(r'^"[0-9a-f]{32}"$')
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
COMMENT_BODY = "Please check the error rate after deployment."  # the contract's CreateCommentRequest


def comment(
    client: ContractClient, persona: Persona, note_id: str, body: str = COMMENT_BODY
) -> httpx.Response:
    return client.post(f"/v1/notes/{note_id}/comments", auth=persona, json={"body": body})


def listed(client: ContractClient, persona: Persona, note_id: str, **params: Any) -> httpx.Response:
    return client.get(f"/v1/notes/{note_id}/comments", auth=persona, params=params or None)


def shared_note(client: ContractClient, owner: Persona, recipient: Persona, *permissions: str) -> str:
    """A note by ``owner`` shared directly with ``recipient``."""
    note_id = str(create(client, owner).json()["id"])
    assert share(client, owner, note_id, user(client, recipient), *permissions).status_code == 201
    return note_id


def note_state(client: ContractClient, owner: Persona, note_id: str) -> tuple[str, str]:
    response = client.get(f"/v1/notes/{note_id}", auth=owner)
    return response.headers["ETag"], response.json()["updatedAt"]


# -- list, get, create ----------------------------------------------------------------------------------


def test_a_commenter_adds_a_comment_and_every_reader_of_the_note_reads_it(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id = shared_note(client, ada, ben, "comment")
    share(client, ada, note_id, user(client, cara), "read")
    response = comment(client, ben, note_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert UUID.match(body["id"]) and body["noteId"] == note_id and body["authorId"] == me(client, ben)
    assert body["body"] == COMMENT_BODY and body["createdAt"] == body["updatedAt"] == START_TS
    assert response.headers["Location"] == f"/v1/notes/{note_id}/comments/{body['id']}"
    assert ETAG.match(response.headers["ETag"])
    for persona in (ada, ben, cara):
        page = listed(client, persona, note_id)
        assert page.status_code == 200 and page.json() == {"items": [body], "nextCursor": None}
        single = client.get(f"/v1/notes/{note_id}/comments/{body['id']}", auth=persona)
        assert single.status_code == 200 and single.json() == body
        assert single.headers["ETag"] == response.headers["ETag"]


@pytest.mark.parametrize("granted", [["read"], ["propose_edit"]], ids=["read-only", "proposal-only"])
def test_readers_without_comment_permission_read_but_cannot_add(
    client: ContractClient, ada: Persona, ben: Persona, granted: list[str]
) -> None:
    note_id = shared_note(client, ada, ben, *granted)
    assert listed(client, ben, note_id).json() == {"items": [], "nextCursor": None}
    refused = comment(client, ben, note_id)
    assert refused.status_code == 403 and refused.json()["code"] == "forbidden"
    assert listed(client, ada, note_id).json()["items"] == []
    added = comment(client, ada, note_id)  # owners always may
    assert added.status_code == 201
    seen = client.get(f"/v1/notes/{note_id}/comments/{added.json()['id']}", auth=ben)
    assert seen.status_code == 200 and seen.json()["authorId"] == me(client, ada)


def test_strangers_see_no_comments_at_all(client: ContractClient, ada: Persona, cara: Persona) -> None:
    note_id = create(client, ada).json()["id"]
    created = comment(client, ada, note_id)
    assert created.status_code == 201
    assert listed(client, cara, note_id).status_code == 404
    assert client.get(f"/v1/notes/{note_id}/comments/{created.json()['id']}", auth=cara).status_code == 404
    assert comment(client, cara, note_id).status_code == 404
    assert client.get(f"/v1/notes/{note_id}/comments").status_code == 401
    assert client.post(f"/v1/notes/{note_id}/comments", json={"body": "x"}).status_code == 401


def test_comments_page_oldest_first_with_ties_broken_by_id(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id = shared_note(client, ada, ben, "comment")
    same_instant = sorted(
        comment(client, persona, note_id, body=f"c{i}").json()["id"]
        for i, persona in enumerate((ada, ben, ada))
    )
    clock.advance(timedelta(seconds=1))
    later = comment(client, ben, note_id, body="later").json()["id"]
    first = listed(client, ben, note_id, limit=2)
    assert [item["id"] for item in first.json()["items"]] == same_instant[:2]
    cursor = first.json()["nextCursor"]
    second = listed(client, ben, note_id, limit=2, cursor=cursor)
    assert [item["id"] for item in second.json()["items"]] == [same_instant[2], later]
    assert second.json()["nextCursor"] is None
    # The cursor is bound to the caller, the note, and the limit.
    for reused in (
        listed(client, ada, note_id, limit=2, cursor=cursor),
        listed(client, ben, note_id, limit=3, cursor=cursor),
        listed(client, ben, shared_note(client, ada, ben, "read"), limit=2, cursor=cursor),
    ):
        assert reused.status_code == 400 and reused.json()["code"] == "invalid_cursor"
    assert errors(listed(client, ben, note_id, limit=0)) == [("query", "limit")]


@pytest.mark.parametrize(
    ("body", "pointers"),
    [
        ({}, ["/body"]),
        ({"body": ""}, ["/body"]),
        ({"body": "  \n\t"}, ["/body"]),
        ({"body": "x" * 10001}, ["/body"]),
        ({"body": "a\x00b"}, ["/body"]),
        ({"body": 5}, ["/body"]),
        ({"body": "ok", "authorId": "x"}, ["/authorId"]),
    ],
    ids=["missing", "empty", "blank", "too long", "NUL", "not a string", "unknown field"],
)
def test_create_validates_the_body_against_the_contract(
    client: ContractClient, ada: Persona, body: dict[str, Any], pointers: list[str]
) -> None:
    note_id = create(client, ada).json()["id"]
    response = client.post(f"/v1/notes/{note_id}/comments", auth=ada, json=body)
    assert response.status_code == 422, response.text
    assert [pointer for _, pointer in errors(response)] == pointers


def test_malformed_bodies_are_400_and_other_media_types_415(client: ContractClient, ada: Persona) -> None:
    note_id = create(client, ada).json()["id"]
    url = f"/v1/notes/{note_id}/comments"
    assert (
        client.post(url, auth=ada, content="{", headers={"Content-Type": "application/json"}).status_code
        == 400
    )
    assert client.post(url, auth=ada).status_code == 400
    assert (
        client.post(url, auth=ada, content="hello", headers={"Content-Type": "text/plain"}).status_code == 415
    )
    assert comment(client, ada, note_id, body="x" * 10000).status_code == 201  # the longest allowed body


def test_a_comment_reached_through_another_note_is_404(client: ContractClient, ada: Persona) -> None:
    note_a = create(client, ada, title="A").json()["id"]
    note_b = create(client, ada, title="B").json()["id"]
    on_b = comment(client, ada, note_b).json()["id"]
    assert client.get(f"/v1/notes/{note_a}/comments/{on_b}", auth=ada).status_code == 404
    assert client.get(f"/v1/notes/{note_b}/comments/{on_b}", auth=ada).status_code == 200
    assert listed(client, ada, note_a).json()["items"] == []
    assert client.get(f"/v1/notes/{uuid.uuid4()}/comments/{on_b}", auth=ada).status_code == 404
    assert errors(client.get(f"/v1/notes/{note_a}/comments/not-a-uuid", auth=ada)) == [("path", "commentId")]


def test_commenting_changes_nothing_about_the_note(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id = shared_note(client, ada, ben, "comment")
    before = note_state(client, ada, note_id)
    clock.advance(timedelta(minutes=1))
    assert comment(client, ben, note_id).status_code == 201
    assert comment(client, ada, note_id).status_code == 201
    assert note_state(client, ada, note_id) == before


@pytest.mark.acceptance("Trash")
def test_a_trashed_notes_comments_are_the_owners_to_read_and_nobodys_to_add(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    created = create(client, ada)
    note_id = created.json()["id"]
    share(client, ada, note_id, user(client, ben), "comment")
    existing = comment(client, ben, note_id).json()
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=created.headers["ETag"]).headers[
        "ETag"
    ]
    page = listed(client, ada, note_id)
    assert page.status_code == 200 and page.json()["items"] == [existing]
    assert client.get(f"/v1/notes/{note_id}/comments/{existing['id']}", auth=ada).status_code == 200
    refused = comment(client, ada, note_id)
    assert refused.status_code == 409 and refused.json()["code"] == "note_not_active"
    assert listed(client, ben, note_id).status_code == 404
    assert client.get(f"/v1/notes/{note_id}/comments/{existing['id']}", auth=ben).status_code == 404
    assert comment(client, ben, note_id).status_code == 404
    assert client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag).status_code == 200
    assert listed(client, ada, note_id).json()["items"] == [existing]  # comments survive the round trip
    assert listed(client, ben, note_id).status_code == 404  # the share did not
    assert comment(client, ada, note_id).status_code == 201


# -- update and delete ----------------------------------------------------------------------------------

UPDATED_BODY = "Please check the error rate and p95 latency after deployment."  # UpdateCommentRequest


def edit(
    client: ContractClient,
    persona: Persona,
    note_id: str,
    comment_id: str,
    etag: str | None,
    body: str = UPDATED_BODY,
    **kwargs: Any,
) -> httpx.Response:
    url = f"/v1/notes/{note_id}/comments/{comment_id}"
    return client.patch(url, auth=persona, if_match=etag, json={"body": body}, **kwargs)


def remove(
    client: ContractClient, persona: Persona, note_id: str, comment_id: str, etag: str | None
) -> httpx.Response:
    return client.delete(f"/v1/notes/{note_id}/comments/{comment_id}", auth=persona, if_match=etag)


def test_the_author_edits_with_the_comment_etag_and_an_identical_body_is_a_no_op(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id = shared_note(client, ada, ben, "comment")
    created = comment(client, ben, note_id)
    comment_id, first_etag = created.json()["id"], created.headers["ETag"]
    clock.advance(timedelta(minutes=1))
    edited = edit(client, ben, note_id, comment_id, first_etag)
    assert edited.status_code == 200, edited.text
    assert edited.json()["body"] == UPDATED_BODY and edited.json()["authorId"] == me(client, ben)
    assert (
        edited.json()["createdAt"] == START_TS and edited.json()["updatedAt"] == "2026-09-13T12:01:00.000000Z"
    )
    second_etag = edited.headers["ETag"]
    assert ETAG.match(second_etag) and second_etag != first_etag
    seen = client.get(f"/v1/notes/{note_id}/comments/{comment_id}", auth=ada)
    assert seen.json() == edited.json() and seen.headers["ETag"] == second_etag
    clock.advance(timedelta(minutes=1))
    same = edit(client, ben, note_id, comment_id, second_etag)
    assert same.status_code == 200 and same.json() == edited.json() and same.headers["ETag"] == second_etag
    stale = edit(client, ben, note_id, comment_id, first_etag, body="stale")
    assert stale.status_code == 412 and stale.json()["code"] == "precondition_failed"
    assert client.get(f"/v1/notes/{note_id}/comments/{comment_id}", auth=ben).json() == edited.json()


def test_owners_delete_any_comment_but_cannot_rewrite_someone_elses(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id = shared_note(client, ada, ben, "comment")
    created = comment(client, ben, note_id)
    comment_id, etag = created.json()["id"], created.headers["ETag"]
    refused = edit(client, ada, note_id, comment_id, etag)
    assert refused.status_code == 403 and refused.json()["code"] == "forbidden"
    assert client.get(f"/v1/notes/{note_id}/comments/{comment_id}", auth=ada).json() == created.json()
    deleted = remove(client, ada, note_id, comment_id, etag)
    assert deleted.status_code == 204 and deleted.content == b"" and "ETag" not in deleted.headers
    assert client.get(f"/v1/notes/{note_id}/comments/{comment_id}", auth=ada).status_code == 404
    assert edit(client, ben, note_id, comment_id, etag).status_code == 404
    assert remove(client, ada, note_id, comment_id, etag).status_code == 404
    assert listed(client, ada, note_id).json()["items"] == []
    own = comment(client, ada, note_id)
    assert edit(client, ada, note_id, own.json()["id"], own.headers["ETag"]).status_code == 200


def test_an_author_who_lost_comment_permission_keeps_reading_only(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id = create(client, ada).json()["id"]
    share_id = share(client, ada, note_id, user(client, ben), "comment").json()["id"]
    created = comment(client, ben, note_id)
    comment_id, etag = created.json()["id"], created.headers["ETag"]
    assert (
        client.patch(
            f"/v1/notes/{note_id}/shares/{share_id}", auth=ada, json={"permissions": ["read"]}
        ).status_code
        == 200
    )
    assert client.get(f"/v1/notes/{note_id}/comments/{comment_id}", auth=ben).status_code == 200
    assert edit(client, ben, note_id, comment_id, etag).status_code == 403
    assert remove(client, ben, note_id, comment_id, etag).status_code == 403
    assert comment(client, ben, note_id).status_code == 403
    assert (
        client.patch(
            f"/v1/notes/{note_id}/shares/{share_id}", auth=ada, json={"permissions": ["comment"]}
        ).status_code
        == 200
    )
    edited = edit(client, ben, note_id, comment_id, etag)
    assert edited.status_code == 200
    assert remove(client, ben, note_id, comment_id, edited.headers["ETag"]).status_code == 204


def test_other_readers_can_neither_edit_nor_delete(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id = shared_note(client, ada, ben, "comment")
    share(client, ada, note_id, user(client, cara), "comment", "propose_edit")
    created = comment(client, ben, note_id)
    comment_id, etag = created.json()["id"], created.headers["ETag"]
    assert client.get(f"/v1/notes/{note_id}/comments/{comment_id}", auth=cara).status_code == 200
    assert edit(client, cara, note_id, comment_id, etag).status_code == 403
    assert remove(client, cara, note_id, comment_id, etag).status_code == 403
    assert edit(client, cara, note_id, comment_id, None).status_code == 403  # 403 precedes 428
    assert client.get(f"/v1/notes/{note_id}/comments/{comment_id}", auth=ben).json() == created.json()


def test_comment_mutations_check_the_body_then_the_precondition(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id = shared_note(client, ada, ben, "comment")
    created = comment(client, ben, note_id)
    comment_id, etag = created.json()["id"], created.headers["ETag"]
    url = f"/v1/notes/{note_id}/comments/{comment_id}"
    assert errors(client.patch(url, auth=ben, if_match=etag, json={})) == [("body", "/body")]
    assert errors(client.patch(url, auth=ben, if_match=etag, json={"body": "x", "id": "y"})) == [
        ("body", "/id")
    ]
    assert errors(client.patch(url, auth=ben, json={})) == [("body", "/body")]  # body shape before 428
    assert (
        client.patch(
            url, auth=ben, if_match=etag, content="x", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert edit(client, ben, note_id, comment_id, None).status_code == 428
    assert remove(client, ben, note_id, comment_id, None).status_code == 428
    for bad in ('W/"x"', "*", '"a", "b"', "x"):
        response = edit(client, ben, note_id, comment_id, bad)
        assert response.status_code == 400 and errors(response) == [("header", "If-Match")]
        assert remove(client, ben, note_id, comment_id, bad).status_code == 400
    stale = '"0123456789abcdef0123456789abcdef"'
    assert edit(client, ben, note_id, comment_id, stale).status_code == 412
    assert remove(client, ben, note_id, comment_id, stale).status_code == 412
    assert client.get(url, auth=ben).json() == created.json()
    other = create(client, ada, title="other").json()["id"]
    assert edit(client, ada, other, comment_id, None).status_code == 404  # mis-nested before 428
    assert remove(client, ada, other, comment_id, etag).status_code == 404


def test_comment_mutations_never_move_the_note_etag(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id = shared_note(client, ada, ben, "comment")
    before = note_state(client, ada, note_id)
    created = comment(client, ben, note_id)
    comment_id = created.json()["id"]
    clock.advance(timedelta(minutes=1))
    edited = edit(client, ben, note_id, comment_id, created.headers["ETag"])
    assert edited.status_code == 200
    assert remove(client, ada, note_id, comment_id, edited.headers["ETag"]).status_code == 204
    assert note_state(client, ada, note_id) == before


@pytest.mark.acceptance("Trash")
def test_a_trashed_note_freezes_its_comments(client: ContractClient, ada: Persona) -> None:
    created = create(client, ada)
    note_id = created.json()["id"]
    own = comment(client, ada, note_id)
    comment_id, etag = own.json()["id"], own.headers["ETag"]
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=created.headers["ETag"]).headers[
        "ETag"
    ]
    frozen = edit(client, ada, note_id, comment_id, etag)
    assert frozen.status_code == 409 and frozen.json()["code"] == "note_not_active"
    assert remove(client, ada, note_id, comment_id, etag).status_code == 409
    stale = edit(client, ada, note_id, comment_id, '"0123456789abcdef0123456789abcdef"')
    assert stale.status_code == 412  # the version check still precedes the lifecycle check
    assert client.get(f"/v1/notes/{note_id}/comments/{comment_id}", auth=ada).json() == own.json()
    assert client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag).status_code == 200
    edited = edit(client, ada, note_id, comment_id, etag)
    assert edited.status_code == 200
    assert remove(client, ada, note_id, comment_id, edited.headers["ETag"]).status_code == 204


def test_a_competing_edit_committed_first_makes_the_second_stale(
    client: ContractClient, app: FastAPI, ada: Persona, restore_hooks: None
) -> None:
    note_id = create(client, ada).json()["id"]
    created = comment(client, ada, note_id)
    comment_id, etag = created.json()["id"], created.headers["ETag"]
    competitor = ContractClient(app)
    fired: list[str] = []

    def before_begin(op: str) -> None:
        if op == "update_comment" and not fired:
            fired.append(op)
            assert edit(competitor, ada, note_id, comment_id, etag, body="First").status_code == 200

    uow.hooks.before_begin = before_begin
    second = edit(client, ada, note_id, comment_id, etag, body="Second")
    assert second.status_code == 412 and fired == ["update_comment"]
    assert client.get(f"/v1/notes/{note_id}/comments/{comment_id}", auth=ada).json()["body"] == "First"
