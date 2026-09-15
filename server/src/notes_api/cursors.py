"""Opaque keyset cursors and the pagination helper every list operation uses.

A cursor is ``v1.<base64url JSON>.<base64url HMAC-SHA256>``. Its authenticated payload contains the sort
key, the caller/collection/filter/limit fingerprint, and expiry in integer Unix microseconds. Cursors
expire after 24 hours. All replicas share an explicitly configured key; authorization is still applied
on every page. Legacy unsigned cursors and any verification failure are ``400 invalid_cursor``.

Pages are read with a row-value comparison ``(moment, id) < (:moment, :id)`` in ``moment DESC, id DESC``
order (or ``>`` in ascending order for the collections the contract sorts oldest first, such as comments)
and one row more than the limit, which tells whether a next page exists without counting.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, tuple_
from sqlalchemy.orm import QueryableAttribute, Session

from notes_api.clock import Clock
from notes_api.http.problems import InvalidCursor

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MICROSECOND = timedelta(microseconds=1)
FINGERPRINT_LENGTH = 16
CURSOR_LIFETIME = timedelta(hours=24)


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


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _encode(raw) != value:
        raise ValueError("noncanonical base64url")
    return raw


class CursorCodec:
    def __init__(self, signing_key: bytes, clock: Clock) -> None:
        if len(signing_key) != 32:
            raise ValueError("cursor signing key must be 32 bytes")
        self._signing_key = signing_key
        self._clock = clock

    def encode(self, key: Key, fingerprint: str) -> str:
        payload = {
            "k": [microseconds(key.moment), key.id.hex],
            "f": fingerprint,
            "exp": microseconds(self._clock.now() + CURSOR_LIFETIME),
        }
        signed = "v1." + _encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = hmac.digest(self._signing_key, signed.encode("ascii"), "sha256")
        return signed + "." + _encode(signature)

    def decode(self, cursor: str, fingerprint: str) -> Key:
        try:
            if len(cursor) > 4096:
                raise ValueError("oversize cursor")
            version, encoded, signature = cursor.split(".")
            if version != "v1":
                raise ValueError("unknown cursor version")
            signed = version + "." + encoded
            expected = hmac.digest(self._signing_key, signed.encode("ascii"), "sha256")
            if not hmac.compare_digest(expected, _decode(signature)):
                raise ValueError("invalid signature")
            payload = json.loads(_decode(encoded))
            if not isinstance(payload, dict) or set(payload) != {"k", "f", "exp"}:
                raise ValueError("invalid cursor payload")
            sort_key, issued_for, expires = payload["k"], payload["f"], payload["exp"]
            if (
                not isinstance(sort_key, list)
                or len(sort_key) != 2
                or type(sort_key[0]) is not int
                or not isinstance(sort_key[1], str)
                or not isinstance(issued_for, str)
                or type(expires) is not int
                or expires <= microseconds(self._clock.now())
                or issued_for != fingerprint
            ):
                raise ValueError("invalid or expired cursor")
            return Key(EPOCH + sort_key[0] * MICROSECOND, uuid.UUID(hex=sort_key[1]))
        except (ValueError, TypeError, KeyError, OverflowError):
            raise InvalidCursor() from None


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
    codec: CursorCodec,
    descending: bool = True,
) -> Page[T]:
    """One page of ``statement`` in ``moment DESC, id DESC`` order (ascending when ``descending`` is false),
    continuing after ``cursor`` when given."""
    if cursor is not None:
        after = codec.decode(cursor, fingerprint)
        position, last = tuple_(moment, id_column), (after.moment, after.id)
        statement = statement.where(position < last if descending else position > last)
    order = (moment.desc(), id_column.desc()) if descending else (moment.asc(), id_column.asc())
    statement = statement.order_by(*order).limit(limit + 1)
    rows = list(session.execute(statement).scalars())
    items = rows[:limit]
    next_cursor = codec.encode(key_of(items[-1]), fingerprint) if len(rows) > limit else None
    return Page(items, next_cursor)
