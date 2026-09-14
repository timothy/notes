"""Acceptance row "Protected notes": "With two or more owners, a PATCH naming title or body is refused whole
with `direct_edit_not_allowed` while a tags-only PATCH succeeds, and an owner added between read and PATCH is
caught inside the transaction. Removing the second-to-last owner restores direct edits and leaves open
requests mergeable. Owner and policy changes advance the note ETag and fail stale
`baseNoteETag`/`expectedNoteETag` values. A twenty-first owner and `requiredApprovals` above the owner count
are `422`." (The twenty-first owner is asserted in ``tests/test_owners.py``.)"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from notes_api import uow
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_edit_requests import STALE, seed_approvals, submit
from tests.test_merge import merge
from tests.test_notes import errors, me
from tests.test_owners import RUNBOOK, add, owned_by, remove, runbook
from tests.test_shares import share, user

pytestmark = pytest.mark.acceptance("Protected notes")

PEER = {"mode": "peer_approval", "requiredApprovals": 1}
SELF = {"mode": "self_merge", "requiredApprovals": None}
ROLLBACK = str(RUNBOOK["body"]) + "- Roll back if the error rate doubles\n"
PLAIN = {"Content-Type": "text/plain"}


def policy(
    client: ContractClient, persona: Persona, note_id: str, etag: str | None, body: Any, **kwargs: Any
) -> httpx.Response:
    return client.patch(
        f"/v1/notes/{note_id}/review-policy", auth=persona, if_match=etag, json=body, **kwargs
    )


def edit(client: ContractClient, persona: Persona, note_id: str, etag: str, **fields: Any) -> httpx.Response:
    return client.patch(f"/v1/notes/{note_id}", auth=persona, if_match=etag, json=fields)


def test_the_guides_walkthrough_up_to_the_merge(
    client: ContractClient,
    clock: FakeClock,
    examples: dict[str, Any],
    app: FastAPI,
    ada: Persona,
    cara: Persona,
) -> None:
    note_id, v2 = owned_by(client, ada, cara)
    ada_id, cara_id = me(client, ada), me(client, cara)
    clock.advance(timedelta(minutes=20))
    protected = policy(client, ada, note_id, v2, examples["ReviewPolicyPeerApprovalRequest"])
    assert protected.status_code == 200, protected.text
    v3 = protected.headers["ETag"]
    assert v3 != v2
    assert protected.json() == {
        **examples["NoteRunbookPeerApproval"],
        "id": note_id,
        "authorId": ada_id,
        "ownerIds": [ada_id, cara_id],
        "createdAt": "2026-09-13T12:00:00.000000Z",
        "updatedAt": "2026-09-13T12:20:00.000000Z",
    }
    refused = edit(client, ada, note_id, v3, body=ROLLBACK)
    assert refused.status_code == 409 and refused.json()["code"] == "direct_edit_not_allowed"
    assert edit(client, ada, note_id, v3, title="x", tags=[]).status_code == 409  # refused whole
    tagged = edit(client, ada, note_id, v3, tags=["ops", "runbook"])
    assert tagged.status_code == 200 and tagged.json()["tags"] == ["ops", "runbook"]
    v4 = tagged.headers["ETag"]
    proposal = {"title": RUNBOOK["title"], "body": ROLLBACK}
    submitted = submit(
        client,
        ada,
        note_id,
        {"baseNoteETag": v4, "proposedContent": proposal, "explanation": "Add the rollback trigger."},
    )
    assert submitted.status_code == 201, submitted.text
    request_id, request_etag = submitted.json()["id"], submitted.headers["ETag"]
    assert submitted.json()["requiredApprovals"] == 1 and submitted.json()["approvals"] == []
    alone = merge(client, ada, request_id, request_etag, {"expectedNoteETag": v4})
    assert alone.status_code == 409 and alone.json()["code"] == "approval_required"
    seed_approvals(app, request_id, cara_id)  # POST /approve arrives in PR 5b
    merged = merge(client, ada, request_id, request_etag, {"expectedNoteETag": v4})
    assert merged.status_code == 200, merged.text
    assert merged.json()["note"]["body"] == ROLLBACK and merged.json()["note"]["reviewPolicy"] == PEER
    assert merged.json()["note"]["tags"] == ["ops", "runbook"]
    assert merged.json()["editRequest"]["requiredApprovals"] == 1


def test_required_approvals_cannot_exceed_the_owner_count(
    client: ContractClient, examples: dict[str, Any], ada: Persona, cara: Persona
) -> None:
    note_id, etag = runbook(client, ada)
    too_many = policy(client, ada, note_id, etag, {"mode": "peer_approval", "requiredApprovals": 2})
    assert too_many.status_code == 422
    assert too_many.json() == {
        **examples["ProblemRequiredApprovalsExceedOwners"],
        "errors": [{"location": "body", "pointer": "/requiredApprovals", "detail": "the note has 1 owner"}],
    }
    single = policy(client, ada, note_id, etag, PEER)  # the policy may be set on a single-owner note
    assert single.status_code == 200 and single.json()["reviewPolicy"] == PEER
    added = add(client, ada, note_id, single.headers["ETag"], me(client, cara))
    three = policy(
        client, ada, note_id, added.headers["ETag"], {"mode": "peer_approval", "requiredApprovals": 3}
    )
    assert three.status_code == 422 and three.json() == examples["ProblemRequiredApprovalsExceedOwners"]
    two = policy(
        client, ada, note_id, added.headers["ETag"], {"mode": "peer_approval", "requiredApprovals": 2}
    )
    assert two.status_code == 200 and two.json()["reviewPolicy"]["requiredApprovals"] == 2


@pytest.mark.parametrize(
    ("body", "pointers"),
    [
        ({"mode": "self_merge", "requiredApprovals": 1}, ["/requiredApprovals"]),
        ({"mode": "self_merge"}, ["/requiredApprovals"]),
        ({"mode": "peer_approval", "requiredApprovals": None}, ["/requiredApprovals"]),
        ({"mode": "peer_approval"}, ["/requiredApprovals"]),
        ({"mode": "peer_approval", "requiredApprovals": 0}, ["/requiredApprovals"]),
        ({"mode": "peer_approval", "requiredApprovals": 21}, ["/requiredApprovals"]),
        ({"mode": "peer_approval", "requiredApprovals": 1.5}, ["/requiredApprovals"]),
        ({"mode": "consensus", "requiredApprovals": 1}, ["/mode"]),
        ({"requiredApprovals": 1}, ["/mode", "/requiredApprovals"]),
        ({"mode": "peer_approval", "requiredApprovals": 1, "owners": []}, ["/owners"]),
        ({}, ["/mode", "/requiredApprovals"]),
    ],
    ids=[
        "self_merge with a number",
        "self_merge without the field",
        "peer_approval with null",
        "peer_approval without the field",
        "zero",
        "twenty-one",
        "fraction",
        "unknown mode",
        "no mode",
        "unknown field",
        "empty",
    ],
)
def test_the_policy_body_is_validated_by_the_schema(
    client: ContractClient, ada: Persona, cara: Persona, body: dict[str, Any], pointers: list[str]
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    response = policy(client, ada, note_id, etag, body)
    assert response.status_code == 422, response.text
    assert [pointer for _, pointer in errors(response)] == pointers
    assert policy(client, ada, note_id, None, body).status_code == 422  # body shape before 428
    assert client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"] == etag


def test_only_the_author_sets_the_policy(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    share(client, ada, note_id, user(client, ben), "comment", "propose_edit")
    assert policy(client, cara, note_id, etag, PEER).status_code == 403  # a co-owner
    assert policy(client, ben, note_id, etag, PEER).status_code == 403  # a reader
    assert policy(client, dan, note_id, etag, PEER).status_code == 404  # a stranger
    assert policy(client, cara, note_id, None, PEER).status_code == 403  # 403 precedes 428
    assert client.patch(f"/v1/notes/{note_id}/review-policy", json=PEER).status_code == 401
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json()["reviewPolicy"] == SELF


def test_policy_preconditions_lifecycle_and_no_op(
    client: ContractClient, clock: FakeClock, ada: Persona, cara: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    assert policy(client, ada, note_id, None, PEER).status_code == 428
    for bad in ('W/"x"', "*", "x"):
        response = policy(client, ada, note_id, bad, PEER)
        assert response.status_code == 400 and errors(response) == [("header", "If-Match")]
    assert policy(client, ada, note_id, STALE, PEER).status_code == 412
    assert policy(client, ada, note_id, etag, PEER, content="x", headers=PLAIN).status_code == 415
    changed = policy(client, ada, note_id, etag, PEER)
    assert changed.status_code == 200 and changed.headers["ETag"] != etag
    clock.advance(timedelta(minutes=1))
    same = policy(client, ada, note_id, changed.headers["ETag"], PEER)
    assert same.status_code == 200 and same.headers["ETag"] == changed.headers["ETag"]
    assert same.json() == changed.json()  # a no-op keeps updatedAt
    back = policy(client, ada, note_id, same.headers["ETag"], SELF)
    assert (
        back.status_code == 200
        and back.json()["reviewPolicy"] == SELF
        and back.headers["ETag"] != same.headers["ETag"]
    )
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=back.headers["ETag"]).headers[
        "ETag"
    ]
    frozen = policy(client, ada, note_id, trash_etag, PEER)
    assert frozen.status_code == 409 and frozen.json()["code"] == "note_not_active"
    assert policy(client, ada, note_id, back.headers["ETag"], PEER).status_code == 412


def test_the_stored_policy_persists_when_owners_leave_and_direct_edits_resume(
    client: ContractClient, examples: dict[str, Any], ada: Persona, cara: Persona
) -> None:
    note_id, v2 = owned_by(client, ada, cara)
    ada_id, cara_id = me(client, ada), me(client, cara)
    v3 = policy(client, ada, note_id, v2, PEER).headers["ETag"]
    proposal = {"title": RUNBOOK["title"], "body": ROLLBACK}
    submitted = submit(client, ada, note_id, {"baseNoteETag": v3, "proposedContent": proposal})
    assert submitted.json()["requiredApprovals"] == 1
    request_id, request_etag = submitted.json()["id"], submitted.headers["ETag"]
    left = remove(client, cara, note_id, cara_id, v3)
    assert left.status_code == 200, left.text
    single = client.get(f"/v1/notes/{note_id}", auth=ada)
    assert (
        single.json()["ownerIds"] == [ada_id] and single.json()["reviewPolicy"] == PEER
    )  # the policy persists
    assert client.get(f"/v1/edit-requests/{request_id}", auth=ada).json()["requiredApprovals"] == 0
    retitled = edit(client, ada, note_id, single.headers["ETag"], title="Incident runbook (2026)")
    assert retitled.status_code == 200  # direct edits resume with one owner
    merged = merge(client, ada, request_id, request_etag, {"expectedNoteETag": retitled.headers["ETag"]})
    assert merged.status_code == 200, merged.text  # the open request stayed mergeable
    assert (
        merged.json()["note"]["title"] == "Incident runbook (2026)"
        and merged.json()["note"]["body"] == ROLLBACK
    )
    assert merged.json()["editRequest"]["requiredApprovals"] == 0
    assert merged.json()["note"]["reviewPolicy"] == PEER
    # The contract's after-owner-left example is this state, up to ids, the title change, and timestamps.
    after = examples["NoteRunbookAfterOwnerLeft"]
    assert merged.json()["note"]["reviewPolicy"] == after["reviewPolicy"]
    assert merged.json()["note"]["body"] == after["body"] and merged.json()["note"]["ownerIds"] == [ada_id]


def test_an_owner_added_between_read_and_patch_is_caught_inside_the_transaction(
    client: ContractClient, app: FastAPI, ada: Persona, cara: Persona, restore_hooks: None
) -> None:
    note_id, etag = runbook(client, ada)
    competitor = ContractClient(app)
    fired: list[str] = []

    def before_begin(op: str) -> None:
        if op == "update_note" and not fired:
            fired.append(op)
            assert add(competitor, ada, note_id, etag, me(client, cara)).status_code == 200

    uow.hooks.before_begin = before_begin
    stale = edit(client, ada, note_id, etag, body=ROLLBACK)
    assert stale.status_code == 412 and fired == ["update_note"]  # the addition moved the version
    live = client.get(f"/v1/notes/{note_id}", auth=ada)
    assert live.json()["body"] == RUNBOOK["body"] and len(live.json()["ownerIds"]) == 2
    protected = edit(client, ada, note_id, live.headers["ETag"], body=ROLLBACK)
    assert protected.status_code == 409 and protected.json()["code"] == "direct_edit_not_allowed"


def test_owner_and_policy_changes_fail_stale_base_and_expected_note_etags(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id, v1 = runbook(client, ada)
    share(client, ada, note_id, user(client, ben), "propose_edit")
    proposal = {"title": RUNBOOK["title"], "body": ROLLBACK}
    submitted = submit(client, ben, note_id, {"baseNoteETag": v1, "proposedContent": proposal})
    request_id, request_etag = submitted.json()["id"], submitted.headers["ETag"]
    v2 = policy(client, ada, note_id, v1, PEER).headers["ETag"]
    assert (
        submit(
            client, ben, note_id, {"baseNoteETag": v1, "proposedContent": {**proposal, "title": "x"}}
        ).status_code
        == 412
    )
    assert merge(client, ada, request_id, request_etag, {"expectedNoteETag": v1}).status_code == 412
    v3 = add(client, ada, note_id, v2, me(client, cara)).headers["ETag"]
    assert merge(client, ada, request_id, request_etag, {"expectedNoteETag": v2}).status_code == 412
    assert (
        client.get(f"/v1/edit-requests/{request_id}", auth=ada).headers["ETag"] == request_etag
    )  # untouched
    assert client.get(f"/v1/edit-requests/{request_id}", auth=ada).json()["requiredApprovals"] == 2  # live
    blocked = merge(client, ada, request_id, request_etag, {"expectedNoteETag": v3})
    assert blocked.status_code == 409 and blocked.json()["code"] == "approval_required"
