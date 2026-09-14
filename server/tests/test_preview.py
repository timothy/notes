"""Acceptance row "Owner adjustments": "Preview has no side effects. Complete final content can refine a
clean proposal or resolve conflicts. Merged content and attribution are stored separately from the submitted
proposal. Current tags are preserved." The previews here reproduce the contract's own examples, byte for
byte, from the same texts; the merge half of the row is asserted in ``tests/test_merge.py``."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from notes_api.models import Note, NoteOwner
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_edit_requests import get_request, proposal, proposer_note, submit, withdraw
from tests.test_notes import create, errors, me
from tests.test_shares import share, user

pytestmark = pytest.mark.acceptance("Owner adjustments")

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
JSON = {"Content-Type": "application/json"}
PLAIN = {"Content-Type": "text/plain"}


def preview(
    client: ContractClient, persona: Persona, request_id: str, body: Any = None, **kwargs: Any
) -> httpx.Response:
    return client.post(f"/v1/edit-requests/{request_id}/preview", auth=persona, json=body, **kwargs)


def with_versions(example: dict[str, Any], request_etag: str, note_etag: str) -> dict[str, Any]:
    """The contract's example with its placeholder versions replaced by the live ones."""
    return {**copy.deepcopy(example), "requestETag": request_etag, "currentNoteETag": note_etag}


def submitted_example(
    client: ContractClient, examples: dict[str, Any], owner: Persona, proposer: Persona
) -> tuple[str, str, str, str]:
    """The guide's note and Ben's example request: note id, note ETag, request id, request ETag."""
    created = client.post("/v1/notes", auth=owner, json=examples["CreateNoteRequest"])
    note_id, note_etag = str(created.json()["id"]), str(created.headers["ETag"])
    assert share(client, owner, note_id, user(client, proposer), "propose_edit").status_code == 201
    body = copy.deepcopy(examples["CreateEditRequestRequest"])
    body["baseNoteETag"] = note_etag
    response = submit(client, proposer, note_id, body)
    assert response.status_code == 201, response.text
    return note_id, note_etag, str(response.json()["id"]), str(response.headers["ETag"])


def conflicting_example(
    client: ContractClient, examples: dict[str, Any], owner: Persona, proposer: Persona
) -> tuple[str, str, str, str]:
    """The contract's conflict scenario: a second request submitted against note-v2 while the owner moves
    the note on to note-v3, whose title and last line collide with the proposal."""
    note_id, note_etag, _, _ = submitted_example(client, examples, owner, proposer)
    conflicting = examples["EditRequestConflicting"]
    v2 = client.patch(
        f"/v1/notes/{note_id}",
        auth=owner,
        if_match=note_etag,
        json={"body": conflicting["baseContent"]["body"]},
    )
    assert v2.status_code == 200
    body = {"baseNoteETag": v2.headers["ETag"], "proposedContent": conflicting["proposedContent"]}
    second = submit(client, proposer, note_id, {**body, "explanation": conflicting["explanation"]})
    assert second.status_code == 201, second.text
    assert second.json()["proposalDiff"] == conflicting["proposalDiff"]
    v3 = client.patch(
        f"/v1/notes/{note_id}",
        auth=owner,
        if_match=v2.headers["ETag"],
        json={
            "title": examples["PreviewConflict"]["conflicts"][0]["current"],
            "body": conflicting["baseContent"]["body"].replace("- Deploy\n", "- Deploy to production\n"),
        },
    )
    assert v3.status_code == 200
    return note_id, str(v3.headers["ETag"]), str(second.json()["id"]), str(second.headers["ETag"])


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


def test_a_clean_preview_reproduces_the_contracts_example_and_changes_nothing(
    client: ContractClient, clock: FakeClock, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    before = versions(client, ada, note_id, request_id)
    clock.advance(timedelta(minutes=1))
    bodies: list[Any] = [None, {}]
    for body in bodies:
        response = preview(client, ada, request_id, body)
        assert response.status_code == 200, response.text
        assert response.json() == with_versions(examples["PreviewClean"], request_etag, note_etag)
        assert "ETag" not in response.headers
    assert versions(client, ada, note_id, request_id) == before  # no version, no timestamp moved


def test_final_content_refines_a_clean_proposal(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    _note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    response = preview(client, ada, request_id, examples["PreviewEditRequestRequest"])
    assert response.status_code == 200, response.text
    assert response.json() == with_versions(examples["PreviewWithFinalContent"], request_etag, note_etag)
    assert (
        get_request(client, ada, request_id)
        .json()["proposedContent"]["body"]
        .endswith("- Review metrics\n- Deploy\n")
    )


def test_conflicts_are_reported_in_a_200_and_final_content_resolves_them(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, request_etag = conflicting_example(client, examples, ada, ben)
    conflict = preview(client, ada, request_id)
    assert conflict.status_code == 200, conflict.text
    assert conflict.json() == with_versions(examples["PreviewConflict"], request_etag, note_etag)
    resolved = preview(
        client, ada, request_id, {"finalContent": examples["PreviewConflictResolved"]["candidate"]}
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json() == with_versions(examples["PreviewConflictResolved"], request_etag, note_etag)
    assert versions(client, ada, note_id, request_id)[0] == note_etag


def test_a_note_change_between_previews_moves_the_current_version_and_the_comparison(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    first = preview(client, ada, request_id).json()
    assert first["currentNoteETag"] == note_etag
    moved = client.patch(
        f"/v1/notes/{note_id}",
        auth=ada,
        if_match=note_etag,
        json={"body": "## Release\n\n- Run tests\n- Deploy\n- Announce in #releases\n"},
    )
    assert moved.status_code == 200
    second = preview(client, ada, request_id).json()
    assert second["requestETag"] == request_etag and second["currentNoteETag"] == moved.headers["ETag"]
    assert second["canMerge"] is True and second["conflicts"] == []
    assert (
        second["candidate"]["body"]
        == "## Release\n\n- Run tests\n- Review metrics\n- Deploy\n- Announce in #releases\n"
    )
    assert second["proposalDiff"] == first["proposalDiff"]  # base to proposed does not depend on the note
    assert second["mergeDiff"] != first["mergeDiff"] and "+- Review metrics\n" in second["mergeDiff"]["body"]


def test_only_owners_preview(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, _, request_id, _ = submitted_example(client, examples, ada, ben)
    share(client, ada, note_id, user(client, cara), "comment", "propose_edit")
    refused = preview(client, ben, request_id)
    assert refused.status_code == 403 and refused.json()["code"] == "forbidden"
    assert preview(client, cara, request_id).status_code == 404  # another reader cannot even see the request
    assert preview(client, dan, request_id).status_code == 404
    assert client.post(f"/v1/edit-requests/{request_id}/preview").status_code == 401
    assert preview(client, ada, str(uuid.uuid4())).status_code == 404
    assert errors(client.post("/v1/edit-requests/nope/preview", auth=ada)) == [("path", "requestId")]


def test_closed_requests_and_trashed_notes_cannot_be_previewed(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    other = submit(client, ben, note_id, proposal(note_etag, explanation="other"))
    assert withdraw(client, ben, request_id, request_etag).status_code == 200
    closed = preview(client, ada, request_id)
    assert closed.status_code == 409 and closed.json()["code"] == "request_not_open"
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=note_etag).headers["ETag"]
    frozen = preview(client, ada, other.json()["id"])
    assert frozen.status_code == 409 and frozen.json()["code"] == "note_not_active"
    assert preview(client, ada, request_id).json()["code"] == "request_not_open"  # closed wins over trashed
    assert preview(client, ben, other.json()["id"]).status_code == 404  # the share went with the trash
    assert client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag).status_code == 200
    assert preview(client, ada, other.json()["id"]).status_code == 200


def test_the_body_is_optional_and_validated(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    _, _, request_id, _ = submitted_example(client, examples, ada, ben)
    url = f"/v1/edit-requests/{request_id}/preview"
    assert client.post(url, auth=ada).status_code == 200  # omitted
    assert client.post(url, auth=ada, content=b"", headers=PLAIN).status_code == 200  # empty is omitted
    assert client.post(url, auth=ada, content=b"", headers={}).status_code == 200
    assert client.post(url, auth=ada, json={}).status_code == 200
    null = client.post(url, auth=ada, content="null", headers=JSON)
    assert null.status_code == 422 and errors(null) == [("body", "")]
    assert errors(preview(client, ada, request_id, {"finalContent": {"title": "t"}})) == [
        ("body", "/finalContent/body")
    ]
    assert errors(preview(client, ada, request_id, {"finalContent": None})) == [("body", "/finalContent")]
    assert errors(preview(client, ada, request_id, {"expectedNoteETag": '"x"'})) == [
        ("body", "/expectedNoteETag")
    ]
    assert errors(preview(client, ada, request_id, {"finalContent": {"title": "   ", "body": "b"}})) == [
        ("body", "/finalContent/title")
    ]
    assert client.post(url, auth=ada, content="x", headers=PLAIN).status_code == 415
    assert client.post(url, auth=ada, content="{", headers=JSON).status_code == 400


def test_final_content_is_refused_under_peer_approval_on_a_protected_note(
    client: ContractClient, app: FastAPI, examples: dict[str, Any], ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id, _note_etag, request_id, _request_etag = submitted_example(client, examples, ada, ben)
    with app.state.session_factory() as session, session.begin():
        session.add(
            NoteOwner(
                note_id=uuid.UUID(note_id), user_id=uuid.UUID(me(client, cara)), position=1, added_at=START
            )
        )
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.review_mode, note.review_required_approvals = "peer_approval", 1
    refused = preview(client, ada, request_id, examples["PreviewEditRequestRequest"])
    assert refused.status_code == 422 and refused.json() == examples["ProblemFinalContentNotAllowed"]
    candidate = preview(client, ada, request_id).json()["candidate"]
    assert preview(client, ada, request_id, {"finalContent": candidate}).status_code == 422  # even the same
    automatic = preview(client, cara, request_id)  # the new owner previews the automatic merge
    assert automatic.status_code == 200 and automatic.json()["canMerge"] is True
    with app.state.session_factory() as session, session.begin():
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.review_mode, note.review_required_approvals = "self_merge", None
    accepted = preview(client, ada, request_id, examples["PreviewEditRequestRequest"])
    assert (
        accepted.status_code == 200 and accepted.json()["usedFinalContent"] is True
    )  # protected, self_merge


def test_a_single_owner_note_accepts_final_content_under_any_stored_policy(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    note_id, note_etag = proposer_note(client, ada, ben)
    request = submit(client, ben, note_id, proposal(note_etag))
    with app.state.session_factory() as session, session.begin():
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.review_mode, note.review_required_approvals = "peer_approval", 1  # allowed on a single owner
    response = preview(
        client, ada, request.json()["id"], {"finalContent": {"title": "Final", "body": "final\n"}}
    )
    assert response.status_code == 200 and response.json()["candidate"] == {
        "title": "Final",
        "body": "final\n",
    }
    assert response.json()["mergeDiff"]["title"].startswith("--- current/title\n+++ candidate/title\n")
    created = create(client, ada, title="untouched")
    assert created.status_code == 201  # unrelated notes are unaffected by the preview
