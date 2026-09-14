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
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select

from notes_api import etags, uow
from notes_api.models import Approval, EditRequest, Note, NoteOwner
from notes_api.services.edit_requests import required_approvals
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_notes import CREATE_NOTE_REQUEST, create, errors, me
from tests.test_shares import share, team, user

pytestmark = [
    pytest.mark.acceptance("Submission"),
    pytest.mark.acceptance("Isolation"),
    pytest.mark.acceptance("Lifecycle"),
    pytest.mark.acceptance("Revoked proposers"),
]

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
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


# -- inspection -----------------------------------------------------------------------------------------


def submitted(client: ContractClient, owner: Persona, proposer: Persona) -> tuple[str, str, str, str]:
    """The guide's note shared with ``proposer``, plus one open request of theirs.

    Returns the note id, the note ETag, the request id, and the request ETag.
    """
    note_id, note_etag = proposer_note(client, owner, proposer)
    response = submit(client, proposer, note_id, proposal(note_etag))
    assert response.status_code == 201, response.text
    return note_id, note_etag, str(response.json()["id"]), str(response.headers["ETag"])


def get_request(client: ContractClient, persona: Persona, request_id: str) -> httpx.Response:
    return client.get(f"/v1/edit-requests/{request_id}", auth=persona)


def seed_request(
    app: FastAPI, *, note_id: str, proposer_id: str, status: str, at: datetime, **fields: Any
) -> tuple[str, str]:
    """A request row written directly, in any state; returns its id and quoted version."""
    request_id, version = uuid.uuid4(), etags.new_version()
    row = EditRequest(
        id=request_id,
        note_id=uuid.UUID(note_id),
        proposer_id=uuid.UUID(proposer_id),
        status=status,
        base_title=CREATE_NOTE_REQUEST["title"],
        base_body=CREATE_NOTE_REQUEST["body"],
        proposed_title="Release checklist",
        proposed_body=PROPOSED_BODY,
        explanation=None,
        created_at=at,
        updated_at=at,
        closed_at=None if status == "open" else at,
        rejected_by=None,
        rejection_reason=None,
        merged_title=None,
        merged_body=None,
        merged_note_version=None,
        merged_by=None,
        merged_at=None,
        required_approvals_at_close=None if status == "open" else 0,
        version=version,
    )
    for key, value in fields.items():
        setattr(row, key, value)
    with app.state.session_factory() as session, session.begin():
        session.add(row)
    return str(request_id), etags.quote(version)


def test_owners_and_the_proposer_inspect_a_request_and_nobody_else(
    client: ContractClient, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, _, request_id, etag = submitted(client, ada, ben)
    share(client, ada, note_id, user(client, cara), "comment", "propose_edit")
    for persona in (ada, ben):
        response = get_request(client, persona, request_id)
        assert response.status_code == 200 and response.headers["ETag"] == etag
        assert response.json()["id"] == request_id and response.json()["proposerId"] == me(client, ben)
    assert get_request(client, cara, request_id).status_code == 404  # another reader, even a proposer
    assert get_request(client, dan, request_id).status_code == 404
    assert client.get(f"/v1/edit-requests/{request_id}").status_code == 401
    assert get_request(client, ada, str(uuid.uuid4())).status_code == 404
    assert errors(client.get("/v1/edit-requests/not-a-uuid", auth=ada)) == [("path", "requestId")]


def test_a_proposer_who_lost_read_access_cannot_inspect_until_re_shared(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id, _, request_id, _ = submitted(client, ada, ben)
    share_id = client.get(f"/v1/notes/{note_id}/shares", auth=ada).json()["items"][0]["id"]
    assert client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 204
    assert get_request(client, ben, request_id).status_code == 404
    assert get_request(client, ada, request_id).status_code == 200  # the owner still reviews it
    assert share(client, ada, note_id, user(client, ben), "read").status_code == 201
    assert get_request(client, ben, request_id).status_code == 200  # read access suffices to inspect


def test_note_title_is_live_while_the_request_etag_is_stored(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, etag = submitted(client, ada, ben)
    before = get_request(client, ben, request_id).json()
    clock.advance(timedelta(minutes=1))
    renamed = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match=note_etag, json={"title": "Release checklist (2026)"}
    )
    assert renamed.status_code == 200
    after = get_request(client, ben, request_id)
    assert after.headers["ETag"] == etag
    assert after.json() == {**before, "noteTitle": "Release checklist (2026)"}  # nothing else moved


@pytest.mark.acceptance("Trash")
def test_a_trashed_notes_requests_are_the_owners_and_an_expired_notes_nobodys(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, etag = submitted(client, ada, ben)
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=note_etag).headers["ETag"]
    assert get_request(client, ben, request_id).status_code == 404
    seen = get_request(client, ada, request_id)
    assert seen.status_code == 200 and seen.headers["ETag"] == etag and seen.json()["status"] == "open"
    expires = START + timedelta(hours=720)
    clock.set(expires - timedelta(microseconds=1))
    assert get_request(client, ada, request_id).status_code == 200
    clock.set(expires)
    assert get_request(client, ada, request_id).status_code == 404
    clock.set(START + timedelta(days=1))
    assert client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag).status_code == 200
    assert get_request(client, ada, request_id).status_code == 200
    assert get_request(client, ben, request_id).status_code == 404  # the share did not come back


def test_closed_requests_render_their_status_specific_fields(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    note_id, _, _, _ = submitted(client, ada, ben)
    ada_id, ben_id = me(client, ada), me(client, ben)
    at = START + timedelta(hours=1)
    merged_id, merged_etag = seed_request(
        app,
        note_id=note_id,
        proposer_id=ben_id,
        status="merged",
        at=at,
        merged_title="Release checklist",
        merged_body=PROPOSED_BODY,
        merged_note_version="f" * 32,
        merged_by=uuid.UUID(ada_id),
        merged_at=at,
    )
    reason = "Staged deploys are covered by the deployment runbook."
    rejected_id, _ = seed_request(
        app,
        note_id=note_id,
        proposer_id=ben_id,
        status="rejected",
        at=at,
        rejected_by=uuid.UUID(ada_id),
        rejection_reason=reason,
    )
    withdrawn_id, _ = seed_request(app, note_id=note_id, proposer_id=ben_id, status="withdrawn", at=at)
    unfrozen_id, _ = seed_request(
        app, note_id=note_id, proposer_id=ben_id, status="withdrawn", at=at, required_approvals_at_close=None
    )

    merged = get_request(client, ada, merged_id)  # the ContractClient validates the per-status rules
    assert merged.status_code == 200 and merged.headers["ETag"] == merged_etag
    body = merged.json()
    assert body["status"] == "merged" and body["closedAt"] == "2026-09-13T13:00:00.000000Z"
    assert body["mergeRecord"] == {
        "content": {"title": "Release checklist", "body": PROPOSED_BODY},
        "noteETag": '"' + "f" * 32 + '"',
        "mergedBy": ada_id,
        "mergedAt": "2026-09-13T13:00:00.000000Z",
    }
    assert body["rejectedBy"] is None and body["rejectionReason"] is None
    assert body["requiredApprovals"] == 0 and body["approvals"] == []
    assert body["proposedContent"]["body"] == PROPOSED_BODY
    assert body["proposalDiff"]["body"].startswith("--- base/body\n+++ proposed/body\n")
    rejected = get_request(client, ben, rejected_id).json()
    assert rejected["status"] == "rejected" and rejected["rejectedBy"] == ada_id
    assert rejected["rejectionReason"] == reason and rejected["mergeRecord"] is None
    withdrawn = get_request(client, ben, withdrawn_id).json()
    assert withdrawn["status"] == "withdrawn" and withdrawn["closedAt"] == "2026-09-13T13:00:00.000000Z"
    assert withdrawn["rejectedBy"] is None and withdrawn["rejectionReason"] is None
    assert withdrawn["mergeRecord"] is None
    assert get_request(client, ada, unfrozen_id).json()["requiredApprovals"] == 0  # falls back to live


@pytest.mark.parametrize(
    ("owners", "mode", "policy", "proposer_is_owner", "expected"),
    [
        (1, "self_merge", None, True, 0),
        (1, "peer_approval", 1, False, 0),
        (2, "self_merge", None, False, 0),
        (2, "peer_approval", 1, True, 1),
        (2, "peer_approval", 1, False, 2),
        (2, "peer_approval", 3, True, 1),
        (2, "peer_approval", 3, False, 2),
        (3, "peer_approval", 3, True, 2),
        (3, "peer_approval", 3, False, 3),
        (5, "peer_approval", 2, True, 2),
        (5, "peer_approval", 2, False, 2),
        (20, "peer_approval", 20, True, 19),
        (20, "peer_approval", 20, False, 20),
    ],
)
def test_the_effective_required_approvals_formula(
    owners: int, mode: str, policy: int | None, proposer_is_owner: bool, expected: int
) -> None:
    assert (
        required_approvals(
            owner_count=owners, review_mode=mode, policy=policy, proposer_is_owner=proposer_is_owner
        )
        == expected
    )


def test_required_approvals_and_approvals_render_from_the_notes_owners_and_policy(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id, note_etag, request_id, etag = submitted(client, ada, ben)  # ben is not an owner
    own = submit(client, ada, note_id, proposal(note_etag, explanation="My own refinement."))
    assert own.json()["requiredApprovals"] == 0
    ada_id, cara_id = me(client, ada), me(client, cara)
    with app.state.session_factory() as session, session.begin():
        session.add(
            NoteOwner(note_id=uuid.UUID(note_id), user_id=uuid.UUID(cara_id), position=1, added_at=START)
        )
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.review_mode, note.review_required_approvals = "peer_approval", 1
        request_uuid = uuid.UUID(request_id)
        session.add(
            Approval(
                request_id=request_uuid, user_id=uuid.UUID(ada_id), approved_at=START + timedelta(minutes=20)
            )
        )
        session.add(
            Approval(
                request_id=request_uuid, user_id=uuid.UUID(cara_id), approved_at=START + timedelta(minutes=10)
            )
        )
    theirs = get_request(client, ada, request_id)
    assert theirs.json()["requiredApprovals"] == 2  # a non-owner's proposal needs two distinct owners
    assert theirs.json()["approvals"] == [
        {"userId": cara_id, "approvedAt": "2026-09-13T12:10:00.000000Z"},
        {"userId": ada_id, "approvedAt": "2026-09-13T12:20:00.000000Z"},
    ]
    assert theirs.headers["ETag"] == etag  # both are live views outside the request ETag
    assert get_request(client, ada, own.json()["id"]).json()["requiredApprovals"] == 1  # min(1, owners - 1)
    assert get_request(client, cara, request_id).status_code == 200  # the new owner inspects too


def test_reading_a_request_costs_a_bounded_number_of_statements(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    _, _, request_id, _ = submitted(client, ada, ben)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", record)
    try:
        assert get_request(client, ben, request_id).status_code == 200
    finally:
        event.remove(app.state.engine, "before_cursor_execute", record)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 8, selects  # caller, the joined pair, access (3), owners, tags, approvals


# -- revise, withdraw, reject ---------------------------------------------------------------------------

REJECTION_REASON = "Staged deploys are covered by the deployment runbook."  # RejectEditRequestRequest


def revise(
    client: ContractClient,
    persona: Persona,
    request_id: str,
    etag: str | None,
    body: Any = None,
    **kwargs: Any,
) -> httpx.Response:
    return client.patch(f"/v1/edit-requests/{request_id}", auth=persona, if_match=etag, json=body, **kwargs)


def withdraw(
    client: ContractClient, persona: Persona, request_id: str, etag: str | None, **kwargs: Any
) -> httpx.Response:
    return client.post(f"/v1/edit-requests/{request_id}/withdraw", auth=persona, if_match=etag, **kwargs)


def reject(
    client: ContractClient,
    persona: Persona,
    request_id: str,
    etag: str | None,
    body: Any = None,
    **kwargs: Any,
) -> httpx.Response:
    return client.post(
        f"/v1/edit-requests/{request_id}/reject", auth=persona, if_match=etag, json=body, **kwargs
    )


def seed_approvals(app: FastAPI, request_id: str, *user_ids: str) -> None:
    with app.state.session_factory() as session, session.begin():
        for offset, user_id in enumerate(user_ids):
            session.add(
                Approval(
                    request_id=uuid.UUID(request_id),
                    user_id=uuid.UUID(user_id),
                    approved_at=START + timedelta(minutes=offset + 1),
                )
            )


def approvers(client: ContractClient, persona: Persona, request_id: str) -> list[str]:
    return [entry["userId"] for entry in get_request(client, persona, request_id).json()["approvals"]]


def test_the_contracts_revision_advances_the_etag_and_recomputes_the_diff(
    client: ContractClient, clock: FakeClock, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, etag = submitted(client, ada, ben)
    clock.advance(timedelta(minutes=1))
    revised = revise(client, ben, request_id, etag, examples["ReviseEditRequestRequest"])
    assert revised.status_code == 200, revised.text
    body = revised.json()
    assert ETAG.match(revised.headers["ETag"]) and revised.headers["ETag"] != etag
    assert body["proposedContent"] == examples["ReviseEditRequestRequest"]["proposedContent"]
    assert body["explanation"] == examples["ReviseEditRequestRequest"]["explanation"]
    assert body["baseContent"] == {"title": CREATE_NOTE_REQUEST["title"], "body": CREATE_NOTE_REQUEST["body"]}
    assert body["proposalDiff"]["title"].startswith("--- base/title\n+++ proposed/title\n")
    assert (
        "+Release runbook\n" in body["proposalDiff"]["title"]
        and "+- Deploy (staged)\n" in body["proposalDiff"]["body"]
    )
    assert body["updatedAt"] == "2026-09-13T12:01:00.000000Z" and body["createdAt"] == START_TS
    assert body["status"] == "open" and body["closedAt"] is None
    assert get_request(client, ada, request_id).json() == body
    assert note_state(client, ada, note_id)[0] == note_etag  # the note is untouched


def test_explanation_only_revisions_keep_approvals_and_content_changes_delete_them(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    _, _, request_id, etag = submitted(client, ada, ben)
    ada_id = me(client, ada)
    seed_approvals(app, request_id, ada_id)
    cleared = revise(client, ben, request_id, etag, {"explanation": None})
    assert cleared.status_code == 200 and cleared.json()["explanation"] is None
    assert cleared.headers["ETag"] != etag and approvers(client, ada, request_id) == [ada_id]
    emptied = revise(client, ben, request_id, cleared.headers["ETag"], {"explanation": ""})
    assert emptied.status_code == 200 and emptied.json()["explanation"] == ""
    assert approvers(client, ada, request_id) == [ada_id]
    current = emptied.json()["proposedContent"]
    same_content = revise(
        client, ben, request_id, emptied.headers["ETag"], {"proposedContent": current, "explanation": "again"}
    )
    assert same_content.status_code == 200 and same_content.json()["explanation"] == "again"
    assert approvers(client, ada, request_id) == [ada_id]  # the stored proposal did not change
    changed = revise(
        client,
        ben,
        request_id,
        same_content.headers["ETag"],
        {"proposedContent": {**current, "title": "Release runbook"}},
    )
    assert changed.status_code == 200 and changed.json()["approvals"] == []
    assert changed.json()["proposedContent"]["title"] == "Release runbook"
    assert changed.json()["explanation"] == "again"  # untouched by a content-only revision
    assert approvers(client, ada, request_id) == []


def test_an_identical_resubmission_is_a_no_op(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    _, _, request_id, etag = submitted(client, ada, ben)
    before = get_request(client, ben, request_id).json()
    clock.advance(timedelta(minutes=5))
    same = revise(
        client,
        ben,
        request_id,
        etag,
        {"proposedContent": before["proposedContent"], "explanation": before["explanation"]},
    )
    assert same.status_code == 200 and same.headers["ETag"] == etag and same.json() == before
    only_content = revise(client, ben, request_id, etag, {"proposedContent": before["proposedContent"]})
    assert only_content.status_code == 200 and only_content.headers["ETag"] == etag
    assert (
        revise(client, ben, request_id, etag, {"explanation": before["explanation"]}).headers["ETag"] == etag
    )


@pytest.mark.parametrize(
    ("body", "pointers"),
    [
        ({}, [""]),
        ({"proposedContent": {"title": "t"}}, ["/proposedContent/body"]),
        ({"proposedContent": {"title": "t", "body": "b", "tags": []}}, ["/proposedContent/tags"]),
        ({"baseNoteETag": '"x"', "explanation": "x"}, ["/baseNoteETag"]),
        ({"baseContent": {"title": "t", "body": "b"}}, ["/baseContent"]),
        ({"explanation": "x" * 10001}, ["/explanation"]),
        ({"explanation": "a\x00b"}, ["/explanation"]),
    ],
    ids=["empty", "incomplete content", "tags in content", "base etag", "forged base", "long", "NUL"],
)
def test_a_revision_body_is_validated_before_the_precondition(
    client: ContractClient, ada: Persona, ben: Persona, body: dict[str, Any], pointers: list[str]
) -> None:
    _, _, request_id, etag = submitted(client, ada, ben)
    with_etag = revise(client, ben, request_id, etag, body)
    assert with_etag.status_code == 422, with_etag.text
    assert [pointer for _, pointer in errors(with_etag)] == pointers
    assert revise(client, ben, request_id, None, body).status_code == 422  # body shape before 428
    assert get_request(client, ben, request_id).headers["ETag"] == etag


def test_a_revision_equal_to_the_base_is_422_and_preconditions_apply(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    _, _, request_id, etag = submitted(client, ada, ben)
    base = get_request(client, ben, request_id).json()["baseContent"]
    empty = revise(client, ben, request_id, etag, {"proposedContent": base})
    assert empty.status_code == 422 and empty.json() == examples["ProblemEmptyProposal"]
    assert revise(client, ben, request_id, None, {"explanation": "x"}).status_code == 428
    for bad in ('W/"x"', "*", "x"):
        response = revise(client, ben, request_id, bad, {"explanation": "x"})
        assert response.status_code == 400 and errors(response) == [("header", "If-Match")]
    assert revise(client, ben, request_id, STALE, {"explanation": "x"}).status_code == 412
    assert (
        revise(client, ben, request_id, etag, content="x", headers={"Content-Type": "text/plain"}).status_code
        == 415
    )
    assert get_request(client, ben, request_id).headers["ETag"] == etag


def test_who_may_revise_withdraw_and_reject(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, dan: Persona
) -> None:
    note_id, note_etag = proposer_note(client, ada, ben)
    share(client, ada, note_id, user(client, dan), "read")
    first = submit(client, ben, note_id, proposal(note_etag, explanation="first"))
    second = submit(client, ben, note_id, proposal(note_etag, explanation="second"))
    third = submit(client, ben, note_id, proposal(note_etag, explanation="third"))
    r1, e1 = first.json()["id"], first.headers["ETag"]
    r2, e2 = second.json()["id"], second.headers["ETag"]
    r3, e3 = third.json()["id"], third.headers["ETag"]
    # An owner who did not propose can neither revise nor withdraw, but may reject; a non-owner proposer
    # cannot reject; another reader cannot even see the request.
    assert revise(client, ada, r1, e1, {"explanation": "x"}).status_code == 403
    assert withdraw(client, ada, r1, e1).status_code == 403
    assert revise(client, ada, r1, None, {"explanation": "x"}).status_code == 403  # 403 precedes 428
    refused = reject(client, ben, r1, e1)
    assert refused.status_code == 403 and refused.json()["code"] == "forbidden"
    for response in (
        revise(client, dan, r1, e1, {"explanation": "x"}),
        withdraw(client, dan, r1, e1),
        reject(client, dan, r1, e1),
    ):
        assert response.status_code == 404
    # A proposer downgraded to read may withdraw but not revise.
    share_id = client.get(f"/v1/notes/{note_id}/shares", auth=ada).json()["items"][0]["id"]
    client.patch(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada, json={"permissions": ["read"]})
    assert revise(client, ben, r1, e1, {"explanation": "x"}).status_code == 403
    clock.advance(timedelta(minutes=1))
    withdrawn = withdraw(client, ben, r1, e1)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "withdrawn" and withdrawn.headers["ETag"] != e1
    assert withdrawn.json()["closedAt"] == withdrawn.json()["updatedAt"] == "2026-09-13T12:01:00.000000Z"
    assert withdrawn.json()["rejectedBy"] is None and withdrawn.json()["mergeRecord"] is None
    # A proposer without read access cannot withdraw; the owner rejects with the example's reason.
    client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada)
    assert withdraw(client, ben, r2, e2).status_code == 404
    rejected = reject(client, ada, r2, e2, {"reason": REJECTION_REASON})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected" and rejected.json()["rejectedBy"] == me(client, ada)
    assert rejected.json()["rejectionReason"] == REJECTION_REASON and rejected.headers["ETag"] != e2
    assert rejected.json()["closedAt"] == "2026-09-13T12:01:00.000000Z"
    # Rejection without a body, and the note is unchanged by every transition.
    plain = reject(client, ada, r3, e3)
    assert plain.status_code == 200 and plain.json()["rejectionReason"] is None
    assert plain.json()["rejectedBy"] == me(client, ada)
    assert note_state(client, ada, note_id)[0] == note_etag
    assert get_request(client, ada, r1).json()["status"] == "withdrawn"  # the owner still reads them all


def test_an_owner_who_proposed_may_withdraw_and_reject_their_own_request(
    client: ContractClient, ada: Persona
) -> None:
    created = create(client, ada)
    note_id, note_etag = created.json()["id"], created.headers["ETag"]
    one = submit(client, ada, note_id, proposal(note_etag, explanation="one"))
    two = submit(client, ada, note_id, proposal(note_etag, explanation="two"))
    assert withdraw(client, ada, one.json()["id"], one.headers["ETag"]).json()["status"] == "withdrawn"
    rejected = reject(client, ada, two.json()["id"], two.headers["ETag"], {"reason": None})
    assert rejected.json()["status"] == "rejected" and rejected.json()["rejectedBy"] == me(client, ada)
    assert rejected.json()["rejectionReason"] is None


def test_optional_bodies_on_reject_and_no_body_on_withdraw(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id, note_etag = proposer_note(client, ada, ben)

    def fresh() -> tuple[str, str]:
        response = submit(client, ben, note_id, proposal(note_etag))
        return str(response.json()["id"]), str(response.headers["ETag"])

    plain = {"Content-Type": "text/plain"}
    r, e = fresh()
    assert reject(client, ada, r, e, {}).json()["rejectionReason"] is None
    r, e = fresh()
    assert reject(client, ada, r, e, content=b"", headers=plain).status_code == 200  # empty is omitted
    r, e = fresh()
    assert reject(client, ada, r, e, content="x", headers=plain).status_code == 415
    assert (
        reject(client, ada, r, e, content="null", headers={"Content-Type": "application/json"}).status_code
        == 422
    )
    assert errors(reject(client, ada, r, e, {"reason": "x" * 10001})) == [("body", "/reason")]
    assert errors(reject(client, ada, r, e, {"reason": "x", "rejectedBy": "me"})) == [("body", "/rejectedBy")]
    assert reject(client, ada, r, None, {"reason": "x"}).status_code == 428
    assert reject(client, ada, r, STALE, {"reason": "x"}).status_code == 412
    assert get_request(client, ada, r).json()["status"] == "open"
    r, e = fresh()
    assert withdraw(client, ben, r, e, content="anything", headers=plain).status_code == 200  # ignored
    r, e = fresh()
    assert withdraw(client, ben, r, None).status_code == 428
    assert withdraw(client, ben, r, STALE).status_code == 412
    assert withdraw(client, ben, r, e, json={"unexpected": True}).status_code == 200


def test_closing_freezes_required_approvals_and_closed_records_are_immutable(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id, note_etag, request_id, etag = submitted(client, ada, ben)
    with app.state.session_factory() as session, session.begin():
        session.add(
            NoteOwner(
                note_id=uuid.UUID(note_id), user_id=uuid.UUID(me(client, cara)), position=1, added_at=START
            )
        )
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.review_mode, note.review_required_approvals = "peer_approval", 1
    assert get_request(client, ada, request_id).json()["requiredApprovals"] == 2
    rejected = reject(client, ada, request_id, etag)
    assert rejected.status_code == 200 and rejected.json()["requiredApprovals"] == 2
    closed_etag = rejected.headers["ETag"]
    with app.state.session_factory() as session, session.begin():
        note = session.get(Note, uuid.UUID(note_id))
        assert note is not None
        note.review_mode, note.review_required_approvals = "self_merge", None  # live value would be 0
    assert get_request(client, ada, request_id).json()["requiredApprovals"] == 2  # frozen at closing
    # Closed records: the old ETag is 412, the current one 409 request_not_open, for every transition.
    for stale in (
        revise(client, ben, request_id, etag, {"explanation": "x"}),
        withdraw(client, ben, request_id, etag),
        reject(client, ada, request_id, etag),
    ):
        assert stale.status_code == 412
    for current in (
        revise(client, ben, request_id, closed_etag, {"explanation": "x"}),
        withdraw(client, ben, request_id, closed_etag),
        reject(client, ada, request_id, closed_etag),
    ):
        assert current.status_code == 409 and current.json()["code"] == "request_not_open"
    # ... and a closed request stays request_not_open when its note is trashed afterwards.
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=note_etag).headers["ETag"]
    assert reject(client, ada, request_id, closed_etag).json()["code"] == "request_not_open"
    assert reject(client, ada, request_id, etag).status_code == 412
    assert client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag).status_code == 200
    assert get_request(client, ada, request_id).headers["ETag"] == closed_etag


@pytest.mark.acceptance("Trash")
def test_a_trashed_note_freezes_its_open_requests(client: ContractClient, ada: Persona) -> None:
    created = create(client, ada)
    note_id, note_etag = created.json()["id"], created.headers["ETag"]
    own = submit(client, ada, note_id, proposal(note_etag, explanation="own"))
    request_id, etag = own.json()["id"], own.headers["ETag"]
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=note_etag).headers["ETag"]
    for frozen in (
        revise(client, ada, request_id, etag, {"explanation": "x"}),
        withdraw(client, ada, request_id, etag),
        reject(client, ada, request_id, etag),
    ):
        assert frozen.status_code == 409 and frozen.json()["code"] == "note_not_active"
    assert withdraw(client, ada, request_id, STALE).status_code == 412  # the version check still comes first
    assert get_request(client, ada, request_id).json()["status"] == "open"
    assert client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag).status_code == 200
    assert withdraw(client, ada, request_id, etag).json()["status"] == "withdrawn"


def test_a_competing_withdrawal_committed_first_makes_the_rejection_stale(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, restore_hooks: None
) -> None:
    _, _, request_id, etag = submitted(client, ada, ben)
    competitor = ContractClient(app)
    fired: list[str] = []

    def before_begin(op: str) -> None:
        if op == "reject_edit_request" and not fired:
            fired.append(op)
            assert withdraw(competitor, ben, request_id, etag).status_code == 200

    uow.hooks.before_begin = before_begin
    stale = reject(client, ada, request_id, etag)
    assert stale.status_code == 412 and fired == ["reject_edit_request"]
    seen = get_request(client, ada, request_id)
    assert seen.json()["status"] == "withdrawn"
    current = reject(client, ada, request_id, seen.headers["ETag"])
    assert current.status_code == 409 and current.json()["code"] == "request_not_open"
