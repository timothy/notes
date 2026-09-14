"""Acceptance row "Trash": "Trashing atomically removes shares and freezes comments/requests, and the 204
carries the trash ETag. Only owner can inspect preserved records. Restore is private and preserves request
states. At the exact expiry boundary, reads and restoration fail before physical purge." (Comments and
requests arrive in later slices; their freezing is asserted there.)"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from notes_api import uow
from notes_api.models import Share
from tests.contract_client import ContractClient
from tests.support import FakeClock, Persona
from tests.test_notes import create, grant, me

pytestmark = pytest.mark.acceptance("Trash")

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
EXPIRES = START + timedelta(hours=720)
EXPIRES_TS = "2026-10-13T12:00:00.000000Z"


def share_count(app: FastAPI, note_id: str) -> int:
    with app.state.session_factory() as session:
        count: int = session.execute(
            select(func.count()).select_from(Share).where(Share.note_id == uuid.UUID(note_id))
        ).scalar_one()
        return count


def trash(client: ContractClient, persona: Persona, note_id: str, etag: str | None) -> httpx.Response:
    return client.delete(f"/v1/notes/{note_id}", auth=persona, if_match=etag)


def restore(
    client: ContractClient, persona: Persona, note_id: str, etag: str | None, **kwargs: Any
) -> httpx.Response:
    return client.post(f"/v1/notes/{note_id}/restore", auth=persona, if_match=etag, **kwargs)


def test_trashing_removes_shares_and_the_204_carries_the_trash_etag(
    client: ContractClient, app: FastAPI, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    created = create(client, ada)
    note_id, active_etag = created.json()["id"], created.headers["ETag"]
    grant(app, note_id, me(client, ben), comment=True)
    assert client.get(f"/v1/notes/{note_id}", auth=ben).status_code == 200
    clock.advance(timedelta(minutes=1))

    response = trash(client, ada, note_id, active_etag)
    assert response.status_code == 204 and response.content == b""
    trash_etag = response.headers["ETag"]
    assert trash_etag != active_etag

    assert share_count(app, note_id) == 0
    assert client.get(f"/v1/notes/{note_id}", auth=ben).status_code == 404
    seen = client.get(f"/v1/notes/{note_id}", auth=ada)
    assert seen.status_code == 200 and seen.headers["ETag"] == trash_etag
    assert seen.json()["deletedAt"] == "2026-09-13T12:01:00.000000Z"
    assert seen.json()["expiresAt"] == "2026-10-13T12:01:00.000000Z"
    assert seen.json()["updatedAt"] == "2026-09-13T12:01:00.000000Z"
    assert seen.json()["title"] == "Release checklist" and seen.json()["tags"] == ["release"]


def test_a_repeated_trash_with_the_trash_etag_is_idempotent(
    client: ContractClient, clock: FakeClock, ada: Persona
) -> None:
    created = create(client, ada)
    note_id, active_etag = created.json()["id"], created.headers["ETag"]
    trash_etag = trash(client, ada, note_id, active_etag).headers["ETag"]
    clock.advance(timedelta(days=3))
    again = trash(client, ada, note_id, trash_etag)
    assert again.status_code == 204 and again.headers["ETag"] == trash_etag
    note = client.get(f"/v1/notes/{note_id}", auth=ada).json()
    assert note["expiresAt"] == EXPIRES_TS and note["deletedAt"] == "2026-09-13T12:00:00.000000Z"
    stale = trash(client, ada, note_id, active_etag)
    assert stale.status_code == 412 and stale.json()["code"] == "precondition_failed"


def test_trash_walks_the_ladder(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona, cara: Persona
) -> None:
    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    grant(app, note_id, me(client, ben), propose=True)
    assert trash(client, cara, note_id, etag).status_code == 404
    assert trash(client, ben, note_id, etag).status_code == 403
    assert trash(client, ben, note_id, None).status_code == 403
    missing = trash(client, ada, note_id, None)
    assert missing.status_code == 428 and missing.json()["code"] == "precondition_required"
    malformed = trash(client, ada, note_id, 'W/"x"')
    assert malformed.status_code == 400 and malformed.json()["errors"][0]["pointer"] == "If-Match"
    assert trash(client, ada, note_id, '"0123456789abcdef0123456789abcdef"').status_code == 412
    assert trash(client, ada, str(uuid.uuid4()), etag).status_code == 404
    assert client.delete("/v1/notes/not-a-uuid", auth=ada, if_match=etag).status_code == 422
    assert client.delete(f"/v1/notes/{note_id}", if_match=etag).status_code == 401
    assert share_count(app, note_id) == 1  # nothing above changed the note
    assert client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"] == etag


def test_restore_is_private_advances_the_etag_and_keeps_content(
    client: ContractClient, app: FastAPI, clock: FakeClock, ada: Persona, ben: Persona
) -> None:
    created = create(client, ada, tags=["a", "b"])
    note_id, active_etag = created.json()["id"], created.headers["ETag"]
    grant(app, note_id, me(client, ben))
    trash_etag = trash(client, ada, note_id, active_etag).headers["ETag"]
    clock.advance(timedelta(hours=2))

    assert restore(client, ada, note_id, active_etag).status_code == 412
    assert restore(client, ada, note_id, None).status_code == 428
    restored = restore(
        client, ada, note_id, trash_etag, content="ignored", headers={"Content-Type": "text/plain"}
    )
    assert restored.status_code == 200
    body = restored.json()
    assert body["deletedAt"] is None and body["expiresAt"] is None
    assert body["updatedAt"] == "2026-09-13T14:00:00.000000Z"
    assert body["title"] == "Release checklist" and body["tags"] == ["a", "b"]
    assert body["body"] == created.json()["body"]
    restored_etag = restored.headers["ETag"]
    assert restored_etag not in (active_etag, trash_etag)
    assert client.get(f"/v1/notes/{note_id}", auth=ada).headers["ETag"] == restored_etag
    # Private again: the former reader's share is gone for good.
    assert client.get(f"/v1/notes/{note_id}", auth=ben).status_code == 404
    assert share_count(app, note_id) == 0
    already = restore(client, ada, note_id, restored_etag)
    assert already.status_code == 409 and already.json()["code"] == "note_already_active"
    assert restore(client, ada, note_id, trash_etag).status_code == 412


def test_a_trashed_note_refuses_content_changes_and_restore_needs_the_owner(
    client: ContractClient, app: FastAPI, ada: Persona, ben: Persona
) -> None:
    created = create(client, ada)
    note_id, active_etag = created.json()["id"], created.headers["ETag"]
    grant(app, note_id, me(client, ben), propose=True)
    trash_etag = trash(client, ada, note_id, active_etag).headers["ETag"]
    refused = client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=trash_etag, json={"title": "x"})
    assert refused.status_code == 409 and refused.json()["code"] == "note_not_active"
    # ben lost his share when the note was trashed, so the note no longer exists for him.
    assert restore(client, ben, note_id, trash_etag).status_code == 404
    assert (
        client.patch(f"/v1/notes/{note_id}", auth=ben, if_match=trash_etag, json={"title": "x"}).status_code
        == 404
    )


def test_at_the_expiry_instant_reads_and_restore_fail_and_a_microsecond_earlier_they_work(
    client: ContractClient, clock: FakeClock, ada: Persona
) -> None:
    created = create(client, ada)
    note_id, active_etag = created.json()["id"], created.headers["ETag"]
    trash_etag = trash(client, ada, note_id, active_etag).headers["ETag"]

    clock.set(EXPIRES - timedelta(microseconds=1))
    assert client.get(f"/v1/notes/{note_id}", auth=ada).status_code == 200
    assert trash(client, ada, note_id, trash_etag).status_code == 204

    clock.set(EXPIRES)
    assert client.get(f"/v1/notes/{note_id}", auth=ada).status_code == 404
    assert restore(client, ada, note_id, trash_etag).status_code == 404
    assert trash(client, ada, note_id, trash_etag).status_code == 404
    assert (
        client.patch(f"/v1/notes/{note_id}", auth=ada, if_match=trash_etag, json={"title": "x"}).status_code
        == 404
    )

    clock.set(EXPIRES - timedelta(microseconds=1))
    assert restore(client, ada, note_id, trash_etag).status_code == 200
    clock.set(EXPIRES + timedelta(days=400))
    assert client.get(f"/v1/notes/{note_id}", auth=ada).status_code == 200  # restored notes never expire


def test_a_competing_trash_committed_first_makes_the_second_stale(
    client: ContractClient, app: FastAPI, ada: Persona, restore_hooks: None
) -> None:
    created = create(client, ada)
    note_id, etag = created.json()["id"], created.headers["ETag"]
    competitor = ContractClient(app)
    fired: list[str] = []

    def before_begin(op: str) -> None:
        if op == "trash_note" and not fired:
            fired.append(op)
            assert competitor.delete(f"/v1/notes/{note_id}", auth=ada, if_match=etag).status_code == 204

    uow.hooks.before_begin = before_begin
    second = trash(client, ada, note_id, etag)
    assert second.status_code == 412 and fired == ["trash_note"]
    assert client.get(f"/v1/notes/{note_id}", auth=ada).json()["deletedAt"] is not None
