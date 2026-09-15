"""Cursors round-trip their key exactly, reject anything malformed or issued for another view, and page a
table in ``moment DESC, id DESC`` order, or oldest first for the collections the contract sorts that way."""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from notes_api import cursors
from notes_api.cursors import CursorCodec, Key, fingerprint, microseconds
from notes_api.http.problems import InvalidCursor
from notes_api.models import User
from tests.support import FakeClock

CALLER = uuid.UUID("11111111-1111-4111-8111-111111111111")
VIEW = fingerprint(CALLER, "users", {}, 25)
KEY = Key(
    datetime(2026, 9, 13, 12, 0, 0, 123456, tzinfo=UTC), uuid.UUID("22222222-2222-4222-8222-222222222222")
)


SIGNING_KEY = bytes.fromhex("01" * 32)
CODEC = CursorCodec(SIGNING_KEY, FakeClock(KEY.moment))
encode = CODEC.encode
decode = CODEC.decode


def test_a_cursor_round_trips_its_key_exactly() -> None:
    cursor = encode(KEY, VIEW)
    assert cursor.isascii() and "=" not in cursor and "+" not in cursor and "/" not in cursor
    assert decode(cursor, VIEW) == KEY


def test_microseconds_survive_far_into_the_future() -> None:
    far = Key(datetime(2255, 6, 5, 23, 47, 34, 999999, tzinfo=UTC), uuid.uuid4())  # about 2**53 microseconds
    assert microseconds(far.moment) > 2**53
    assert decode(encode(far, VIEW), VIEW) == far
    assert microseconds(cursors.EPOCH) == 0


def test_a_cursor_for_another_view_is_rejected() -> None:
    cursor = encode(KEY, VIEW)
    for other in (
        fingerprint(uuid.uuid4(), "users", {}, 25),
        fingerprint(CALLER, "teams", {}, 25),
        fingerprint(CALLER, "users", {"scope": "mine"}, 25),
        fingerprint(CALLER, "users", {}, 26),
    ):
        with pytest.raises(InvalidCursor):
            decode(cursor, other)


def test_the_fingerprint_does_not_depend_on_filter_order() -> None:
    assert fingerprint(CALLER, "notes", {"a": 1, "b": [2, 3]}, 5) == fingerprint(
        CALLER, "notes", {"b": [2, 3], "a": 1}, 5
    )


def tampered(payload: object) -> str:
    if isinstance(payload, dict):
        payload = {"exp": microseconds(KEY.moment + timedelta(hours=24)), **payload}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signed = "v1." + encoded
    signature = (
        base64.urlsafe_b64encode(hmac.digest(SIGNING_KEY, signed.encode(), "sha256")).rstrip(b"=").decode()
    )
    return signed + "." + signature


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "!!!",
        "abc",
        "x" * 5000,
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        tampered("a string"),
        tampered([1, 2]),
        tampered({"k": [1, "0" * 32]}),
        tampered({"k": "1", "f": VIEW}),
        tampered({"k": [1.5, "0" * 32], "f": VIEW}),
        tampered({"k": [True, "0" * 32], "f": VIEW}),
        tampered({"k": [1, "not-a-uuid"], "f": VIEW}),
        tampered({"k": [1, 2], "f": VIEW}),
        tampered({"k": [10**30, "0" * 32], "f": VIEW}),
        tampered({"k": [1, "0" * 32], "f": 5}),
    ],
    ids=[
        "empty",
        "not base64",
        "not json",
        "oversize garbage",
        "not utf-8",
        "string payload",
        "list payload",
        "no fingerprint",
        "key not a list",
        "float microseconds",
        "boolean microseconds",
        "bad uuid",
        "numeric uuid",
        "overflowing microseconds",
        "numeric fingerprint",
    ],
)
def test_malformed_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(InvalidCursor):
        decode(cursor, VIEW)


MOMENT = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def seed_users(app: FastAPI, rows: list[tuple[uuid.UUID, datetime]]) -> None:
    with app.state.session_factory() as session, session.begin():
        for index, (user_id, moment) in enumerate(rows):
            session.add(
                User(id=user_id, issuer="i", subject=f"s{index}", display_name="x", created_at=moment)
            )


def page(app: FastAPI, view: str, cursor: str | None, *, descending: bool = True) -> cursors.Page[User]:
    with app.state.session_factory() as session:
        return cursors.paginate(
            session,
            select(User),
            moment=User.created_at,
            id_column=User.id,
            key_of=lambda row: Key(row.created_at, row.id),
            limit=2,
            cursor=cursor,
            fingerprint=view,
            codec=app.state.cursor_codec,
            descending=descending,
        )


def test_pagination_orders_by_moment_then_id_and_stops_exactly(app: FastAPI) -> None:
    ids = sorted(uuid.uuid4() for _ in range(3))
    older = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")  # the largest id, but one microsecond older
    seed_users(app, [(user_id, MOMENT) for user_id in ids] + [(older, MOMENT - timedelta(microseconds=1))])
    view = fingerprint(CALLER, "users", {}, 2)
    first = page(app, view, None)
    assert [row.id for row in first.items] == [ids[2], ids[1]]
    assert first.next_cursor is not None
    second = page(app, view, first.next_cursor)
    assert [row.id for row in second.items] == [ids[0], older]
    assert second.next_cursor is None


def test_ascending_pagination_orders_oldest_first_then_by_id_and_stops_exactly(app: FastAPI) -> None:
    ids = sorted(uuid.uuid4() for _ in range(3))
    newer = uuid.UUID("00000000-0000-4000-8000-000000000000")  # the smallest id, but one microsecond newer
    seed_users(app, [(user_id, MOMENT) for user_id in ids] + [(newer, MOMENT + timedelta(microseconds=1))])
    view = fingerprint(CALLER, "comments", {}, 2)
    first = page(app, view, None, descending=False)
    assert [row.id for row in first.items] == [ids[0], ids[1]]
    assert first.next_cursor is not None
    second = page(app, view, first.next_cursor, descending=False)
    assert [row.id for row in second.items] == [ids[2], newer]
    assert second.next_cursor is None
    # The direction is a property of the collection, whose name is in the fingerprint, so a cursor cannot
    # continue a view of the other kind.
    with pytest.raises(InvalidCursor):
        page(app, fingerprint(CALLER, "users", {}, 2), first.next_cursor)


def test_cursor_works_across_replicas_and_not_across_signing_keys() -> None:
    replica = CursorCodec(SIGNING_KEY, FakeClock(KEY.moment))
    token = encode(KEY, VIEW)
    assert replica.decode(token, VIEW) == KEY
    with pytest.raises(InvalidCursor):
        CursorCodec(b"x" * 32, FakeClock(KEY.moment)).decode(token, VIEW)


@pytest.mark.parametrize(
    "transform",
    [
        lambda token: token.replace("v1.", "v2.", 1),
        lambda token: token.rsplit(".", 1)[0] + "." + "A" * 43,
        lambda token: token + "=",
        lambda token: token.split(".")[1],
    ],
)
def test_version_signature_encoding_and_legacy_cursors_are_rejected(transform: object) -> None:
    assert callable(transform)
    with pytest.raises(InvalidCursor):
        decode(transform(encode(KEY, VIEW)), VIEW)


@pytest.mark.parametrize("expiry", [True, "tomorrow", microseconds(KEY.moment)])
def test_authenticated_but_invalid_expiry_is_rejected(expiry: object) -> None:
    token = tampered({"k": [microseconds(KEY.moment), KEY.id.hex], "f": VIEW, "exp": expiry})
    with pytest.raises(InvalidCursor):
        decode(token, VIEW)
