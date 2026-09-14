"""Acceptance row "Request comments": "Owners and the proposer can comment without note `comment`
permission; other readers get `404`. Authors edit their own, owners delete any, a trashed note freezes them,
closed requests still accept them, and no request comment changes the request ETag.\""""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from notes_api import uow
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_edit_requests import STALE, get_request, proposal, submit, submitted, withdraw
from tests.test_notes import errors, me
from tests.test_owners import add
from tests.test_shares import share, user

pytestmark = pytest.mark.acceptance("Request comments")

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
START_TS = "2026-09-13T12:00:00.000000Z"
ETAG = re.compile(r'^"[0-9a-f]{32}"$')
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
FEEDBACK = "Looks good. Can you also say who declares the incident over?"  # CreateEditRequestCommentRequest
REVISED = "Looks good. Can you also say who declares the incident over, and where?"  # the Update example
PLAIN = {"Content-Type": "text/plain"}


def comment(
    client: ContractClient, persona: Persona, request_id: str, body: str = FEEDBACK
) -> httpx.Response:
    return client.post(f"/v1/edit-requests/{request_id}/comments", auth=persona, json={"body": body})


def listed(client: ContractClient, persona: Persona, request_id: str, **params: Any) -> httpx.Response:
    return client.get(f"/v1/edit-requests/{request_id}/comments", auth=persona, params=params or None)


def get_comment(client: ContractClient, persona: Persona, request_id: str, comment_id: str) -> httpx.Response:
    return client.get(f"/v1/edit-requests/{request_id}/comments/{comment_id}", auth=persona)


def edit(
    client: ContractClient,
    persona: Persona,
    request_id: str,
    comment_id: str,
    etag: str | None,
    body: str = REVISED,
    **kwargs: Any,
) -> httpx.Response:
    url = f"/v1/edit-requests/{request_id}/comments/{comment_id}"
    return client.patch(url, auth=persona, if_match=etag, json={"body": body}, **kwargs)


def remove(
    client: ContractClient, persona: Persona, request_id: str, comment_id: str, etag: str | None
) -> httpx.Response:
    return client.delete(f"/v1/edit-requests/{request_id}/comments/{comment_id}", auth=persona, if_match=etag)


def versions(
    client: ContractClient, owner: Persona, note_id: str, request_id: str
) -> tuple[str, str, str, str]:
    note = client.get(f"/v1/notes/{note_id}", auth=owner)
    request = get_request(client, owner, request_id)
    return (
        note.headers["ETag"],
        note.json()["updatedAt"],
        request.headers["ETag"],
        request.json()["updatedAt"],
    )


def test_inspectors_comment_without_comment_permission_and_read_each_others(
    client: ContractClient,
    clock: FakeClock,
    examples: dict[str, Any],
    ada: Persona,
    ben: Persona,
    cara: Persona,
) -> None:
    note_id, note_etag, request_id, _ = submitted(client, ada, ben)  # Ben holds propose_edit alone
    assert client.get(f"/v1/notes/{note_id}", auth=ben).json()["effectivePermissions"] == [
        "read",
        "propose_edit",
    ]
    response = client.post(
        f"/v1/edit-requests/{request_id}/comments", auth=ben, json=examples["CreateEditRequestCommentRequest"]
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert UUID.match(body["id"]) and body["requestId"] == request_id and body["authorId"] == me(client, ben)
    assert body["body"] == FEEDBACK and body["createdAt"] == body["updatedAt"] == START_TS
    assert response.headers["Location"] == f"/v1/edit-requests/{request_id}/comments/{body['id']}"
    assert ETAG.match(response.headers["ETag"])
    assert body == {
        **examples["RequestCommentExample"],
        "id": body["id"],
        "requestId": request_id,
        "authorId": me(client, ben),
        "createdAt": START_TS,
        "updatedAt": START_TS,
    }
    clock.advance(timedelta(seconds=1))
    owners = comment(client, ada, request_id, body="Ada's reply")
    assert owners.status_code == 201
    assert (
        add(client, ada, note_id, note_etag, me(client, cara)).status_code == 200
    )  # a co-owner inspects too
    for persona in (ada, ben, cara):
        page = listed(client, persona, request_id)
        assert page.status_code == 200 and [item["id"] for item in page.json()["items"]] == [
            body["id"],
            owners.json()["id"],
        ]
        single = get_comment(client, persona, request_id, body["id"])
        assert (
            single.status_code == 200
            and single.json() == body
            and single.headers["ETag"] == response.headers["ETag"]
        )
    assert comment(client, cara, request_id, body="Cara's reply").status_code == 201


def test_other_readers_and_strangers_get_404(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, _, request_id, _ = submitted(client, ada, ben)
    share(
        client, ada, note_id, user(client, cara), "comment", "propose_edit"
    )  # a reader with every permission
    created = comment(client, ben, request_id)
    comment_id = created.json()["id"]
    for persona in (cara, dan):
        assert listed(client, persona, request_id).status_code == 404
        assert get_comment(client, persona, request_id, comment_id).status_code == 404
        assert comment(client, persona, request_id).status_code == 404
        assert edit(client, persona, request_id, comment_id, created.headers["ETag"]).status_code == 404
        assert remove(client, persona, request_id, comment_id, created.headers["ETag"]).status_code == 404
    assert client.get(f"/v1/edit-requests/{request_id}/comments").status_code == 401
    assert client.post(f"/v1/edit-requests/{request_id}/comments", json={"body": "x"}).status_code == 401
    assert listed(client, ada, str(uuid.uuid4())).status_code == 404


def test_pages_are_oldest_first_with_ties_by_id_and_cursors_bound_to_the_request(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, _ = submitted(client, ada, ben)
    other = submit(client, ben, note_id, proposal(note_etag, explanation="other")).json()["id"]
    same_instant = sorted(
        comment(client, persona, request_id, body=f"c{i}").json()["id"]
        for i, persona in enumerate((ada, ben, ada))
    )
    clock.advance(timedelta(seconds=1))
    later = comment(client, ben, request_id, body="later").json()["id"]
    first = listed(client, ben, request_id, limit=2)
    assert [item["id"] for item in first.json()["items"]] == same_instant[:2]
    cursor = first.json()["nextCursor"]
    second = listed(client, ben, request_id, limit=2, cursor=cursor)
    assert [item["id"] for item in second.json()["items"]] == [same_instant[2], later]
    assert second.json()["nextCursor"] is None
    for reused in (
        listed(client, ada, request_id, limit=2, cursor=cursor),
        listed(client, ben, request_id, limit=3, cursor=cursor),
        listed(client, ben, other, limit=2, cursor=cursor),
    ):
        assert reused.status_code == 400 and reused.json()["code"] == "invalid_cursor"
    assert errors(listed(client, ben, request_id, limit=0)) == [("query", "limit")]


def test_a_comment_reached_through_another_request_is_404(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_a, _ = submitted(client, ada, ben)
    request_b = submit(client, ben, note_id, proposal(note_etag, explanation="B")).json()["id"]
    on_b = comment(client, ben, request_b).json()["id"]
    assert get_comment(client, ada, request_a, on_b).status_code == 404
    assert get_comment(client, ada, request_b, on_b).status_code == 200
    assert listed(client, ada, request_a).json()["items"] == []
    assert edit(client, ben, request_a, on_b, STALE).status_code == 404
    assert remove(client, ada, request_a, on_b, STALE).status_code == 404
    assert errors(client.get(f"/v1/edit-requests/{request_a}/comments/nope", auth=ada)) == [
        ("path", "commentId")
    ]


def test_the_author_edits_and_owners_cannot_rewrite_someone_elses(
    client: ContractClient, clock: FakeClock, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    _, _, request_id, _ = submitted(client, ada, ben)
    created = comment(client, ben, request_id)
    comment_id, first_etag = created.json()["id"], created.headers["ETag"]
    assert (
        edit(client, ada, request_id, comment_id, first_etag).status_code == 403
    )  # an owner, not the author
    clock.advance(timedelta(minutes=1))
    edited = client.patch(
        f"/v1/edit-requests/{request_id}/comments/{comment_id}",
        auth=ben,
        if_match=first_etag,
        json=examples["UpdateEditRequestCommentRequest"],
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["body"] == REVISED and edited.json()["updatedAt"] == "2026-09-13T12:01:00.000000Z"
    assert edited.headers["ETag"] != first_etag
    clock.advance(timedelta(minutes=1))
    same = edit(client, ben, request_id, comment_id, edited.headers["ETag"])
    assert (
        same.status_code == 200
        and same.headers["ETag"] == edited.headers["ETag"]
        and same.json() == edited.json()
    )
    assert edit(client, ben, request_id, comment_id, first_etag, body="stale").status_code == 412
    own = comment(client, ada, request_id, body="Ada's note")
    assert (
        edit(
            client, ada, request_id, own.json()["id"], own.headers["ETag"], body="Ada's edited note"
        ).status_code
        == 200
    )


def test_owners_delete_any_comment_and_the_author_their_own(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id, note_etag, request_id, _ = submitted(client, ada, ben)
    add(client, ada, note_id, note_etag, me(client, cara))
    bens = comment(client, ben, request_id, body="Ben 1")
    adas = comment(client, ada, request_id, body="Ada 1")
    bens_second = comment(client, ben, request_id, body="Ben 2")
    assert remove(client, ben, request_id, adas.json()["id"], adas.headers["ETag"]).status_code == 403
    assert remove(client, ada, request_id, bens.json()["id"], bens.headers["ETag"]).status_code == 204
    assert remove(client, cara, request_id, adas.json()["id"], adas.headers["ETag"]).status_code == 204
    deleted = remove(client, ben, request_id, bens_second.json()["id"], bens_second.headers["ETag"])
    assert deleted.status_code == 204 and deleted.content == b"" and "ETag" not in deleted.headers
    assert listed(client, ada, request_id).json()["items"] == []
    gone = bens.json()["id"]
    assert get_comment(client, ada, request_id, gone).status_code == 404
    assert edit(client, ben, request_id, gone, bens.headers["ETag"]).status_code == 404
    assert remove(client, ada, request_id, gone, bens.headers["ETag"]).status_code == 404


def test_an_author_who_lost_read_access_is_404_everywhere(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id, _, request_id, _ = submitted(client, ada, ben)
    created = comment(client, ben, request_id)
    comment_id, etag = created.json()["id"], created.headers["ETag"]
    share_id = client.get(f"/v1/notes/{note_id}/shares", auth=ada).json()["items"][0]["id"]
    assert client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 204
    assert listed(client, ben, request_id).status_code == 404
    assert get_comment(client, ben, request_id, comment_id).status_code == 404
    assert comment(client, ben, request_id).status_code == 404
    assert edit(client, ben, request_id, comment_id, etag).status_code == 404
    assert remove(client, ben, request_id, comment_id, etag).status_code == 404
    assert get_comment(client, ada, request_id, comment_id).status_code == 200  # the owner keeps the record
    assert share(client, ada, note_id, user(client, ben), "read").status_code == 201  # read alone suffices
    assert edit(client, ben, request_id, comment_id, etag).status_code == 200


def test_closed_requests_still_take_comments(client: ContractClient, ada: Persona, ben: Persona) -> None:
    _, _, request_id, request_etag = submitted(client, ada, ben)
    assert withdraw(client, ben, request_id, request_etag).status_code == 200
    created = comment(client, ben, request_id)
    assert created.status_code == 201
    assert listed(client, ada, request_id).json()["items"] == [created.json()]
    edited = edit(client, ben, request_id, created.json()["id"], created.headers["ETag"])
    assert edited.status_code == 200
    assert remove(client, ada, request_id, created.json()["id"], edited.headers["ETag"]).status_code == 204


@pytest.mark.acceptance("Trash")
def test_a_trashed_note_freezes_request_comments_for_owners_and_hides_them_from_the_proposer(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, _ = submitted(client, ada, ben)
    bens = comment(client, ben, request_id)
    clock.advance(timedelta(seconds=1))
    adas = comment(client, ada, request_id, body="Ada's own")
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=note_etag).headers["ETag"]
    assert [item["id"] for item in listed(client, ada, request_id).json()["items"]] == [
        bens.json()["id"],
        adas.json()["id"],
    ]
    assert get_comment(client, ada, request_id, bens.json()["id"]).status_code == 200
    refused = comment(client, ada, request_id)
    assert refused.status_code == 409 and refused.json()["code"] == "note_not_active"
    frozen_edit = edit(client, ada, request_id, adas.json()["id"], adas.headers["ETag"])
    assert frozen_edit.status_code == 409 and frozen_edit.json()["code"] == "note_not_active"
    assert (
        remove(client, ada, request_id, bens.json()["id"], bens.headers["ETag"]).json()["code"]
        == "note_not_active"
    )
    assert (
        remove(client, ada, request_id, bens.json()["id"], STALE).status_code == 412
    )  # the version check first
    assert listed(client, ben, request_id).status_code == 404  # the share went with the trash
    assert comment(client, ben, request_id).status_code == 404
    assert client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag).status_code == 200
    assert comment(client, ada, request_id, body="after the restore").status_code == 201
    assert remove(client, ada, request_id, bens.json()["id"], bens.headers["ETag"]).status_code == 204


def test_preconditions_and_body_validation(client: ContractClient, ada: Persona, ben: Persona) -> None:
    _, _, request_id, _ = submitted(client, ada, ben)
    created = comment(client, ben, request_id)
    comment_id, etag = created.json()["id"], created.headers["ETag"]
    url = f"/v1/edit-requests/{request_id}/comments"
    for body, pointers in (
        ({}, ["/body"]),
        ({"body": ""}, ["/body"]),
        ({"body": " \n"}, ["/body"]),
        ({"body": "x" * 10001}, ["/body"]),
        ({"body": "a\x00b"}, ["/body"]),
        ({"body": "ok", "authorId": "x"}, ["/authorId"]),
    ):
        response = client.post(url, auth=ben, json=body)
        assert response.status_code == 422 and [p for _, p in errors(response)] == pointers, response.text
        assert [
            p for _, p in errors(client.patch(f"{url}/{comment_id}", auth=ben, if_match=etag, json=body))
        ] == pointers
    assert client.post(url, auth=ben, content="x", headers=PLAIN).status_code == 415
    assert (
        client.post(url, auth=ben, content="{", headers={"Content-Type": "application/json"}).status_code
        == 400
    )
    assert edit(client, ben, request_id, comment_id, None).status_code == 428
    assert remove(client, ben, request_id, comment_id, None).status_code == 428
    for bad in ('W/"x"', "*", "x"):
        response = edit(client, ben, request_id, comment_id, bad)
        assert response.status_code == 400 and errors(response) == [("header", "If-Match")]
        assert remove(client, ben, request_id, comment_id, bad).status_code == 400
    assert edit(client, ben, request_id, comment_id, STALE).status_code == 412
    assert remove(client, ben, request_id, comment_id, STALE).status_code == 412
    assert edit(client, ada, request_id, comment_id, None).status_code == 403  # 403 precedes 428
    assert get_comment(client, ben, request_id, comment_id).json() == created.json()


def test_request_comments_never_move_the_request_or_the_note(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id, _, request_id, _ = submitted(client, ada, ben)
    before = versions(client, ada, note_id, request_id)
    clock.advance(timedelta(minutes=1))
    created = comment(client, ben, request_id)
    edited = edit(client, ben, request_id, created.json()["id"], created.headers["ETag"])
    assert remove(client, ada, request_id, created.json()["id"], edited.headers["ETag"]).status_code == 204
    assert versions(client, ada, note_id, request_id) == before


def test_a_competing_edit_committed_first_makes_the_second_stale(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, restore_hooks: None
) -> None:
    _, _, request_id, _ = submitted(client, ada, ben)
    created = comment(client, ben, request_id)
    comment_id, etag = created.json()["id"], created.headers["ETag"]
    competitor = ContractClient(app)
    fired: list[str] = []

    def before_begin(op: str) -> None:
        if op == "update_edit_request_comment" and not fired:
            fired.append(op)
            assert edit(competitor, ben, request_id, comment_id, etag, body="First").status_code == 200

    uow.hooks.before_begin = before_begin
    second = edit(client, ben, request_id, comment_id, etag, body="Second")
    assert second.status_code == 412 and fired == ["update_edit_request_comment"]
    assert get_comment(client, ada, request_id, comment_id).json()["body"] == "First"
