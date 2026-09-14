"""Acceptance row "Review races": "A note edit or proposal revision after preview yields 412. Two competing
merges cannot overwrite each other. Concurrent withdrawal/rejection versus merge permits only one transition.
An approval and a revision racing on the same request ETag permit only one. Authorization revoked before a
mutation commits prevents the unauthorized commit." (The approval race arrives with PR 5's endpoints.)

Deterministic races run the competitor inside ``uow.hooks.before_begin`` of the primary operation, in its
own session, which proves that every check the primary makes lives inside its transaction. The
thread-and-barrier races assert the set of outcomes and run unchanged against PostgreSQL in CI."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from notes_api import uow
from notes_api.models import NoteOwner
from tests.contract_client import ContractClient
from tests.support import Persona
from tests.test_edit_requests import get_request, proposal, proposer_note, reject, revise, submit, withdraw
from tests.test_merge import merge
from tests.test_notes import CREATE_NOTE_REQUEST, me
from tests.test_preview import preview, submitted_example

pytestmark = pytest.mark.acceptance("Review races")

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
ANNOUNCE = str(CREATE_NOTE_REQUEST["body"]) + "- Announce in #releases\n"  # a change in a separate region


def hook(op: str, action: Callable[[], None]) -> list[str]:
    """Run ``action`` once, in its own session, right before the primary ``op`` begins its transaction."""
    fired: list[str] = []

    def before_begin(name: str) -> None:
        if name == op and not fired:
            fired.append(name)
            action()

    uow.hooks.before_begin = before_begin
    return fired


def add_owner(app: FastAPI, client: ContractClient, note_id: str, persona: Persona) -> None:
    with app.state.session_factory() as session, session.begin():
        owner_id = uuid.UUID(me(client, persona))
        session.add(NoteOwner(note_id=uuid.UUID(note_id), user_id=owner_id, position=1, added_at=START))


def note_of(client: ContractClient, persona: Persona, note_id: str) -> httpx.Response:
    return client.get(f"/v1/notes/{note_id}", auth=persona)


def test_two_merges_on_one_note_cannot_overwrite_each_other(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, restore_hooks: None
) -> None:
    note_id, note_etag = proposer_note(client, ada, ben)
    a = submit(client, ben, note_id, proposal(note_etag, explanation="A"))
    b = submit(
        client,
        ben,
        note_id,
        proposal(
            note_etag, explanation="B", proposedContent={"title": "Release checklist", "body": ANNOUNCE}
        ),
    )
    competitor = ContractClient(app)

    def merge_b_first() -> None:
        response = merge(competitor, ada, b.json()["id"], b.headers["ETag"], {"expectedNoteETag": note_etag})
        assert response.status_code == 200, response.text

    fired = hook("merge_edit_request", merge_b_first)
    lost = merge(client, ada, a.json()["id"], a.headers["ETag"], {"expectedNoteETag": note_etag})
    assert lost.status_code == 412 and fired == ["merge_edit_request"]
    assert get_request(client, ada, a.json()["id"]).json()["status"] == "open"
    live = note_of(client, ada, note_id)
    assert live.json()["body"] == ANNOUNCE  # the first merge's result is intact
    # The loser previews again against the new version and merges cleanly: separate regions combine.
    again = preview(client, ada, a.json()["id"]).json()
    assert again["canMerge"] is True and again["currentNoteETag"] == live.headers["ETag"]
    merged = merge(client, ada, a.json()["id"], a.headers["ETag"], {"expectedNoteETag": live.headers["ETag"]})
    assert merged.status_code == 200, merged.text
    assert (
        merged.json()["note"]["body"]
        == "## Release\n\n- Run tests\n- Review metrics\n- Deploy\n- Announce in #releases\n"
    )
    assert get_request(client, ada, b.json()["id"]).json()["status"] == "merged"  # merging A did not touch B


def test_a_revision_committed_first_makes_the_merge_stale(
    client: ContractClient,
    app: FastAPI,
    examples: dict[str, Any],
    ada: Persona,
    ben: Persona,
    restore_hooks: None,
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    competitor = ContractClient(app)
    revised: dict[str, str] = {}

    def revise_first() -> None:
        response = revise(competitor, ben, request_id, request_etag, examples["ReviseEditRequestRequest"])
        assert response.status_code == 200, response.text
        revised["etag"] = response.headers["ETag"]

    fired = hook("merge_edit_request", revise_first)
    stale = merge(client, ada, request_id, request_etag, {"expectedNoteETag": note_etag})
    assert stale.status_code == 412 and fired == ["merge_edit_request"]
    assert note_of(client, ada, note_id).headers["ETag"] == note_etag  # nothing was written
    assert get_request(client, ada, request_id).json()["status"] == "open"
    # Reviewing the revision, the owner merges what was actually proposed.
    merged = merge(client, ada, request_id, revised["etag"], {"expectedNoteETag": note_etag})
    assert merged.status_code == 200, merged.text
    assert merged.json()["note"]["title"] == "Release runbook"


@pytest.mark.parametrize("transition", ["withdraw", "reject"])
def test_a_close_committed_first_permits_no_merge(
    client: ContractClient,
    app: FastAPI,
    examples: dict[str, Any],
    ada: Persona,
    ben: Persona,
    restore_hooks: None,
    transition: str,
) -> None:
    note_id, note_etag, request_id, request_etag = submitted_example(client, examples, ada, ben)
    competitor = ContractClient(app)
    closed: dict[str, str] = {}

    def close_first() -> None:
        if transition == "withdraw":
            response = withdraw(competitor, ben, request_id, request_etag)
        else:
            response = reject(competitor, ada, request_id, request_etag)
        assert response.status_code == 200, response.text
        closed["etag"] = response.headers["ETag"]

    fired = hook("merge_edit_request", close_first)
    stale = merge(client, ada, request_id, request_etag, {"expectedNoteETag": note_etag})
    assert stale.status_code == 412 and fired == ["merge_edit_request"]
    current = merge(client, ada, request_id, closed["etag"], {"expectedNoteETag": note_etag})
    assert current.status_code == 409 and current.json()["code"] == "request_not_open"
    assert note_of(client, ada, note_id).headers["ETag"] == note_etag
    assert get_request(client, ada, request_id).json()["status"] == (
        "withdrawn" if transition == "withdraw" else "rejected"
    )


def test_a_note_edit_or_a_revision_after_a_preview_makes_the_merge_stale(
    client: ContractClient, examples: dict[str, Any], ada: Persona, ben: Persona
) -> None:
    note_id, note_etag, request_id, _ = submitted_example(client, examples, ada, ben)
    previewed = preview(client, ada, request_id).json()
    moved = client.patch(
        f"/v1/notes/{note_id}", auth=ada, if_match=note_etag, json={"tags": ["release", "ops"]}
    )
    assert moved.status_code == 200  # even a tags-only change moves the note's version
    stale = merge(
        client, ada, request_id, previewed["requestETag"], {"expectedNoteETag": previewed["currentNoteETag"]}
    )
    assert stale.status_code == 412
    fresh = preview(client, ada, request_id).json()
    assert (
        fresh["currentNoteETag"] == moved.headers["ETag"] and fresh["requestETag"] == previewed["requestETag"]
    )
    # A revision after this preview moves the request's version.
    revised = revise(client, ben, request_id, fresh["requestETag"], {"explanation": "after the preview"})
    assert revised.status_code == 200
    assert (
        merge(
            client, ada, request_id, fresh["requestETag"], {"expectedNoteETag": fresh["currentNoteETag"]}
        ).status_code
        == 412
    )
    final = preview(client, ada, request_id).json()
    merged = merge(
        client, ada, request_id, final["requestETag"], {"expectedNoteETag": final["currentNoteETag"]}
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["note"]["tags"] == ["release", "ops"]  # current tags are preserved by the merge


def test_access_revoked_before_a_revision_commits_blocks_it(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, restore_hooks: None
) -> None:
    note_id, note_etag = proposer_note(client, ada, ben)
    submitted = submit(client, ben, note_id, proposal(note_etag))
    request_id, request_etag = submitted.json()["id"], submitted.headers["ETag"]
    share_id = client.get(f"/v1/notes/{note_id}/shares", auth=ada).json()["items"][0]["id"]
    competitor = ContractClient(app)

    def downgrade_first() -> None:
        url = f"/v1/notes/{note_id}/shares/{share_id}"
        assert competitor.patch(url, auth=ada, json={"permissions": ["read"]}).status_code == 200

    fired = hook("revise_edit_request", downgrade_first)
    refused = revise(client, ben, request_id, request_etag, {"explanation": "sneaky"})
    assert refused.status_code == 403 and fired == ["revise_edit_request"]
    assert get_request(client, ben, request_id).json()["explanation"] != "sneaky"


def test_concurrent_merges_of_two_requests_permit_exactly_one(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, cara: Persona
) -> None:
    note_id, note_etag = proposer_note(client, ada, ben)
    add_owner(app, client, note_id, cara)  # two owners under self_merge: either may merge alone
    a = submit(client, ben, note_id, proposal(note_etag, explanation="A"))
    b = submit(
        client,
        ben,
        note_id,
        proposal(
            note_etag, explanation="B", proposedContent={"title": "Release checklist", "body": ANNOUNCE}
        ),
    )
    barrier = threading.Barrier(2)
    outcomes: list[int] = []
    lock = threading.Lock()

    def act(persona: Persona, request: httpx.Response) -> None:
        own = ContractClient(app)
        barrier.wait()
        response = merge(
            own, persona, request.json()["id"], request.headers["ETag"], {"expectedNoteETag": note_etag}
        )
        with lock:
            outcomes.append(response.status_code)

    threads = [threading.Thread(target=act, args=(ada, a)), threading.Thread(target=act, args=(cara, b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [200, 412], outcomes
    statuses = sorted(get_request(client, ada, r.json()["id"]).json()["status"] for r in (a, b))
    assert statuses == ["merged", "open"]
    live = note_of(client, ada, note_id).json()["body"]
    assert live in (a.json()["proposedContent"]["body"], ANNOUNCE)  # exactly the winner's candidate


def test_a_merge_and_a_revision_racing_on_one_request_etag_permit_exactly_one(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    note_id, note_etag = proposer_note(client, ada, ben)
    submitted = submit(client, ben, note_id, proposal(note_etag))
    request_id, request_etag = submitted.json()["id"], submitted.headers["ETag"]
    barrier = threading.Barrier(2)
    outcomes: list[int] = []
    lock = threading.Lock()

    def merge_it() -> None:
        own = ContractClient(app)
        barrier.wait()
        response = merge(own, ada, request_id, request_etag, {"expectedNoteETag": note_etag})
        with lock:
            outcomes.append(response.status_code)

    def revise_it() -> None:
        own = ContractClient(app)
        barrier.wait()
        response = revise(own, ben, request_id, request_etag, {"explanation": "racing"})
        with lock:
            outcomes.append(response.status_code)

    threads = [threading.Thread(target=merge_it), threading.Thread(target=revise_it)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [200, 412], outcomes
    final = get_request(client, ada, request_id).json()
    merged_won = final["status"] == "merged" and final["explanation"] != "racing"
    revision_won = final["status"] == "open" and final["explanation"] == "racing"
    assert merged_won or revision_won, final
