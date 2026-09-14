"""Acceptance row "Lists", the edit-request half: "Lists/search/inbox and all cursor pages reveal only
authorized entries" (Isolation) and "Inbox summaries carry the note's current title." The note-scoped list
returns only requests the caller may inspect; the inbox's `incoming` view is the notes the caller owns and
`outgoing` the requests they proposed on notes they can still read."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event

from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_edit_requests import START, proposal, proposer_note, seed_request, submit
from tests.test_notes import errors, me
from tests.test_shares import share, user

pytestmark = [pytest.mark.acceptance("Lists"), pytest.mark.acceptance("Isolation")]

SUMMARY_KEYS = {
    "id",
    "noteId",
    "noteTitle",
    "proposerId",
    "status",
    "requiredApprovals",
    "approvals",
    "createdAt",
    "updatedAt",
    "closedAt",
}


def note_list(client: ContractClient, persona: Persona, note_id: str, **params: Any) -> httpx.Response:
    return client.get(f"/v1/notes/{note_id}/edit-requests", auth=persona, params=params or None)


def inbox(client: ContractClient, persona: Persona, **params: Any) -> httpx.Response:
    return client.get("/v1/edit-requests", auth=persona, params=params or None)


def ids(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def propose(client: ContractClient, persona: Persona, note_id: str, etag: str, explanation: str) -> str:
    response = submit(client, persona, note_id, proposal(etag, explanation=explanation))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_the_note_scoped_list_shows_owners_everything_and_proposers_their_own(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona, dan: Persona
) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    cara_share = share(client, ada, note_id, user(client, cara), "propose_edit").json()["id"]
    share(client, ada, note_id, user(client, dan), "read")
    first = propose(client, ben, note_id, etag, "first")
    clock.advance(timedelta(seconds=1))
    second = propose(client, cara, note_id, etag, "second")
    clock.advance(timedelta(seconds=1))
    third = propose(client, ben, note_id, etag, "third")
    assert ids(note_list(client, ada, note_id)) == [third, second, first]  # newest first
    assert ids(note_list(client, ben, note_id)) == [third, first]
    assert ids(note_list(client, cara, note_id)) == [second]
    assert ids(note_list(client, dan, note_id)) == []  # a reader who never proposed: an empty page, not 404
    # A proposer downgraded to read still sees their own; one who lost read gets 404.
    client.patch(f"/v1/notes/{note_id}/shares/{cara_share}", auth=ada, json={"permissions": ["read"]})
    assert ids(note_list(client, cara, note_id)) == [second]
    client.delete(f"/v1/notes/{note_id}/shares/{cara_share}", auth=ada)
    assert note_list(client, cara, note_id).status_code == 404
    assert client.get(f"/v1/notes/{note_id}/edit-requests").status_code == 401
    assert note_list(client, ada, str(uuid.uuid4())).status_code == 404
    summary = note_list(client, ada, note_id).json()["items"][0]
    assert set(summary) == SUMMARY_KEYS  # no content or diffs in a summary
    assert summary["noteTitle"] == "Release checklist" and summary["requiredApprovals"] == 0
    assert summary["approvals"] == [] and summary["closedAt"] is None


def test_status_selects_exactly_one_status_and_defaults_to_open(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    open_id = propose(client, ben, note_id, etag, "open")
    ben_id = me(client, ben)
    rejected_id, _ = seed_request(app, note_id=note_id, proposer_id=ben_id, status="rejected", at=START)
    merged_id, _ = seed_request(app, note_id=note_id, proposer_id=ben_id, status="merged", at=START)
    assert ids(note_list(client, ada, note_id)) == [open_id]
    assert ids(note_list(client, ada, note_id, status="open")) == [open_id]
    assert ids(note_list(client, ada, note_id, status="rejected")) == [rejected_id]
    assert ids(note_list(client, ben, note_id, status="merged")) == [merged_id]
    assert ids(note_list(client, ada, note_id, status="withdrawn")) == []
    assert errors(note_list(client, ada, note_id, status="closed")) == [("query", "status")]
    assert ids(inbox(client, ada)) == [open_id]
    assert ids(inbox(client, ada, status="rejected")) == [rejected_id]
    assert ids(inbox(client, ben, view="outgoing", status="merged")) == [merged_id]


def test_incoming_is_what_the_caller_owns_and_outgoing_what_they_proposed(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_a, etag_a = proposer_note(client, ada, ben)
    note_b, etag_b = proposer_note(client, ada, cara)
    note_c, etag_c = proposer_note(client, ben, ada)  # ben's note, shared with ada for proposals
    on_a = propose(client, ben, note_a, etag_a, "ben on a")
    clock.advance(timedelta(seconds=1))
    on_b = propose(client, cara, note_b, etag_b, "cara on b")
    clock.advance(timedelta(seconds=1))
    on_c = propose(client, ada, note_c, etag_c, "ada on c")
    clock.advance(timedelta(seconds=1))
    own = propose(client, ada, note_a, etag_a, "ada on her own note")
    assert ids(inbox(client, ada)) == [own, on_b, on_a]  # incoming, newest first
    assert ids(inbox(client, ada, view="incoming")) == [own, on_b, on_a]
    assert ids(inbox(client, ada, view="outgoing")) == [own, on_c]  # her own proposal appears in both
    assert ids(inbox(client, ben)) == [on_c]
    assert ids(inbox(client, ben, view="outgoing")) == [on_a]
    assert ids(inbox(client, cara)) == []
    assert ids(inbox(client, cara, view="outgoing")) == [on_b]
    assert client.get("/v1/edit-requests").status_code == 401
    titles = {item["id"]: item["noteTitle"] for item in inbox(client, ada).json()["items"]}
    assert set(titles.values()) == {"Release checklist"}


def test_outgoing_needs_current_read_access(client: ContractClient, ada: Persona, ben: Persona) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    request_id = propose(client, ben, note_id, etag, "mine")
    share_id = client.get(f"/v1/notes/{note_id}/shares", auth=ada).json()["items"][0]["id"]
    assert ids(inbox(client, ben, view="outgoing")) == [request_id]
    assert client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 204
    assert ids(inbox(client, ben, view="outgoing")) == []
    assert ids(inbox(client, ada)) == [request_id]  # the owner still sees the submission
    share(client, ada, note_id, user(client, ben), "read")
    assert ids(inbox(client, ben, view="outgoing")) == [request_id]


def test_trashed_requests_are_owner_visible_only_and_expired_ones_invisible(
    client: ContractClient, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    theirs = propose(client, ben, note_id, etag, "ben")
    own = propose(client, ada, note_id, etag, "ada")
    trash_etag = client.delete(f"/v1/notes/{note_id}", auth=ada, if_match=etag).headers["ETag"]
    assert ids(inbox(client, ada)) == [] and ids(inbox(client, ada, view="outgoing")) == []
    assert sorted(ids(inbox(client, ada, state="trashed"))) == sorted([own, theirs])
    assert ids(inbox(client, ada, view="outgoing", state="trashed")) == [own]  # an owner-proposer sees theirs
    assert ids(inbox(client, ben, view="outgoing")) == []
    assert ids(inbox(client, ben, view="outgoing", state="trashed")) == []  # a trashed note is owners only
    assert ids(inbox(client, ben, state="trashed")) == []
    assert sorted(ids(note_list(client, ada, note_id))) == sorted([own, theirs])  # no state filter here
    assert note_list(client, ben, note_id).status_code == 404
    clock.set(START + timedelta(hours=720))  # exactly expiresAt
    assert ids(inbox(client, ada, state="trashed")) == [] and ids(inbox(client, ada)) == []
    clock.set(START + timedelta(hours=720) - timedelta(microseconds=1))
    assert client.post(f"/v1/notes/{note_id}/restore", auth=ada, if_match=trash_etag).status_code == 200
    assert sorted(ids(inbox(client, ada))) == sorted([own, theirs])
    assert ids(inbox(client, ada, state="trashed")) == []


def test_summaries_carry_the_notes_current_title(client: ContractClient, ada: Persona, ben: Persona) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    propose(client, ben, note_id, etag, "x")
    renamed = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=etag, json={"title": "Release runbook"})
    assert renamed.status_code == 200
    assert inbox(client, ada).json()["items"][0]["noteTitle"] == "Release runbook"
    assert note_list(client, ben, note_id).json()["items"][0]["noteTitle"] == "Release runbook"


def test_pages_sort_newest_first_with_ties_by_id_and_revocation_between_pages_hides_requests(
    client: ContractClient, ada: Persona, ben: Persona
) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    same_instant = sorted(propose(client, ben, note_id, etag, f"r{i}") for i in range(3))
    first = inbox(client, ben, view="outgoing", limit=2)
    assert ids(first) == [same_instant[2], same_instant[1]]  # ties on createdAt page by id, descending
    cursor = first.json()["nextCursor"]
    assert ids(inbox(client, ben, view="outgoing", limit=2, cursor=cursor)) == [same_instant[0]]
    for reused in (
        inbox(client, ben, view="incoming", limit=2, cursor=cursor),
        inbox(client, ben, view="outgoing", limit=3, cursor=cursor),
        inbox(client, ada, view="outgoing", limit=2, cursor=cursor),
        note_list(client, ben, note_id, limit=2, cursor=cursor),
    ):
        assert reused.status_code == 400 and reused.json()["code"] == "invalid_cursor"
    owner_first = note_list(client, ada, note_id, limit=2)
    assert ids(owner_first) == [same_instant[2], same_instant[1]]
    share_id = client.get(f"/v1/notes/{note_id}/shares", auth=ada).json()["items"][0]["id"]
    # Revocation between pages: ben's second outgoing page is empty, the cursor itself is still valid.
    assert client.delete(f"/v1/notes/{note_id}/shares/{share_id}", auth=ada).status_code == 204
    assert ids(inbox(client, ben, view="outgoing", limit=2, cursor=cursor)) == []
    assert ids(note_list(client, ada, note_id, limit=2, cursor=owner_first.json()["nextCursor"])) == [
        same_instant[0]
    ]


@pytest.mark.parametrize(
    ("params", "pointer"),
    [
        ({"view": "mine"}, "view"),
        ({"status": "closed"}, "status"),
        ({"state": "deleted"}, "state"),
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"cursor": ""}, "cursor"),
    ],
)
def test_inbox_parameters_are_validated(
    client: ContractClient, ada: Persona, params: dict[str, Any], pointer: str
) -> None:
    assert errors(inbox(client, ada, **params)) == [("query", pointer)]


def test_a_page_costs_a_bounded_number_of_statements(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    note_id, etag = proposer_note(client, ada, ben)
    for i in range(12):
        propose(client, ben, note_id, etag, f"r{i}")

    def selects_during(call: Callable[[], httpx.Response]) -> list[str]:
        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        event.listen(app.state.engine, "before_cursor_execute", record)
        try:
            assert len(ids(call())) == 12
        finally:
            event.remove(app.state.engine, "before_cursor_execute", record)
        return [s for s in statements if s.lstrip().upper().startswith("SELECT")]

    # The caller, the page, the notes, the five batch loads for owners, tags, and access, and the approvals.
    inbox_selects = selects_during(lambda: inbox(client, ada, limit=12))
    assert len(inbox_selects) <= 9, inbox_selects
    # The caller, the note and the caller's access to it (five statements), the page, and the approvals.
    list_selects = selects_during(lambda: note_list(client, ben, note_id, limit=12))
    assert len(list_selects) <= 9, list_selects
