"""Acceptance row "Owner control": "Recipients cannot directly PATCH a note, manage shares or owners,
trash/restore, preview, approve, reject, or merge. Team admins do not gain those rights. Co-owners cannot add
or remove other owners or change the policy; only the author can, and the author cannot be removed." The
policy half and the protected-note behaviour are in ``tests/test_review_policy.py``."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from notes_api import uow
from notes_api.models import NoteOwner, User
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_edit_requests import (
    STALE,
    approvers,
    get_request,
    proposal,
    seed_approvals,
    seed_request,
    submit,
)
from tests.test_merge import merge
from tests.test_notes import errors, me
from tests.test_shares import share, user

pytestmark = pytest.mark.acceptance("Owner control")

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
RUNBOOK = {
    "title": "Incident runbook",
    "body": "## Incident\n\n- Page the on-call\n- Open a channel\n",
    "tags": ["ops"],
}
PLAIN = {"Content-Type": "text/plain"}


def add(
    client: ContractClient, persona: Persona, note_id: str, etag: str | None, user_id: str, **kwargs: Any
) -> httpx.Response:
    return client.post(
        f"/v1/notes/{note_id}/owners", auth=persona, if_match=etag, json={"userId": user_id}, **kwargs
    )


def remove(
    client: ContractClient, persona: Persona, note_id: str, user_id: str, etag: str | None, **kwargs: Any
) -> httpx.Response:
    return client.delete(f"/v1/notes/{note_id}/owners/{user_id}", auth=persona, if_match=etag, **kwargs)


def runbook(client: ContractClient, author: Persona) -> tuple[str, str]:
    created = client.post("/v1/notes", auth=author, json=RUNBOOK)
    assert created.status_code == 201
    return str(created.json()["id"]), str(created.headers["ETag"])


def owned_by(client: ContractClient, author: Persona, *co_owners: Persona) -> tuple[str, str]:
    """The runbook with ``co_owners`` added through the API; returns the note id and its current ETag."""
    note_id, etag = runbook(client, author)
    for co_owner in co_owners:
        response = add(client, author, note_id, etag, me(client, co_owner))
        assert response.status_code == 200, response.text
        etag = response.headers["ETag"]
    return note_id, etag


def seed_owners(app: FastAPI, note_id: str, count: int) -> None:
    """``count`` extra users, each written directly as a co-owner at the positions after the current ones."""
    with app.state.session_factory() as session, session.begin():
        highest = session.execute(
            select(func.max(NoteOwner.position)).where(NoteOwner.note_id == uuid.UUID(note_id))
        ).scalar_one()
        for index in range(count):
            owner = User(
                id=uuid.uuid4(), issuer="i", subject=f"owner{index}", display_name="x", created_at=START
            )
            session.add(owner)
            session.flush()
            session.add(
                NoteOwner(
                    note_id=uuid.UUID(note_id), user_id=owner.id, position=highest + index + 1, added_at=START
                )
            )


def test_the_author_adds_a_co_owner_with_the_contracts_request(
    client: ContractClient, clock: FakeClock, examples: dict[str, Any], ada: Persona, cara: Persona
) -> None:
    note_id, etag = runbook(client, ada)
    ada_id, cara_id = me(client, ada), me(client, cara)
    clock.advance(timedelta(minutes=10))
    body = {**examples["AddOwnerRequest"], "userId": cara_id}
    response = add(client, ada, note_id, etag, body["userId"])
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] != etag
    expected = {
        **examples["NoteRunbookProtected"],
        "id": note_id,
        "authorId": ada_id,
        "ownerIds": [ada_id, cara_id],
        "createdAt": "2026-09-13T12:00:00.000000Z",
        "updatedAt": "2026-09-13T12:10:00.000000Z",
    }
    assert response.json() == expected  # protected, policy still self_merge, Ada's view
    as_cara = client.get(f"/v1/notes/{note_id}", auth=cara)
    assert as_cara.status_code == 200 and as_cara.headers["ETag"] == response.headers["ETag"]
    assert as_cara.json()["isOwner"] is True
    assert as_cara.json()["effectivePermissions"] == ["read", "comment", "propose_edit"]
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json() == expected


def test_only_the_author_adds_owners(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    share(client, ada, note_id, user(client, ben), "comment", "propose_edit")
    refused = add(client, cara, note_id, etag, me(client, dan))  # a co-owner
    assert refused.status_code == 403 and refused.json()["code"] == "forbidden"
    assert add(client, ben, note_id, etag, me(client, dan)).status_code == 403  # a reader
    assert add(client, dan, note_id, etag, me(client, dan)).status_code == 404  # a stranger
    assert add(client, cara, note_id, None, me(client, dan)).status_code == 403  # 403 precedes 428
    assert client.post(f"/v1/notes/{note_id}/owners", json={"userId": me(client, dan)}).status_code == 401
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json()["ownerIds"] == [
        me(client, ada),
        me(client, cara),
    ]


def test_add_owner_validates_conflicts_and_the_body(
    client: ContractClient, app: FastAPI, examples: dict[str, Any], ada: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    unknown = add(client, ada, note_id, etag, str(uuid.uuid4()))
    assert unknown.status_code == 422 and errors(unknown) == [("body", "/userId")]
    assert unknown.json()["errors"][0]["detail"] == "unknown user"
    duplicate = add(client, ada, note_id, etag, me(client, cara))
    assert duplicate.status_code == 409 and duplicate.json() == examples["ProblemDuplicateOwner"]
    assert add(client, ada, note_id, etag, me(client, ada)).json()["code"] == "duplicate_owner"
    url = f"/v1/notes/{note_id}/owners"
    assert errors(client.post(url, auth=ada, if_match=etag, json={})) == [("body", "/userId")]
    assert errors(client.post(url, auth=ada, if_match=etag, json={"userId": "ben"})) == [("body", "/userId")]
    assert errors(
        client.post(url, auth=ada, if_match=etag, json={"userId": me(client, dan), "role": "x"})
    ) == [("body", "/role")]
    assert client.post(url, auth=ada, if_match=etag, content="x", headers=PLAIN).status_code == 415
    assert (
        client.post(
            url, auth=ada, if_match=etag, content="{", headers={"Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"] == etag  # nothing moved
    # The twentieth owner is the last: seventeen seeded rows plus Ada and Cara make nineteen, Dan is twenty.
    seed_owners(app, note_id, 17)
    twentieth = add(client, ada, note_id, etag, me(client, dan))
    assert twentieth.status_code == 200 and len(twentieth.json()["ownerIds"]) == 20
    with app.state.session_factory() as session, session.begin():
        extra = User(id=uuid.uuid4(), issuer="i", subject="twenty-first", display_name="x", created_at=START)
        session.add(extra)
    refused = add(client, ada, note_id, twentieth.headers["ETag"], str(extra.id))
    assert refused.status_code == 422 and errors(refused) == [("body", "/userId")]
    assert refused.json()["errors"][0]["detail"] == "the note already has 20 owners"


def test_add_owner_preconditions_and_lifecycle(
    client: ContractClient, ada: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    dan_id = me(client, dan)
    assert add(client, ada, note_id, None, dan_id).status_code == 428
    for bad in ('W/"x"', "*", "x"):
        response = add(client, ada, note_id, bad, dan_id)
        assert response.status_code == 400 and errors(response) == [("header", "If-Match")]
    assert add(client, ada, note_id, STALE, dan_id).status_code == 412
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=etag).headers["ETag"]
    frozen = add(client, ada, note_id, trash_etag, dan_id)
    assert frozen.status_code == 409 and frozen.json()["code"] == "note_not_active"
    assert add(client, ada, note_id, etag, dan_id).status_code == 412  # the version check comes first
    restored = client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag)
    assert add(client, ada, note_id, restored.headers["ETag"], dan_id).status_code == 200


def test_a_share_held_by_the_new_owner_stays_and_is_effective_again_after_removal(
    client: ContractClient, ada: Persona, cara: Persona
) -> None:
    note_id, etag = runbook(client, ada)
    share_id = share(client, ada, note_id, user(client, cara), "comment").json()["id"]
    added = add(client, ada, note_id, etag, me(client, cara))
    assert added.status_code == 200
    seen = client.get(f"/v1/notes/{note_id}", auth=cara).json()
    assert seen["isOwner"] is True and seen["effectivePermissions"] == ["read", "comment", "propose_edit"]
    listed = client.get(f"/v1/notes/{note_id}/shares", auth=ada).json()["items"]
    assert [item["id"] for item in listed] == [share_id]  # redundant while Cara owns the note, but kept
    removed = remove(client, ada, note_id, me(client, cara), added.headers["ETag"])
    assert removed.status_code == 200, removed.text
    assert removed.json()["ownerIds"] == [me(client, ada)] and removed.json()["isOwner"] is True
    seen = client.get(f"/v1/notes/{note_id}", auth=cara).json()
    assert seen["isOwner"] is False and seen["effectivePermissions"] == ["read", "comment"]  # the share again


def test_positions_continue_past_a_gap(
    client: ContractClient, ada: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara, dan)
    ada_id, cara_id, dan_id = me(client, ada), me(client, cara), me(client, dan)
    removed = remove(client, ada, note_id, cara_id, etag)
    assert removed.json()["ownerIds"] == [ada_id, dan_id]
    again = add(client, ada, note_id, removed.headers["ETag"], cara_id)
    assert again.status_code == 200 and again.json()["ownerIds"] == [ada_id, dan_id, cara_id]


def test_who_may_remove_whom(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara, dan)
    ada_id, ben_id, cara_id, dan_id = me(client, ada), me(client, ben), me(client, cara), me(client, dan)
    assert remove(client, ben, note_id, cara_id, etag).status_code == 404  # a stranger sees no note
    share(client, ada, note_id, user(client, ben), "comment", "propose_edit")
    assert remove(client, ben, note_id, cara_id, etag).status_code == 403  # a reader may not act
    assert remove(client, cara, note_id, dan_id, etag).status_code == 403  # a co-owner names another owner
    assert remove(client, cara, note_id, ada_id, etag).status_code == 403  # ... or the author
    assert remove(client, cara, note_id, dan_id, None).status_code == 403  # 403 precedes 428
    assert remove(client, ada, note_id, ben_id, etag).status_code == 404  # a non-owner target
    assert remove(client, cara, note_id, ben_id, etag).status_code == 404  # ... for anyone (404 before 403)
    assert remove(client, ben, note_id, ben_id, etag).status_code == 404
    author = remove(client, ada, note_id, ada_id, etag)
    assert author.status_code == 409 and author.json() == examples["ProblemAuthorCannotBeRemoved"]
    assert client.delete(f"/v1/notes/{note_id}/owners/{cara_id}").status_code == 401
    assert errors(client.delete(f"/v1/notes/{note_id}/owners/not-a-uuid", auth=ada, if_match=etag)) == [
        ("path", "userId")
    ]
    assert client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"] == etag  # nothing moved yet
    # A co-owner leaves: the response is their own view, read alone since they hold no share.
    left = remove(client, cara, note_id, cara_id, etag)
    assert left.status_code == 200, left.text
    assert left.json()["ownerIds"] == [ada_id, dan_id] and left.json()["isOwner"] is False
    assert left.json()["effectivePermissions"] == ["read"] and left.headers["ETag"] != etag
    assert client.get(f"/v1/notes/{note_id}", auth=cara).status_code == 404  # and that was the last read
    # The author removes the remaining co-owner and keeps the owner's view.
    alone = remove(client, ada, note_id, dan_id, left.headers["ETag"])
    assert alone.status_code == 200 and alone.json()["ownerIds"] == [ada_id]
    assert alone.json()["isOwner"] is True and alone.json()["effectivePermissions"] == [
        "read",
        "comment",
        "propose_edit",
    ]


def test_a_self_removal_keeps_the_permissions_of_a_remaining_share(
    client: ContractClient, ada: Persona, cara: Persona
) -> None:
    note_id, etag = runbook(client, ada)
    share(client, ada, note_id, user(client, cara), "comment")
    added = add(client, ada, note_id, etag, me(client, cara))
    left = remove(client, cara, note_id, me(client, cara), added.headers["ETag"])
    assert left.status_code == 200
    assert left.json()["isOwner"] is False and left.json()["effectivePermissions"] == ["read", "comment"]
    assert client.get(f"/v1/notes/{note_id}", auth=cara).json() == left.json()


def test_remove_owner_preconditions_and_lifecycle(
    client: ContractClient, ada: Persona, cara: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    cara_id = me(client, cara)
    assert remove(client, ada, note_id, cara_id, None).status_code == 428
    assert remove(client, ada, note_id, cara_id, 'W/"x"').status_code == 400
    assert remove(client, ada, note_id, cara_id, STALE).status_code == 412
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=etag).headers["ETag"]
    frozen = remove(client, ada, note_id, cara_id, trash_etag)
    assert frozen.status_code == 409 and frozen.json()["code"] == "note_not_active"
    assert remove(client, cara, note_id, cara_id, etag).status_code == 412  # owners still see the trash
    restored = client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag)
    ignored = remove(client, ada, note_id, cara_id, restored.headers["ETag"], content="x", headers=PLAIN)
    assert ignored.status_code == 200  # no body is ever read


def test_removal_deletes_the_leavers_approvals_and_bumps_only_those_requests(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    ada_id, ben_id, cara_id = me(client, ada), me(client, ben), me(client, cara)
    share(client, ada, note_id, user(client, ben), "propose_edit")
    a = submit(client, ben, note_id, proposal(etag, explanation="A"))
    b = submit(client, ben, note_id, proposal(etag, explanation="B"))
    a_id, a_etag, b_id, b_etag = a.json()["id"], a.headers["ETag"], b.json()["id"], b.headers["ETag"]
    seed_approvals(app, a_id, cara_id)
    seed_approvals(app, b_id, ada_id)
    c_id, c_etag = seed_request(app, note_id=note_id, proposer_id=ben_id, status="withdrawn", at=START)
    seed_approvals(app, c_id, cara_id)
    before_b = get_request(client, ada, b_id).json()

    removed = remove(client, ada, note_id, cara_id, etag)
    assert removed.status_code == 200, removed.text
    after_a = get_request(client, ada, a_id)
    assert after_a.json()["approvals"] == [] and after_a.headers["ETag"] != a_etag
    assert after_a.json()["updatedAt"] == removed.json()["updatedAt"]
    after_b = get_request(client, ada, b_id)
    assert approvers(client, ada, b_id) == [ada_id] and after_b.headers["ETag"] == b_etag
    assert after_b.json() == before_b  # untouched, requiredApprovals included (0 on this self_merge note)
    after_c = get_request(client, ada, c_id)
    assert approvers(client, ada, c_id) == [cara_id] and after_c.headers["ETag"] == c_etag  # frozen
    # The note's version moved, so both note ETags a client may hold are stale.
    assert submit(client, ben, note_id, proposal(etag, explanation="late")).status_code == 412
    assert merge(client, ada, b_id, b_etag, {"expectedNoteETag": etag}).status_code == 412
    assert merge(client, ada, b_id, b_etag, {"expectedNoteETag": removed.headers["ETag"]}).status_code == 200


def test_a_competing_removal_committed_first_makes_the_addition_stale(
    client: ContractClient, app: FastAPI, ada: Persona, cara: Persona, dan: Persona, restore_hooks: None
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    competitor = ContractClient(app)
    fired: list[str] = []

    def before_begin(op: str) -> None:
        if op == "add_owner" and not fired:
            fired.append(op)
            assert remove(competitor, cara, note_id, me(client, cara), etag).status_code == 200

    uow.hooks.before_begin = before_begin
    stale = add(client, ada, note_id, etag, me(client, dan))
    assert stale.status_code == 412 and fired == ["add_owner"]
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json()["ownerIds"] == [me(client, ada)]


def test_two_co_owners_leaving_on_one_etag_permit_exactly_one(
    client: ContractClient, app: FastAPI, ada: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara, dan)
    barrier = threading.Barrier(2)
    outcomes: list[int] = []
    lock = threading.Lock()

    def leave(persona: Persona) -> None:
        own = ContractClient(app)
        user_id = me(own, persona)
        barrier.wait()
        response = remove(own, persona, note_id, user_id, etag)
        with lock:
            outcomes.append(response.status_code)

    threads = [threading.Thread(target=leave, args=(cara,)), threading.Thread(target=leave, args=(dan,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [200, 412], outcomes
    assert len(client.get(f"/v1/notes/{note_id}", auth=ada).json()["ownerIds"]) == 2


def test_recipients_and_team_admins_cannot_administer_the_note(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    """The row's first sentences, gathered: a recipient with every permission, and an admin of a recipient
    team, can neither PATCH the note's content, nor manage shares or owners, nor trash it."""
    from tests.test_shares import team

    note_id, etag = runbook(client, ada)
    platform = team(client, ben)  # Ben administers the team the note is shared with
    share(client, ada, note_id, platform, "comment", "propose_edit")
    assert (
        client.patch(f"/v1/notes/{note_id}", auth=ben, if_match=etag, json={"title": "x"}).status_code == 403
    )
    assert client.get(f"/v1/notes/{note_id}/shares", auth=ben).status_code == 403
    assert add(client, ben, note_id, etag, me(client, cara)).status_code == 403
    assert (
        client.patch(
            f"/v1/notes/{note_id}/review-policy",
            auth=ben,
            if_match=etag,
            json={"mode": "self_merge", "requiredApprovals": None},
        ).status_code
        == 403
    )
    assert client.delete(f"/v1/notes/{note_id}", auth=ben, if_match=etag).status_code == 403
    assert client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"] == etag
