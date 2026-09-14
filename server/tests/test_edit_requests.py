"""Acceptance rows "Submission", "Isolation", "Lifecycle", and "Revoked proposers".

"Submission": "A valid proposal captures the server base and leaves the note unchanged. Stale base ETag
fails without creating a request. Forged base content, tags, ownership, and empty changes are rejected."
"Isolation" (the request half): "Third-party note readers cannot inspect another person's proposal."
"Lifecycle": "Only permitted actors reject/withdraw; read-only proposers may withdraw. Closed records cannot
be mutated/reopened (`request_not_open`). Repeated merge with an old ETag cannot create a second merge. Read
the record after an ambiguous response." "Revoked proposers": "Owner can still review/merge a valid prior
submission. A proposer without read cannot view it; a proposer without propose_edit cannot revise it."
"""

from __future__ import annotations

import copy
import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from notes_api import uow
from notes_api.models import EditRequest
from tests.contract_client import ContractClient
from tests.support import Persona
from tests.test_notes import CREATE_NOTE_REQUEST, create, errors, me
from tests.test_shares import share, team, user

pytestmark = [
    pytest.mark.acceptance("Submission"),
    pytest.mark.acceptance("Isolation"),
    pytest.mark.acceptance("Lifecycle"),
    pytest.mark.acceptance("Revoked proposers"),
]

START_TS = "2026-09-13T12:00:00.000000Z"
ETAG = re.compile(r'^"[0-9a-f]{32}"$')
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
PROPOSED_BODY = "## Release\n\n- Run tests\n- Review metrics\n- Deploy\n"  # CreateEditRequestRequest
STALE = '"0123456789abcdef0123456789abcdef"'


def proposal(base_etag: str, **overrides: Any) -> dict[str, Any]:
    """The contract's ``CreateEditRequestRequest`` against a live note version."""
    body: dict[str, Any] = {
        "baseNoteETag": base_etag,
        "proposedContent": {"title": "Release checklist", "body": PROPOSED_BODY},
        "explanation": "Add a metrics check before deployment.",
    }
    body.update(overrides)
    return body


def submit(client: ContractClient, persona: Persona, note_id: str, body: dict[str, Any]) -> httpx.Response:
    return client.post(f"/v1/notes/{note_id}/edit-requests", auth=persona, json=body)


def proposer_note(client: ContractClient, owner: Persona, proposer: Persona) -> tuple[str, str]:
    """The guide's note, shared with ``proposer`` for proposals; returns its id and ETag."""
    created = create(client, owner)
    note_id = str(created.json()["id"])
    assert share(client, owner, note_id, user(client, proposer), "propose_edit").status_code == 201
    return note_id, str(created.headers["ETag"])


def request_count(app: FastAPI) -> int:
    with app.state.session_factory() as session:
        return int(session.execute(select(func.count()).select_from(EditRequest)).scalar_one())


def note_state(client: ContractClient, owner: Persona, note_id: str) -> tuple[str, str, str]:
    response = client.get(f"/v1/notes/{note_id}", auth=owner)
    return response.headers["ETag"], response.json()["updatedAt"], response.json()["body"]


# -- submission -----------------------------------------------------------------------------------------


def test_the_contracts_submission_captures_the_base_and_leaves_the_note_unchanged(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    before = note_state(client, ada, note_id)
    body = copy.deepcopy(examples["CreateEditRequestRequest"])
    body["baseNoteETag"] = etag
    response = submit(client, ben, note_id, body)
    assert response.status_code == 201, response.text
    request = response.json()
    assert UUID.match(request["id"]) and ETAG.match(response.headers["ETag"])
    assert response.headers["Location"] == f"/v1/edit-requests/{request['id']}"
    assert request["noteId"] == note_id and request["noteTitle"] == "Release checklist"
    assert request["proposerId"] == me(client, ben) and request["status"] == "open"
    assert request["requiredApprovals"] == 0 and request["approvals"] == []
    assert request["createdAt"] == request["updatedAt"] == START_TS and request["closedAt"] is None
    assert request["baseContent"] == {
        "title": CREATE_NOTE_REQUEST["title"],
        "body": CREATE_NOTE_REQUEST["body"],
    }
    assert request["proposedContent"] == body["proposedContent"]
    assert request["explanation"] == body["explanation"]
    assert request["proposalDiff"] == examples["EditRequestOpen"]["proposalDiff"]  # byte for byte
    assert request["rejectedBy"] is None and request["rejectionReason"] is None
    assert request["mergeRecord"] is None
    assert note_state(client, ada, note_id) == before


def test_a_stale_base_is_412_and_creates_nothing(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    note_id, old_etag = proposer_note(client, ada, ben)
    moved = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=old_etag, json={"tags": ["ops"]})
    assert moved.status_code == 200
    stale = submit(client, ben, note_id, proposal(old_etag))
    assert stale.status_code == 412 and stale.json()["code"] == "precondition_failed"
    assert "baseNoteETag" in stale.json()["detail"]
    assert request_count(app) == 0
    assert submit(client, ben, note_id, proposal(STALE)).status_code == 412
    assert request_count(app) == 0
    assert submit(client, ben, note_id, proposal(moved.headers["ETag"])).status_code == 201
    assert request_count(app) == 1


def test_a_proposal_identical_to_the_note_is_the_contracts_empty_proposal_problem(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    same = {"title": CREATE_NOTE_REQUEST["title"], "body": CREATE_NOTE_REQUEST["body"]}
    response = submit(client, ben, note_id, proposal(etag, proposedContent=same))
    assert response.status_code == 422
    assert response.json() == examples["ProblemEmptyProposal"]
    # A different title with the same body, or the same title with a different body, is a real proposal.
    assert (
        submit(
            client, ben, note_id, proposal(etag, proposedContent={**same, "title": "Release plan"})
        ).status_code
        == 201
    )


@pytest.mark.parametrize(
    ("changes", "pointers"),
    [
        ({"baseNoteETag": None}, ["/baseNoteETag"]),
        ({"baseNoteETag": 'W/"x"'}, ["/baseNoteETag"]),
        ({"baseNoteETag": "note-v1"}, ["/baseNoteETag"]),
        ({"baseNoteETag": "*"}, ["/baseNoteETag"]),
        ({"proposedContent": None}, ["/proposedContent"]),
        ({"proposedContent": {"title": "t"}}, ["/proposedContent/body"]),
        ({"proposedContent": {"title": "   ", "body": "b"}}, ["/proposedContent/title"]),
        ({"proposedContent": {"title": "t", "body": "b", "tags": []}}, ["/proposedContent/tags"]),
        ({"baseContent": {"title": "t", "body": "b"}}, ["/baseContent"]),
        ({"tags": ["x"]}, ["/tags"]),
        ({"status": "merged"}, ["/status"]),
        ({"proposerId": "x"}, ["/proposerId"]),
        ({"explanation": "x" * 10001}, ["/explanation"]),
        ({"explanation": "a\x00b"}, ["/explanation"]),
        ({"explanation": 5}, ["/explanation"]),
    ],
    ids=[
        "no base etag",
        "weak base etag",
        "unquoted base etag",
        "wildcard base etag",
        "no proposed content",
        "incomplete proposed content",
        "blank title",
        "tags inside the content",
        "forged base content",
        "tags",
        "status",
        "proposer",
        "explanation too long",
        "explanation with NUL",
        "explanation not a string",
    ],
)
def test_the_body_is_validated_against_the_contract(
    client: ContractClient,
    app: FastAPI,
    ada: Persona,
    ben: Persona,
    changes: dict[str, Any],
    pointers: list[str],
) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    body = proposal(etag)
    for key, value in changes.items():
        if value is None:
            del body[key]
        else:
            body[key] = value
    response = submit(client, ben, note_id, body)
    assert response.status_code == 422, response.text
    assert [pointer for _, pointer in errors(response)] == pointers
    assert request_count(app) == 0


def test_explanation_is_optional_and_may_be_null(client: ContractClient, ada: Persona, ben: Persona) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    body = proposal(etag)
    del body["explanation"]
    assert submit(client, ben, note_id, body).json()["explanation"] is None
    body = proposal(etag, explanation=None)
    assert submit(client, ben, note_id, body).json()["explanation"] is None
    assert submit(client, ben, note_id, proposal(etag, explanation="")).json()["explanation"] == ""


def test_who_may_submit(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    share(client, ada, note_id, user(client, ben), "comment")
    refused = submit(client, ben, note_id, proposal(etag))
    assert refused.status_code == 403 and refused.json()["code"] == "forbidden"
    assert submit(client, cara, note_id, proposal(etag)).status_code == 404
    assert client.post(f"/v1/notes/{note_id}/edit-requests", json=proposal(etag)).status_code == 401
    # A team share carries propose_edit to its current members; an owner may propose on their own note.
    share(client, ada, note_id, team(client, cara, dan), "propose_edit")
    assert submit(client, dan, note_id, proposal(etag)).status_code == 201
    own = submit(client, ada, note_id, proposal(etag, explanation="My own refinement."))
    assert own.status_code == 201 and own.json()["proposerId"] == me(client, ada)
    assert own.json()["requiredApprovals"] == 0
    assert (
        submit(client, cara, note_id, proposal(etag)).status_code == 201
    )  # the team's admin is a member too


def test_a_trashed_note_refuses_submissions_after_the_version_check(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=etag).headers["ETag"]
    assert submit(client, ben, note_id, proposal(trash_etag)).status_code == 404  # the share is gone
    refused = submit(client, ada, note_id, proposal(trash_etag))
    assert refused.status_code == 409 and refused.json()["code"] == "note_not_active"
    assert submit(client, ada, note_id, proposal(etag)).status_code == 412  # 412 precedes 409
    restored = client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag)
    assert submit(client, ada, note_id, proposal(restored.headers["ETag"])).status_code == 201


@pytest.mark.parametrize("revocation", ["delete", "downgrade"])
def test_access_revoked_before_the_transaction_blocks_the_submission(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, restore_hooks: None, revocation: str
) -> None:
    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    share_id = share(client, ada, note_id, user(client, ben), "propose_edit").json()["id"]
    competitor = ContractClient(app)
    fired: list[str] = []

    def before_begin(op: str) -> None:
        if op == "create_edit_request" and not fired:
            fired.append(op)
            url = f"/v1/notes/{note_id}/shares/{share_id}"
            if revocation == "delete":
                assert competitor.delete(url, auth=ada).status_code == 204
            else:
                assert competitor.patch(url, auth=ada, json={"permissions": ["read"]}).status_code == 200

    uow.hooks.before_begin = before_begin
    response = submit(client, ben, note_id, proposal(etag))
    assert response.status_code == (404 if revocation == "delete" else 403) and fired == [
        "create_edit_request"
    ]
    assert request_count(app) == 0
