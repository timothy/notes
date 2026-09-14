"""Acceptance row "Approvals": "Only owners other than the proposer can approve; the proposer gets `403` and
non-inspectors `404`; re-approving and revoking a missing approval are no-ops. A content revision deletes
approvals, an explanation-only revision keeps them, and owner removal deletes that owner's approvals on open
requests with those request ETags advancing. Merge counts stored approvals plus a non-proposing merger; too
few is `approval_required` with nothing written; under `peer_approval` a non-owner's proposal needs two
distinct owners even with `requiredApprovals: 1`; `finalContent` is `422` under `peer_approval` and works
under `self_merge`. Closed requests freeze `approvals` and `requiredApprovals`; a rejected request records
`rejectedBy`." The counting and freezing are in ``tests/test_merge.py``, the owner removal in
``tests/test_owners.py``; this module is the two actions themselves."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_edit_requests import STALE, get_request, proposal, revise, submit, withdraw
from tests.test_merge import merge
from tests.test_notes import errors, me
from tests.test_owners import RUNBOOK, add, owned_by, runbook
from tests.test_review_policy import PEER, ROLLBACK, policy
from tests.test_shares import share, user

pytestmark = pytest.mark.acceptance("Approvals")

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
START_TS = "2026-09-13T12:00:00.000000Z"
PLAIN = {"Content-Type": "text/plain"}


def approve(
    client: ContractClient, persona: Persona, request_id: str, etag: str | None, **kwargs: Any
) -> httpx.Response:
    return client.post(f"/v1/edit-requests/{request_id}/approve", auth=persona, if_match=etag, **kwargs)


def revoke(
    client: ContractClient, persona: Persona, request_id: str, etag: str | None, **kwargs: Any
) -> httpx.Response:
    return client.post(
        f"/v1/edit-requests/{request_id}/revoke-approval", auth=persona, if_match=etag, **kwargs
    )


def protected_proposal(client: ContractClient, ada: Persona, cara: Persona) -> tuple[str, str, str, str]:
    """The guide's runbook under peer approval with Ada's own proposal.

    Returns the note id, the note ETag, the request id, and the request ETag.
    """
    note_id, v2 = owned_by(client, ada, cara)
    v3 = policy(client, ada, note_id, v2, PEER).headers["ETag"]
    body = {
        "baseNoteETag": v3,
        "proposedContent": {"title": RUNBOOK["title"], "body": ROLLBACK},
        "explanation": "Add the rollback trigger.",
    }
    submitted = submit(client, ada, note_id, body)
    assert submitted.status_code == 201, submitted.text
    return note_id, v3, str(submitted.json()["id"]), str(submitted.headers["ETag"])


def awaiting(
    examples: dict[str, Any], note_id: str, request_id: str, proposer_id: str, **overrides: Any
) -> dict[str, Any]:
    """The contract's ``EditRequestAwaitingApproval`` with the live identifiers and timestamps."""
    return {
        **examples["EditRequestAwaitingApproval"],
        "id": request_id,
        "noteId": note_id,
        "proposerId": proposer_id,
        "createdAt": START_TS,
        "updatedAt": START_TS,
        **overrides,
    }


def test_a_peer_approval_matches_the_contracts_example_and_repeats_are_no_ops(
    client: ContractClient, clock: FakeClock, examples: dict[str, Any], ada: Persona, cara: Persona
) -> None:
    note_id, _, request_id, etag = protected_proposal(client, ada, cara)
    ada_id, cara_id = me(client, ada), me(client, cara)
    assert get_request(client, ada, request_id).json() == awaiting(examples, note_id, request_id, ada_id)
    clock.advance(timedelta(minutes=10))
    approved = approve(client, cara, request_id, etag)
    assert approved.status_code == 200, approved.text
    assert approved.headers["ETag"] != etag
    assert approved.json() == awaiting(
        examples,
        note_id,
        request_id,
        ada_id,
        updatedAt="2026-09-13T12:10:00.000000Z",
        approvals=[{"userId": cara_id, "approvedAt": "2026-09-13T12:10:00.000000Z"}],
    )
    assert approved.json()["approvals"] == [
        {
            **examples["EditRequestApproved"]["approvals"][0],
            "userId": cara_id,
            "approvedAt": "2026-09-13T12:10:00.000000Z",
        }
    ]
    clock.advance(timedelta(minutes=5))
    again = approve(client, cara, request_id, approved.headers["ETag"])
    assert again.status_code == 200 and again.headers["ETag"] == approved.headers["ETag"]
    assert again.json() == approved.json()  # one approval, updatedAt unchanged
    assert get_request(client, ada, request_id).headers["ETag"] == approved.headers["ETag"]


def test_revoking_restores_the_awaiting_state_and_a_missing_approval_is_a_no_op(
    client: ContractClient,
    clock: FakeClock,
    examples: dict[str, Any],
    ada: Persona,
    cara: Persona,
    dan: Persona,
) -> None:
    note_id, v3, request_id, etag = protected_proposal(client, ada, cara)
    ada_id = me(client, ada)
    approved = approve(client, cara, request_id, etag)
    clock.advance(timedelta(minutes=1))
    revoked = revoke(client, cara, request_id, approved.headers["ETag"])
    assert revoked.status_code == 200, revoked.text
    assert revoked.headers["ETag"] not in (etag, approved.headers["ETag"])
    assert revoked.json() == awaiting(
        examples, note_id, request_id, ada_id, updatedAt="2026-09-13T12:01:00.000000Z"
    )
    clock.advance(timedelta(minutes=1))
    nothing = revoke(client, cara, request_id, revoked.headers["ETag"])
    assert nothing.status_code == 200 and nothing.headers["ETag"] == revoked.headers["ETag"]
    assert nothing.json() == revoked.json()
    # Another owner who never approved revokes nothing either.
    added = add(client, ada, note_id, v3, me(client, dan))
    assert added.status_code == 200
    assert revoke(client, dan, request_id, revoked.headers["ETag"]).headers["ETag"] == revoked.headers["ETag"]


def test_the_proposer_cannot_approve_and_others_need_ownership(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, v3, request_id, etag = protected_proposal(client, ada, cara)
    share(client, ada, note_id, user(client, ben), "propose_edit")
    share(client, ada, note_id, user(client, dan), "read")
    own = approve(client, ada, request_id, etag)  # the owner-proposer
    assert own.status_code == 403 and own.json() == examples["ProblemSelfApproval"]
    assert approve(client, ada, request_id, None).status_code == 403  # 403 precedes 428
    plain = revoke(client, ada, request_id, etag)
    assert plain.status_code == 403 and plain.json() == examples["ProblemForbidden"]
    theirs = submit(client, ben, note_id, proposal(v3, explanation="Ben's"))
    ben_request, ben_etag = theirs.json()["id"], theirs.headers["ETag"]
    self_approval = approve(client, ben, ben_request, ben_etag)  # a non-owner proposer
    assert self_approval.status_code == 403 and self_approval.json() == examples["ProblemSelfApproval"]
    # A proposer of another request is neither an owner nor this request's proposer: they cannot inspect it.
    assert approve(client, ben, request_id, etag).status_code == 404
    assert revoke(client, ben, request_id, etag).status_code == 404
    assert approve(client, dan, request_id, etag).status_code == 404  # a reader cannot inspect it
    assert revoke(client, dan, request_id, etag).status_code == 404
    assert client.post(f"/v1/edit-requests/{request_id}/approve").status_code == 401
    assert errors(client.post("/v1/edit-requests/nope/approve", auth=cara, if_match=etag)) == [
        ("path", "requestId")
    ]
    assert get_request(client, ada, request_id).headers["ETag"] == etag  # nothing moved


def test_preconditions_lifecycle_and_closed_requests(
    client: ContractClient, ada: Persona, cara: Persona
) -> None:
    note_id, v3, request_id, etag = protected_proposal(client, ada, cara)
    assert approve(client, cara, request_id, None).status_code == 428
    assert revoke(client, cara, request_id, None).status_code == 428
    for bad in ('W/"x"', "*", "x"):
        response = approve(client, cara, request_id, bad)
        assert response.status_code == 400 and errors(response) == [("header", "If-Match")]
        assert revoke(client, cara, request_id, bad).status_code == 400
    assert approve(client, cara, request_id, STALE).status_code == 412
    assert revoke(client, cara, request_id, STALE).status_code == 412
    ignored = approve(client, cara, request_id, etag, content="anything", headers=PLAIN)
    assert ignored.status_code == 200  # no body is ever read
    approved_etag = ignored.headers["ETag"]
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=v3).headers["ETag"]
    frozen = revoke(client, cara, request_id, approved_etag)
    assert frozen.status_code == 409 and frozen.json()["code"] == "note_not_active"
    assert approve(client, cara, request_id, STALE).status_code == 412  # the version check still comes first
    restored = client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag)
    assert restored.status_code == 200
    withdrawn = withdraw(client, ada, request_id, approved_etag)
    assert withdrawn.status_code == 200 and withdrawn.json()["approvals"] != []  # frozen, not deleted
    assert approve(client, cara, request_id, approved_etag).status_code == 412
    closed = approve(client, cara, request_id, withdrawn.headers["ETag"])
    assert closed.status_code == 409 and closed.json()["code"] == "request_not_open"
    assert revoke(client, cara, request_id, withdrawn.headers["ETag"]).json()["code"] == "request_not_open"


def test_approvals_are_recorded_where_the_policy_does_not_require_them(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    # A protected self_merge note: Cara's approval is recorded, Ada merges without needing it.
    note_id, etag = owned_by(client, ada, cara)
    share(client, ada, note_id, user(client, ben), "propose_edit")
    theirs = submit(client, ben, note_id, proposal(etag))
    request_id, request_etag = theirs.json()["id"], theirs.headers["ETag"]
    assert theirs.json()["requiredApprovals"] == 0
    approved = approve(client, cara, request_id, request_etag)
    assert approved.status_code == 200 and [a["userId"] for a in approved.json()["approvals"]] == [
        me(client, cara)
    ]
    merged = merge(client, ada, request_id, approved.headers["ETag"], {"expectedNoteETag": etag})
    assert (
        merged.status_code == 200
        and merged.json()["editRequest"]["approvals"] == approved.json()["approvals"]
    )
    # A single-owner note: the author approves a proposal and the record survives the merge, frozen.
    solo_id, solo_etag = runbook(client, ada)
    share(client, ada, solo_id, user(client, ben), "propose_edit")
    solo = submit(
        client,
        ben,
        solo_id,
        {"baseNoteETag": solo_etag, "proposedContent": {"title": RUNBOOK["title"], "body": ROLLBACK}},
    )
    recorded = approve(client, ada, solo.json()["id"], solo.headers["ETag"])
    assert recorded.status_code == 200 and recorded.json()["requiredApprovals"] == 0
    landed = merge(client, ada, solo.json()["id"], recorded.headers["ETag"], {"expectedNoteETag": solo_etag})
    assert landed.status_code == 200
    assert [a["userId"] for a in landed.json()["editRequest"]["approvals"]] == [me(client, ada)]


def test_a_content_revision_deletes_approvals_and_an_explanation_only_one_keeps_them(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara)
    share(client, ada, note_id, user(client, ben), "propose_edit")
    theirs = submit(client, ben, note_id, proposal(etag))
    request_id = theirs.json()["id"]
    approved = approve(client, cara, request_id, theirs.headers["ETag"])
    explained = revise(client, ben, request_id, approved.headers["ETag"], {"explanation": "clarified"})
    assert explained.status_code == 200 and [a["userId"] for a in explained.json()["approvals"]] == [
        me(client, cara)
    ]
    changed = revise(
        client,
        ben,
        request_id,
        explained.headers["ETag"],
        {"proposedContent": {**explained.json()["proposedContent"], "title": "Release runbook"}},
    )
    assert changed.status_code == 200 and changed.json()["approvals"] == []
    again = approve(client, cara, request_id, changed.headers["ETag"])
    assert again.status_code == 200 and [a["userId"] for a in again.json()["approvals"]] == [me(client, cara)]


def test_approvals_render_in_approval_order(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, etag = owned_by(client, ada, cara, dan)
    share(client, ada, note_id, user(client, ben), "propose_edit")
    theirs = submit(client, ben, note_id, proposal(etag))
    request_id = theirs.json()["id"]
    clock.advance(timedelta(minutes=1))
    first = approve(client, dan, request_id, theirs.headers["ETag"])
    clock.advance(timedelta(minutes=1))
    second = approve(client, cara, request_id, first.headers["ETag"])
    assert [a["userId"] for a in second.json()["approvals"]] == [me(client, dan), me(client, cara)]
    assert [a["approvedAt"] for a in second.json()["approvals"]] == [
        "2026-09-13T12:01:00.000000Z",
        "2026-09-13T12:02:00.000000Z",
    ]
    assert second.json()["updatedAt"] == "2026-09-13T12:02:00.000000Z"
