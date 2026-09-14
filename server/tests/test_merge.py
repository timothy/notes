"""Acceptance row "Atomicity/errors" for merges: "Invalid final content, stale versions, missing
preconditions, conflicts, and failed authorization leave note/request unchanged and use the documented
Problem Details status and `code`. No private content leaks in errors." Plus the merge half of "Owner
adjustments" ("Merged content and attribution are stored separately from the submitted proposal. Current
tags are preserved.") and the counting rules of "Approvals" that PR 5's endpoints will exercise further."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from notes_api.models import EditRequest, Note, NoteOwner
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_edit_requests import (
    STALE,
    get_request,
    proposal,
    proposer_note,
    seed_approvals,
    submit,
    withdraw,
)
from tests.test_notes import errors, me
from tests.test_preview import conflicting_example, preview, submitted_example
from tests.test_shares import share, user

pytestmark = pytest.mark.acceptance("Atomicity/errors")

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
JSON = {"Content-Type": "application/json"}


def merge(
    client: ContractClient,
    persona: Persona,
    request_id: str,
    etag: str | None,
    body: Any = None,
    **kwargs: Any,
) -> httpx.Response:
    return client.post(
        f"/v1/edit-requests/{request_id}/merge", auth=persona, if_match=etag, json=body, **kwargs
    )


def row_values(session: Session, model: type[Any], row_id: str) -> dict[str, Any]:
    row = session.get(model, uuid.UUID(row_id))
    assert row is not None
    return {attr.key: getattr(row, attr.key) for attr in sa_inspect(model).column_attrs}


def snapshot(app: FastAPI, note_id: str, request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Every stored column of the note and the request, for byte-identical comparisons."""
    with app.state.session_factory() as session:
        return row_values(session, Note, note_id), row_values(session, EditRequest, request_id)


def protect(app: FastAPI, client: ContractClient, note_id: str, co_owner: Persona, policy: int = 1) -> None:
    """Seed a second owner and a peer_approval policy, the way PR 5's endpoints will set them."""
    with app.state.session_factory() as session, session.begin():
        owner_id = uuid.UUID(me(client, co_owner))
        session.add(NoteOwner(note_id=uuid.UUID(note_id), user_id=owner_id, position=1, added_at=START))
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.review_mode, note.review_required_approvals = "peer_approval", policy


def test_merging_the_clean_candidate_updates_the_note_and_closes_the_request(
    client: ContractClient, clock: FakeClock, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    clock.advance(timedelta(hours=1))
    body = {**examples["MergeEditRequestPlain"], "expectedNoteETag": note_etag}
    merged = merge(client, ada, request_id, request_etag, body)
    assert merged.status_code == 200, merged.text
    result = merged.json()
    proposed = examples["CreateEditRequestRequest"]["proposedContent"]
    assert result["note"]["title"] == proposed["title"] and result["note"]["body"] == proposed["body"]
    assert result["note"]["tags"] == ["release"] and result["note"]["ownerIds"] == [me(client, ada)]
    assert result["note"]["isOwner"] is True and result["note"]["updatedAt"] == "2026-09-13T13:00:00.000000Z"
    assert result["noteETag"] != note_etag
    request = result["editRequest"]
    assert request["status"] == "merged" and request["closedAt"] == "2026-09-13T13:00:00.000000Z"
    assert request["requiredApprovals"] == 0 and request["approvals"] == []
    assert request["mergeRecord"] == {
        "content": proposed,
        "noteETag": result["noteETag"],
        "mergedBy": me(client, ada),
        "mergedAt": "2026-09-13T13:00:00.000000Z",
    }
    assert request["proposedContent"] == proposed  # preserved
    assert request["proposalDiff"] == examples["EditRequestOpen"]["proposalDiff"]
    assert request["rejectedBy"] is None and request["rejectionReason"] is None
    assert merged.headers["ETag"] != request_etag
    # The HTTP ETag is the request's; the note and the request read back exactly as returned.
    seen = get_request(client, ada, request_id)
    assert seen.headers["ETag"] == merged.headers["ETag"] and seen.json() == request
    live = client.get(f"/v1/notes/{note_id}", auth=ada)
    assert live.headers["ETag"] == result["noteETag"] and live.json() == result["note"]
    assert get_request(client, ben, request_id).json()["status"] == "merged"  # the proposer sees the outcome


def test_final_content_is_stored_in_the_note_and_the_record_but_never_in_the_proposal(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    body = {**copy.deepcopy(examples["MergeEditRequestWithFinalContent"]), "expectedNoteETag": note_etag}
    merged = merge(client, ada, request_id, request_etag, body)
    assert merged.status_code == 200, merged.text
    final = body["finalContent"]
    assert merged.json()["note"]["body"] == final["body"]
    request = merged.json()["editRequest"]
    assert (
        request["mergeRecord"]["content"] == final == examples["EditRequestMerged"]["mergeRecord"]["content"]
    )
    assert request["proposedContent"]["body"].endswith("- Review metrics\n- Deploy\n")  # still the proposal
    assert request["proposalDiff"] == examples["EditRequestMerged"]["proposalDiff"]
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json()["tags"] == ["release"]


def test_a_candidate_equal_to_the_live_content_still_advances_the_note(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id, note_etag = proposer_note(client, ada, ben)
    submitted = submit(client, ben, note_id, proposal(note_etag))
    request_id, request_etag = submitted.json()["id"], submitted.headers["ETag"]
    caught_up = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match=note_etag, json=submitted.json()["proposedContent"]
    )
    assert caught_up.status_code == 200
    ahead = preview(client, ada, request_id).json()
    assert ahead["canMerge"] is True and ahead["mergeDiff"] == {"title": "", "body": ""}
    clock.advance(timedelta(minutes=1))
    merged = merge(client, ada, request_id, request_etag, {"expectedNoteETag": caught_up.headers["ETag"]})
    assert merged.status_code == 200, merged.text
    assert merged.json()["noteETag"] != caught_up.headers["ETag"]
    assert merged.json()["note"]["updatedAt"] == "2026-09-13T12:01:00.000000Z"
    assert merged.json()["editRequest"]["mergeRecord"]["noteETag"] == merged.json()["noteETag"]


def test_conflicts_block_the_automatic_merge_and_final_content_resolves_them(
    client: ContractClient, app: FastAPI, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, request_etag = conflicting_example(client, examples, ada, ben)
    before = snapshot(app, note_id, request_id)
    blocked = merge(client, ada, request_id, request_etag, {"expectedNoteETag": note_etag})
    assert blocked.status_code == 409 and blocked.json() == examples["ProblemMergeConflict"]
    assert snapshot(app, note_id, request_id) == before
    candidate = examples["PreviewConflictResolved"]["candidate"]
    merged = merge(
        client, ada, request_id, request_etag, {"expectedNoteETag": note_etag, "finalContent": candidate}
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["note"]["title"] == candidate["title"]
    assert merged.json()["note"]["body"] == candidate["body"]
    assert merged.json()["editRequest"]["mergeRecord"]["content"] == candidate
    assert (
        merged.json()["editRequest"]["proposedContent"]
        == examples["EditRequestConflicting"]["proposedContent"]
    )


def test_every_failing_check_leaves_both_rows_untouched(
    client: ContractClient,
    app: FastAPI,
    examples: dict[str, Any],
    ada: Persona,
    ben: Persona,
    cara: Persona,
    dan: Persona,
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    share(client, ada, note_id, user(client, cara), "comment", "propose_edit")
    before = snapshot(app, note_id, request_id)
    good = {"expectedNoteETag": note_etag}
    url = f"/v1/edit-requests/{request_id}/merge"
    checks: list[tuple[httpx.Response, int]] = [
        (client.post(url, json=good), 401),
        (merge(client, dan, request_id, request_etag, good), 404),
        (merge(client, cara, request_id, request_etag, good), 404),
        (merge(client, ben, request_id, request_etag, good), 403),
        (merge(client, ben, request_id, None, good), 403),  # 403 precedes 428
        (merge(client, ada, request_id, None, good), 428),
        (merge(client, ada, request_id, 'W/"x"', good), 400),
        (merge(client, ada, request_id, STALE, good), 412),
        (merge(client, ada, request_id, request_etag, {"expectedNoteETag": STALE}), 412),
        (merge(client, ada, request_id, request_etag, {}), 422),
        (merge(client, ada, request_id, request_etag, {"expectedNoteETag": "note-v1"}), 422),
        (merge(client, ada, request_id, request_etag, {**good, "finalContent": {"title": "t"}}), 422),
        (merge(client, ada, request_id, request_etag, {**good, "tags": []}), 422),
        (merge(client, ada, request_id, None, {}), 422),  # body shape precedes 428
        (
            client.post(
                url, auth=ada, headers={"If-Match": request_etag, "Content-Type": "text/plain"}, content="x"
            ),
            415,
        ),
        (client.post(url, auth=ada, headers={"If-Match": request_etag, **JSON}, content="{"), 400),
    ]
    for response, status in checks:
        assert response.status_code == status, (status, response.text)
    assert errors(merge(client, ada, request_id, request_etag, {})) == [("body", "/expectedNoteETag")]
    assert errors(merge(client, ada, request_id, request_etag, {**good, "finalContent": {"title": "t"}})) == [
        ("body", "/finalContent/body")
    ]
    stale_note = merge(client, ada, request_id, request_etag, {"expectedNoteETag": STALE})
    assert "expectedNoteETag" in stale_note.json()["detail"]
    both_stale = merge(client, ada, request_id, STALE, {"expectedNoteETag": STALE})
    assert both_stale.status_code == 412 and "expectedNoteETag" not in both_stale.json()["detail"]
    assert snapshot(app, note_id, request_id) == before
    assert get_request(client, ada, request_id).json()["status"] == "open"


def test_closed_requests_and_trashed_notes_cannot_be_merged(
    client: ContractClient, app: FastAPI, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    other = submit(client, ben, note_id, proposal(note_etag, explanation="other"))
    other_id, other_etag = other.json()["id"], other.headers["ETag"]
    withdrawn = withdraw(client, ben, request_id, request_etag)
    assert withdrawn.status_code == 200
    old = merge(client, ada, request_id, request_etag, {"expectedNoteETag": note_etag})
    assert old.status_code == 412
    current = merge(client, ada, request_id, withdrawn.headers["ETag"], {"expectedNoteETag": note_etag})
    assert current.status_code == 409 and current.json()["code"] == "request_not_open"
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=note_etag).headers["ETag"]
    before = snapshot(app, note_id, other_id)
    frozen = merge(client, ada, other_id, other_etag, {"expectedNoteETag": trash_etag})
    assert frozen.status_code == 409 and frozen.json()["code"] == "note_not_active"
    assert merge(client, ada, other_id, other_etag, {"expectedNoteETag": note_etag}).status_code == 412
    assert snapshot(app, note_id, other_id) == before
    restored = client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag)
    merged = merge(client, ada, other_id, other_etag, {"expectedNoteETag": restored.headers["ETag"]})
    assert merged.status_code == 200, merged.text


def test_repeating_a_merge_with_the_old_etag_cannot_merge_twice(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    body = {"expectedNoteETag": note_etag}
    first = merge(client, ada, request_id, request_etag, body)
    assert first.status_code == 200
    assert merge(client, ada, request_id, request_etag, body).status_code == 412
    again = merge(
        client, ada, request_id, first.headers["ETag"], {"expectedNoteETag": first.json()["noteETag"]}
    )
    assert again.status_code == 409 and again.json()["code"] == "request_not_open"
    live = client.get(f"/v1/notes/{note_id}", auth=ada)
    assert live.headers["ETag"] == first.json()["noteETag"]  # the lost-response reader finds the merge record
    assert (
        get_request(client, ada, request_id).json()["mergeRecord"]
        == first.json()["editRequest"]["mergeRecord"]
    )


@pytest.mark.acceptance("Approvals")
def test_peer_approval_counts_stored_approvals_plus_a_non_proposing_merger(
    client: ContractClient,
    app: FastAPI,
    examples: dict[str, Any],
    ada: Persona,
    ben: Persona,
    cara: Persona,
    dan: Persona,
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    protect(app, client, note_id, cara)  # ada and cara own it; ben's proposal needs two distinct owners
    assert get_request(client, ada, request_id).json()["requiredApprovals"] == 2
    before = snapshot(app, note_id, request_id)
    alone = merge(client, ada, request_id, request_etag, {"expectedNoteETag": note_etag})
    assert alone.status_code == 409 and alone.json() == examples["ProblemApprovalRequired"]
    assert snapshot(app, note_id, request_id) == before
    # Approvals by the proposer or by a non-owner do not count.
    seed_approvals(app, request_id, me(client, ben), me(client, dan))
    assert merge(client, ada, request_id, request_etag, {"expectedNoteETag": note_etag}).status_code == 409
    # finalContent is refused before the count is even taken.
    refused = merge(
        client,
        ada,
        request_id,
        request_etag,
        {
            "expectedNoteETag": note_etag,
            "finalContent": examples["PreviewEditRequestRequest"]["finalContent"],
        },
    )
    assert refused.status_code == 422 and refused.json() == examples["ProblemFinalContentNotAllowed"]
    # cara's approval plus ada as the merger makes two.
    seed_approvals(app, request_id, me(client, cara))
    merged = merge(client, ada, request_id, request_etag, {"expectedNoteETag": note_etag})
    assert merged.status_code == 200, merged.text
    request = merged.json()["editRequest"]
    assert request["requiredApprovals"] == 2 and request["mergeRecord"]["mergedBy"] == me(client, ada)
    assert {entry["userId"] for entry in request["approvals"]} == {
        me(client, ben),
        me(client, dan),
        me(client, cara),
    }  # stored rows, frozen; the sort is proven in test_edit_requests
    # Closing froze the requirement and the list: a later policy change does not touch the record.
    with app.state.session_factory() as session, session.begin():
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.review_mode, note.review_required_approvals = "self_merge", None
    frozen = get_request(client, ada, request_id).json()
    assert frozen["requiredApprovals"] == 2 and len(frozen["approvals"]) == 3


@pytest.mark.acceptance("Approvals")
def test_an_owners_own_proposal_merges_with_one_peer_approval(
    client: ContractClient, app: FastAPI, ada: Persona, cara: Persona
) -> None:
    created = client.post(
        "/v1/notes",
        auth=ada,
        json={
            "title": "Incident runbook",
            "body": "## Incident\n\n- Page the on-call\n- Open a channel\n",
            "tags": ["ops"],
        },
    )
    note_id, note_etag = created.json()["id"], created.headers["ETag"]
    protect(app, client, note_id, cara)
    proposed = {
        "title": "Incident runbook",
        "body": "## Incident\n\n- Page the on-call\n- Open a channel\n"
        "- Roll back if the error rate doubles\n",
    }
    own = submit(
        client,
        ada,
        note_id,
        {"baseNoteETag": note_etag, "proposedContent": proposed, "explanation": "Add the rollback trigger."},
    )
    assert own.status_code == 201 and own.json()["requiredApprovals"] == 1  # min(1, owners - 1)
    request_id, request_etag = own.json()["id"], own.headers["ETag"]
    alone = merge(client, ada, request_id, request_etag, {"expectedNoteETag": note_etag})
    assert (
        alone.status_code == 409 and alone.json()["code"] == "approval_required"
    )  # the proposer adds nothing
    seed_approvals(app, request_id, me(client, cara))
    merged = merge(client, ada, request_id, request_etag, {"expectedNoteETag": note_etag})
    assert merged.status_code == 200, merged.text
    result = merged.json()
    assert result["note"]["body"] == proposed["body"] and result["note"]["reviewPolicy"] == {
        "mode": "peer_approval",
        "requiredApprovals": 1,
    }
    assert result["editRequest"]["requiredApprovals"] == 1
    assert [entry["userId"] for entry in result["editRequest"]["approvals"]] == [me(client, cara)]
    assert result["editRequest"]["mergeRecord"]["mergedBy"] == me(client, ada)
    # A co-owner merging a non-proposer's request under self_merge needs nothing.
    with app.state.session_factory() as session, session.begin():
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.review_mode, note.review_required_approvals = "self_merge", None
    second = submit(
        client,
        ada,
        note_id,
        {"baseNoteETag": result["noteETag"], "proposedContent": {**proposed, "title": "Incident runbook v2"}},
    )
    assert second.json()["requiredApprovals"] == 0
    assert (
        merge(
            client,
            cara,
            second.json()["id"],
            second.headers["ETag"],
            {"expectedNoteETag": result["noteETag"]},
        ).status_code
        == 200
    )
