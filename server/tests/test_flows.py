"""The design guide's section 5 walkthroughs, driven with the contract's own example payloads."""

from __future__ import annotations

import copy
from typing import Any

from tests.contract_client import ContractClient
from tests.support import Persona
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
