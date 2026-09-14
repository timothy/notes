"""The design guide's section 5 walkthroughs, driven with the contract's own example payloads."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_notes import me


def test_create_and_share(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona, cara: Persona
) -> None:
    """Section 5, "Create and share": create, grant proposal-only access, comment once the grant allows it,
    widen it, revoke it. The independent team grant proves that revoking one share leaves other paths
    intact."""
    # 1. The owner sends POST /notes.
    created = client.post("/v1/notes", auth=ada, json=examples["CreateNoteRequest"])
    assert created.status_code == 201
    note_id = created.json()["id"]
    assert created.headers["Location"] == f"/v1/notes/{note_id}"
    assert created.json()["title"] == "Release checklist" and created.json()["tags"] == ["release"]
    etag = created.headers["ETag"]

    # 2. Proposal-only access for Ben, using the example with Ben's real id.
    grant = copy.deepcopy(examples["CreateShareRequest"])
    grant["recipient"]["id"] = me(client, ben)
    share = client.post(f"/v1/notes/{note_id}/shares", auth=ada, json=grant)
    assert share.status_code == 201
    assert share.json()["permissions"] == ["read", "propose_edit"]
    share_id = share.json()["id"]
    assert share.headers["Location"] == f"/v1/notes/{note_id}/shares/{share_id}"
    seen = client.get(f"/v1/notes/{note_id}", auth=ben)
    assert seen.status_code == 200 and seen.json()["effectivePermissions"] == ["read", "propose_edit"]
    # Ben can read existing comments but receives 403 if he tries to add one.
    assert client.get(f"/v1/notes/{note_id}/comments", auth=ben).json() == {"items": [], "nextCursor": None}
    refused = client.post(f"/v1/notes/{note_id}/comments", auth=ben, json=examples["CreateCommentRequest"])
    assert refused.status_code == 403

    # 3. Widening the grant replaces the set and returns the canonical three.
    widened = client.patch(
        f"/v1/notes/{note_id}/shares/{share_id}", auth=ada, json=examples["UpdateShareRequest"]
    )
    assert widened.status_code == 200
    assert widened.json()["permissions"] == ["read", "comment", "propose_edit"]
    assert client.get(f"/v1/notes/{note_id}", auth=ben).json()["effectivePermissions"] == [
        "read",
        "comment",
        "propose_edit",
    ]
    # A now-authorized recipient can POST to the note's /comments collection.
    posted = client.post(f"/v1/notes/{note_id}/comments", auth=ben, json=examples["CreateCommentRequest"])
    assert posted.status_code == 201 and posted.json()["body"] == examples["CreateCommentRequest"]["body"]
    assert posted.headers["Location"] == f"/v1/notes/{note_id}/comments/{posted.json()['id']}"

    # 4. An independent team grant, then revocation of the direct share: the team path continues to apply.
    team_id = client.post("/v1/teams", auth=cara, json={"name": "Reviewers"}).json()["id"]
    client.post(f"/v1/teams/{team_id}/members", auth=cara, json={"userId": me(client, ben)})
    client.post(
        f"/v1/notes/{note_id}/shares",
        auth=ada,
        json={"recipient": {"type": "team", "id": team_id}, "permissions": ["read"]},
    )
    assert client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 204
    assert client.get(f"/v1/notes/{note_id}", auth=ben).json()["effectivePermissions"] == ["read"]

    # Sharing and commenting changed nothing about the note itself.
    unchanged = client.get(f"/v1/notes/{note_id}", auth=ada)
    assert unchanged.headers["ETag"] == etag and unchanged.json()["updatedAt"] == created.json()["updatedAt"]


GUIDE_DIFF = (
    "--- base/body\n"
    "+++ proposed/body\n"
    "@@ -1,4 +1,5 @@\n"
    " ## Release\n"
    " \n"
    " - Run tests\n"
    "+- Review metrics\n"
    " - Deploy\n"
)


def test_submit_and_discover_an_edit_request(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    """Section 5, "Submit and discover an edit request": the proposer submits against the note's ETag, the
    note is unchanged, and the owner finds the request in the inbox and reads its diff, the guide's diff."""
    created = client.post("/v1/notes", auth=ada, json=examples["CreateNoteRequest"])
    note_id, note_etag = created.json()["id"], created.headers["ETag"]
    grant = copy.deepcopy(examples["CreateShareRequest"])
    grant["recipient"]["id"] = me(client, ben)
    assert client.post(f"/v1/notes/{note_id}/shares", auth=ada, json=grant).status_code == 201

    # 1. After reading the note and its ETag, the proposer sends the example with that ETag.
    submission = copy.deepcopy(examples["CreateEditRequestRequest"])
    submission["baseNoteETag"] = client.get(f"/v1/notes/{note_id}", auth=ben).headers["ETag"]
    submitted = client.post(f"/v1/notes/{note_id}/edit-requests", auth=ben, json=submission)
    assert submitted.status_code == 201, submitted.text
    request_id, request_etag = submitted.json()["id"], submitted.headers["ETag"]
    assert submitted.headers["Location"] == f"/v1/edit-requests/{request_id}"
    assert submitted.json()["status"] == "open" and submitted.json()["requiredApprovals"] == 0
    # The live note still has its ETag and its original body.
    live = client.get(f"/v1/notes/{note_id}", auth=ada)
    assert live.headers["ETag"] == note_etag and live.json()["body"] == examples["CreateNoteRequest"]["body"]

    # 2. The owner polls the inbox, then reads the individual request.
    inbox = client.get(
        "/v1/edit-requests", auth=ada, params={"view": "incoming", "status": "open", "state": "active"}
    )
    assert inbox.status_code == 200
    assert [item["id"] for item in inbox.json()["items"]] == [request_id]
    assert inbox.json()["items"][0]["noteTitle"] == "Release checklist"
    detail = client.get(f"/v1/edit-requests/{request_id}", auth=ada)
    assert detail.status_code == 200 and detail.headers["ETag"] == request_etag
    assert detail.json()["proposalDiff"] == {"title": "", "body": GUIDE_DIFF}
    assert detail.json()["proposalDiff"] == examples["EditRequestOpen"]["proposalDiff"]
    assert detail.json()["baseContent"]["body"] == examples["CreateNoteRequest"]["body"]
    assert detail.json()["proposedContent"] == submission["proposedContent"]
    # The proposer sees the same request; another reader of the note would not.
    assert client.get(f"/v1/edit-requests/{request_id}", auth=ben).json() == detail.json()


def test_preview_and_merge_unchanged_or_with_owner_edits(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    """Section 5, "Preview and merge unchanged or with owner edits": the two alternative merge paths, each on
    its own submission, then the contract's conflict chain (EditRequestConflicting, PreviewConflict,
    ProblemMergeConflict, PreviewConflictResolved) resolved by complete finalContent."""

    def submitted() -> tuple[str, str, str, str]:
        created = client.post("/v1/notes", auth=ada, json=examples["CreateNoteRequest"])
        note_id, note_etag = str(created.json()["id"]), str(created.headers["ETag"])
        grant = copy.deepcopy(examples["CreateShareRequest"])
        grant["recipient"]["id"] = me(client, ben)
        assert client.post(f"/v1/notes/{note_id}/shares", auth=ada, json=grant).status_code == 201
        body = copy.deepcopy(examples["CreateEditRequestRequest"])
        body["baseNoteETag"] = note_etag
        response = client.post(f"/v1/notes/{note_id}/edit-requests", auth=ben, json=body)
        assert response.status_code == 201, response.text
        return note_id, note_etag, str(response.json()["id"]), str(response.headers["ETag"])

    proposed = examples["CreateEditRequestRequest"]["proposedContent"]

    # Path A: preview without a body, then merge the candidate unchanged with the versions it reported.
    note_id, note_etag, request_id, request_etag = submitted()
    clean = client.post(f"/v1/edit-requests/{request_id}/preview", auth=ada)
    assert clean.status_code == 200, clean.text
    assert clean.json() == {
        **examples["PreviewClean"],
        "requestETag": request_etag,
        "currentNoteETag": note_etag,
    }
    plain = {**examples["MergeEditRequestPlain"], "expectedNoteETag": clean.json()["currentNoteETag"]}
    merged = client.post(
        f"/v1/edit-requests/{request_id}/merge", auth=ada, if_match=clean.json()["requestETag"], json=plain
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["note"]["body"] == proposed["body"] and merged.json()["note"]["tags"] == ["release"]
    assert merged.json()["editRequest"]["status"] == "merged"
    assert merged.headers["ETag"] == client.get(f"/v1/edit-requests/{request_id}", auth=ada).headers["ETag"]

    # Path B: preview the owner's own edits, then merge with the versions returned by that preview.
    note_id, note_etag, request_id, request_etag = submitted()
    refined = client.post(
        f"/v1/edit-requests/{request_id}/preview", auth=ada, json=examples["PreviewEditRequestRequest"]
    )
    assert refined.status_code == 200, refined.text
    assert refined.json() == {
        **examples["PreviewWithFinalContent"],
        "requestETag": request_etag,
        "currentNoteETag": note_etag,
    }
    with_edits = copy.deepcopy(examples["MergeEditRequestWithFinalContent"])
    with_edits["expectedNoteETag"] = refined.json()["currentNoteETag"]
    merged = client.post(
        f"/v1/edit-requests/{request_id}/merge",
        auth=ada,
        if_match=refined.json()["requestETag"],
        json=with_edits,
    )
    assert merged.status_code == 200, merged.text
    result = merged.json()
    assert result["note"]["body"] == with_edits["finalContent"]["body"]  # "Review metrics and error rates"
    assert result["editRequest"]["proposedContent"] == proposed  # the proposal still says "Review metrics"
    assert (
        result["editRequest"]["mergeRecord"]["content"]
        == examples["EditRequestMerged"]["mergeRecord"]["content"]
    )
    assert result["note"]["tags"] == ["release"]
    note_v2 = result["noteETag"]

    # The conflict chain: Ben submits against note-v2 while Ada moves the note on to note-v3.
    conflicting = examples["EditRequestConflicting"]
    assert result["note"]["body"] == conflicting["baseContent"]["body"]  # note-v2 is the example's base
    second = client.post(
        f"/v1/notes/{note_id}/edit-requests",
        auth=ben,
        json={
            "baseNoteETag": note_v2,
            "proposedContent": conflicting["proposedContent"],
            "explanation": conflicting["explanation"],
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["proposalDiff"] == conflicting["proposalDiff"]
    second_id, second_etag = second.json()["id"], second.headers["ETag"]
    v3 = client.patch(
        f"/v1/notes/{note_id}",
        auth=ada,
        if_match=note_v2,
        json=examples["EditRequestWithdrawn"]["baseContent"],
    )
    assert v3.status_code == 200, v3.text
    conflict = client.post(f"/v1/edit-requests/{second_id}/preview", auth=ada)
    assert conflict.json() == {
        **examples["PreviewConflict"],
        "requestETag": second_etag,
        "currentNoteETag": v3.headers["ETag"],
    }
    blocked = client.post(
        f"/v1/edit-requests/{second_id}/merge",
        auth=ada,
        if_match=second_etag,
        json={"expectedNoteETag": v3.headers["ETag"]},
    )
    assert blocked.status_code == 409 and blocked.json() == examples["ProblemMergeConflict"]
    resolution = {"finalContent": examples["PreviewConflictResolved"]["candidate"]}
    resolved = client.post(f"/v1/edit-requests/{second_id}/preview", auth=ada, json=resolution)
    assert resolved.json() == {
        **examples["PreviewConflictResolved"],
        "requestETag": second_etag,
        "currentNoteETag": v3.headers["ETag"],
    }
    landed = client.post(
        f"/v1/edit-requests/{second_id}/merge",
        auth=ada,
        if_match=second_etag,
        json={"expectedNoteETag": v3.headers["ETag"], **resolution},
    )
    assert landed.status_code == 200, landed.text
    assert landed.json()["note"]["title"] == "Release runbook (2026)"
    assert landed.json()["note"]["body"] == resolution["finalContent"]["body"]
    assert landed.json()["editRequest"]["proposedContent"] == conflicting["proposedContent"]
    assert landed.json()["editRequest"]["mergeRecord"]["content"] == resolution["finalContent"]
    assert landed.json()["note"]["tags"] == ["release"]


def test_protect_a_note_and_merge_with_peer_approval(
    client: ContractClient,
    clock: FakeClock,
    examples: dict[str, Any],
    ada: Persona,
    ben: Persona,
    cara: Persona,
) -> None:
    """Section 5, "Protect a note and merge with peer approval": Ada adds Cara, requires one peer approval,
    is refused a direct edit, proposes instead, Cara approves, Ada merges; then the guide's alternatives and
    Cara's departure. Cara's request comment waits for slice 12. Every response equals the contract's example
    once ids, versions, and timestamps are substituted."""
    ada_id, ben_id, cara_id = me(client, ada), me(client, ben), me(client, cara)
    protected = examples["NoteRunbookProtected"]

    def stamp(minutes: int) -> str:
        return f"2026-09-13T{12 + minutes // 60:02d}:{minutes % 60:02d}:00.000000Z"

    def note_like(example: dict[str, Any], note_id: str, owners: list[str], updated: str) -> dict[str, Any]:
        return {
            **example,
            "id": note_id,
            "authorId": ada_id,
            "ownerIds": owners,
            "createdAt": stamp(0),
            "updatedAt": updated,
        }

    # 1. Ada creates the runbook (runbook-v1): one owner, the default self_merge policy.
    created = client.post(
        "/v1/notes",
        auth=ada,
        json={"title": protected["title"], "body": protected["body"], "tags": protected["tags"]},
    )
    assert created.status_code == 201
    note_id, v1 = created.json()["id"], created.headers["ETag"]
    assert created.json()["ownerIds"] == [ada_id] and created.json()["reviewPolicy"]["mode"] == "self_merge"

    # 2. She adds Cara: two owners, the note is now protected (runbook-v2).
    clock.advance(timedelta(minutes=10))
    added = client.post(
        f"/v1/notes/{note_id}/owners",
        auth=ada,
        if_match=v1,
        json={**examples["AddOwnerRequest"], "userId": cara_id},
    )
    assert added.status_code == 200, added.text
    v2 = added.headers["ETag"]
    assert added.json() == note_like(protected, note_id, [ada_id, cara_id], stamp(10))

    # 3. She requires one peer approval (runbook-v3).
    clock.advance(timedelta(minutes=10))
    policy = client.patch(
        f"/v1/notes/{note_id}/review-policy",
        auth=ada,
        if_match=v2,
        json=examples["ReviewPolicyPeerApprovalRequest"],
    )
    assert policy.status_code == 200, policy.text
    v3 = policy.headers["ETag"]
    assert policy.json() == note_like(
        examples["NoteRunbookPeerApproval"], note_id, [ada_id, cara_id], stamp(20)
    )

    # 4. A direct edit is refused; Ada submits an edit request instead (proposal-v1).
    awaiting = examples["EditRequestAwaitingApproval"]
    refused = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match=v3, json={"body": awaiting["proposedContent"]["body"]}
    )
    assert refused.status_code == 409 and refused.json()["code"] == "direct_edit_not_allowed"
    clock.advance(timedelta(minutes=40))
    submitted = client.post(
        f"/v1/notes/{note_id}/edit-requests",
        auth=ada,
        json={
            "baseNoteETag": v3,
            "proposedContent": awaiting["proposedContent"],
            "explanation": awaiting["explanation"],
        },
    )
    assert submitted.status_code == 201, submitted.text
    request_id, p1 = submitted.json()["id"], submitted.headers["ETag"]
    request_like = {"id": request_id, "noteId": note_id, "proposerId": ada_id, "createdAt": stamp(60)}
    assert submitted.json() == {**awaiting, **request_like, "updatedAt": stamp(60)}
    # Cara sees it in her incoming inbox.
    inbox = client.get("/v1/edit-requests", auth=cara)
    assert [item["id"] for item in inbox.json()["items"]] == [request_id]
    assert inbox.json()["items"][0]["requiredApprovals"] == 1

    # 5. Before the approval Ada can neither merge nor supply finalContent.
    early = client.post(
        f"/v1/edit-requests/{request_id}/merge", auth=ada, if_match=p1, json={"expectedNoteETag": v3}
    )
    assert early.status_code == 409 and early.json()["code"] == "approval_required"
    with_edits = client.post(
        f"/v1/edit-requests/{request_id}/merge",
        auth=ada,
        if_match=p1,
        json={"expectedNoteETag": v3, "finalContent": awaiting["proposedContent"]},
    )
    assert with_edits.status_code == 422 and with_edits.json() == examples["ProblemFinalContentNotAllowed"]

    # 6. Cara approves (proposal-v2).
    clock.advance(timedelta(minutes=10))
    approved = client.post(f"/v1/edit-requests/{request_id}/approve", auth=cara, if_match=p1)
    assert approved.status_code == 200, approved.text
    p2 = approved.headers["ETag"]
    assert approved.json() == {
        **examples["EditRequestApproved"],
        **request_like,
        "updatedAt": stamp(70),
        "approvals": [{"userId": cara_id, "approvedAt": stamp(70)}],
    }

    # 7. Ada previews and merges with the versions the preview reports.
    previewed = client.post(f"/v1/edit-requests/{request_id}/preview", auth=ada).json()
    assert previewed["requestETag"] == p2 and previewed["currentNoteETag"] == v3 and previewed["canMerge"]
    clock.advance(timedelta(minutes=50))
    merged = client.post(
        f"/v1/edit-requests/{request_id}/merge",
        auth=ada,
        if_match=previewed["requestETag"],
        json={"expectedNoteETag": previewed["currentNoteETag"]},
    )
    assert merged.status_code == 200, merged.text
    result, example = merged.json(), examples["MergeResultPeerApproval"]
    v4 = result["noteETag"]
    assert result["note"] == note_like(example["note"], note_id, [ada_id, cara_id], stamp(120))
    assert result["editRequest"] == {
        **example["editRequest"],
        **request_like,
        "updatedAt": stamp(120),
        "closedAt": stamp(120),
        "approvals": [{"userId": cara_id, "approvedAt": stamp(70)}],
        "mergeRecord": {
            **example["editRequest"]["mergeRecord"],
            "noteETag": v4,
            "mergedBy": ada_id,
            "mergedAt": stamp(120),
        },
    }
    assert merged.headers["ETag"] == client.get(f"/v1/edit-requests/{request_id}", auth=ada).headers["ETag"]

    # 8. Alternatively, on a second runbook, Cara merges Ada's proposal directly: her merge is the approval.
    other = client.post(
        "/v1/notes",
        auth=ada,
        json={"title": protected["title"], "body": protected["body"], "tags": protected["tags"]},
    )
    other_id = other.json()["id"]
    o2 = client.post(
        f"/v1/notes/{other_id}/owners", auth=ada, if_match=other.headers["ETag"], json={"userId": cara_id}
    ).headers["ETag"]
    o3 = client.patch(
        f"/v1/notes/{other_id}/review-policy",
        auth=ada,
        if_match=o2,
        json=examples["ReviewPolicyPeerApprovalRequest"],
    ).headers["ETag"]
    proposed = client.post(
        f"/v1/notes/{other_id}/edit-requests",
        auth=ada,
        json={"baseNoteETag": o3, "proposedContent": awaiting["proposedContent"]},
    )
    direct = client.post(
        f"/v1/edit-requests/{proposed.json()['id']}/merge",
        auth=cara,
        if_match=proposed.headers["ETag"],
        json={"expectedNoteETag": o3},
    )
    assert direct.status_code == 200, direct.text
    assert (
        direct.json()["editRequest"]["approvals"] == []
        and direct.json()["editRequest"]["mergeRecord"]["mergedBy"] == cara_id
    )

    # 9. Had Ben proposed the same change, two distinct owners would have to take part.
    grant = copy.deepcopy(examples["CreateShareRequest"])
    grant["recipient"]["id"] = ben_id
    assert client.post(f"/v1/notes/{note_id}/shares", auth=ada, json=grant).status_code == 201
    bens = client.post(
        f"/v1/notes/{note_id}/edit-requests",
        auth=ben,
        json={
            "baseNoteETag": v4,
            "proposedContent": {**awaiting["proposedContent"], "title": "Incident runbook (draft)"},
        },
    )
    assert bens.status_code == 201 and bens.json()["requiredApprovals"] == 2

    # 10. Cara removes herself (runbook-v5): one owner again, the stored policy persists, direct edits resume.
    clock.advance(timedelta(minutes=60))
    left = client.delete(f"/v1/notes/{note_id}/owners/{cara_id}", auth=cara, if_match=v4)
    assert left.status_code == 200, left.text
    assert left.json()["isOwner"] is False and left.json()["ownerIds"] == [ada_id]
    after = client.get(f"/v1/notes/{note_id}", auth=ada)
    assert after.json() == note_like(examples["NoteRunbookAfterOwnerLeft"], note_id, [ada_id], stamp(180))
    assert client.get(f"/v1/edit-requests/{bens.json()['id']}", auth=ada).json()["requiredApprovals"] == 0
    edited = client.patch(
        f"/v1/notes/{note_id}",
        auth=ada,
        if_match=after.headers["ETag"],
        json={"title": "Incident runbook (2026)"},
    )
    assert edited.status_code == 200
