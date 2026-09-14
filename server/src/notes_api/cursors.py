"""Opaque keyset cursors and the pagination helper every list operation uses.

A cursor is base64url JSON ``{"k": [microseconds, id], "f": fingerprint}``: the sort key of the last row
of the page and a fingerprint of the caller, the collection, the filters, and the limit. Any decoding
failure or fingerprint mismatch is the contract's ``400 invalid_cursor``. No signature: authorization is
re-applied on every page, so a forged cursor can only reposition the caller's own view.

Pages are read with a row-value comparison ``(moment, id) < (:moment, :id)`` in ``moment DESC, id DESC``
order and one row more than the limit, which tells whether a next page exists without counting.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, tuple_
from sqlalchemy.orm import QueryableAttribute, Session

from notes_api.http.problems import InvalidCursor

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MICROSECOND = timedelta(microseconds=1)
FINGERPRINT_LENGTH = 16


@dataclass(frozen=True, slots=True)
class Key:
    """The sort key of one row: its timestamp (microsecond precision) and its id."""

    moment: datetime
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


def fingerprint(caller_id: uuid.UUID, collection: str, filters: Mapping[str, object], limit: int) -> str:
    view = {"caller": str(caller_id), "collection": collection, "filters": filters, "limit": limit}
    canonical = json.dumps(view, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:FINGERPRINT_LENGTH]


def microseconds(moment: datetime) -> int:
    return (moment - EPOCH) // MICROSECOND


def encode(key: Key, fingerprint: str) -> str:
    payload = {"k": [microseconds(key.moment), key.id.hex], "f": fingerprint}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode(cursor: str, fingerprint: str) -> Key:
    """The key a cursor points at.

    ``InvalidCursor`` when it is malformed or was issued for another view.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload: Any = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("cursor payload is not an object")
        sort_key, issued_for = payload["k"], payload["f"]
        if (
            not isinstance(sort_key, list)
            or len(sort_key) != 2
            or not isinstance(sort_key[0], int)
            or isinstance(sort_key[0], bool)
            or not isinstance(sort_key[1], str)
            or not isinstance(issued_for, str)
        ):
            raise ValueError("cursor payload has the wrong shape")
        # Integer arithmetic keeps every microsecond; float seconds would round at this magnitude.
        key = Key(EPOCH + sort_key[0] * MICROSECOND, uuid.UUID(hex=sort_key[1]))
    except (ValueError, TypeError, KeyError, OverflowError):
        raise InvalidCursor() from None
    if issued_for != fingerprint:
        raise InvalidCursor()
    return key


def paginate[T](
    session: Session,
    statement: Select[tuple[T]],
    *,
    moment: QueryableAttribute[datetime],
    id_column: QueryableAttribute[uuid.UUID],
    key_of: Callable[[T], Key],
    limit: int,
    cursor: str | None,
    fingerprint: str,
) -> Page[T]:
    """One page of ``statement`` sorted ``moment DESC, id DESC``, continuing after ``cursor`` when given."""
    if cursor is not None:
        after = decode(cursor, fingerprint)
        statement = statement.where(tuple_(moment, id_column) < (after.moment, after.id))
    statement = statement.order_by(moment.desc(), id_column.desc()).limit(limit + 1)
    rows = list(session.execute(statement).scalars())
    items = rows[:limit]
    next_cursor = encode(key_of(items[-1]), fingerprint) if len(rows) > limit else None
    return Page(items, next_cursor)
