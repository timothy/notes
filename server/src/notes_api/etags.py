"""Opaque strong ETags: one random version per resource state, quoted on the wire.

Versions are ``secrets.token_hex(16)`` strings stored per resource and compared by equality. The
``If-Match`` parser enforces the contract's rules: the header is required (``428``), and it must hold
exactly one strong tag, so weak validators, ``*``, and lists are ``400`` with an error naming the header.
"""

from __future__ import annotations

import re
import secrets

from starlette.datastructures import Headers

from notes_api.contract import FieldError
from notes_api.http.problems import MalformedRequest, PreconditionRequired

# openapi.yaml, StrongETag: one quoted tag, no weak prefix, no wildcard, no list, at most 256 characters.
STRONG_ETAG = re.compile(r'^"[\x21\x23-\x7E]+"$')
MAX_ETAG_LENGTH = 256
IF_MATCH_DETAIL = "weak validators, wildcards, and lists are not accepted"


def new_version() -> str:
    return secrets.token_hex(16)


def quote(version: str) -> str:
    return f'"{version}"'


def read_if_match(headers: Headers) -> str:
    """Reject repeated header fields as well as comma-separated lists, at the precondition rung."""
    values = headers.getlist("if-match")
    if len(values) > 1:
        raise MalformedRequest(
            detail="If-Match must contain exactly one strong entity tag.",
            errors=[FieldError("header", "If-Match", IF_MATCH_DETAIL)],
        )
    return parse_if_match(values[0] if values else None)


def parse_if_match(value: str | None) -> str:
    """Return the single strong tag carried by ``If-Match``, or raise the contract's 428 or 400 Problem."""
    if value is None:
        raise PreconditionRequired()
    if len(value) > MAX_ETAG_LENGTH or not STRONG_ETAG.match(value):
        raise MalformedRequest(
            detail="If-Match must contain exactly one strong entity tag.",
            errors=[FieldError("header", "If-Match", IF_MATCH_DETAIL)],
        )
    return value
