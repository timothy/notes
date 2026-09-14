"""Cursors round-trip their key exactly, reject anything malformed or issued for another view, and page a
table in ``moment DESC, id DESC`` order."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from notes_api import cursors
from notes_api.cursors import Key, decode, encode, fingerprint, microseconds
from notes_api.http.problems import InvalidCursor
from notes_api.models import User

CALLER = uuid.UUID("11111111-1111-4111-8111-111111111111")
VIEW = fingerprint(CALLER, "users", {}, 25)
KEY = Key(
    datetime(2026, 9, 13, 12, 0, 0, 123456, tzinfo=UTC), uuid.UUID("22222222-2222-4222-8222-222222222222")
)


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
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


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


def test_pagination_orders_by_moment_then_id_and_stops_exactly(app: FastAPI) -> None:
    moment = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    ids = sorted(uuid.uuid4() for _ in range(3))
    older = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")  # the largest id, but one microsecond older
    with app.state.session_factory() as session, session.begin():
        for index, user_id in enumerate(ids):
            session.add(
                User(id=user_id, issuer="i", subject=f"s{index}", display_name="x", created_at=moment)
            )
        earlier = moment - timedelta(microseconds=1)
        session.add(User(id=older, issuer="i", subject="older", display_name="x", created_at=earlier))
    view = fingerprint(CALLER, "users", {}, 2)

    def page(cursor: str | None) -> cursors.Page[User]:
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
            )

    first = page(None)
    assert [row.id for row in first.items] == [ids[2], ids[1]]
    assert first.next_cursor is not None
    second = page(first.next_cursor)
    assert [row.id for row in second.items] == [ids[0], older]
    assert second.next_cursor is None
