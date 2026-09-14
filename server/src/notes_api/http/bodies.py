"""Read and validate JSON request bodies in the contract's order.

Handlers are plain ``def`` functions running in FastAPI's threadpool, and bodies are never declared as
FastAPI parameters, so the order of checks is ours: ``415`` for anything but ``application/json``,
``400 malformed_request`` for an unparseable body (or a missing one when required), then ``422`` with
field errors from the spec's own schema. An optional body that is omitted validates as ``{}``.
"""

from __future__ import annotations

import json
from typing import Any, cast

import anyio
from fastapi import Request

from notes_api.contract import Contract, nul_character_errors
from notes_api.http.problems import MalformedRequest, UnsupportedMediaType, ValidationFailed

JSON_MEDIA_TYPE = "application/json"


def parse_body(request: Request, schema_name: str, *, required: bool = True) -> dict[str, Any]:
    raw = anyio.from_thread.run(request.body)
    if not raw:
        if required:
            raise MalformedRequest(detail="A JSON request body is required.")
        data: Any = {}
    else:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != JSON_MEDIA_TYPE:
            raise UnsupportedMediaType()
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise MalformedRequest(detail="The request body is not well-formed JSON.") from exc
    contract = cast(Contract, request.app.state.contract)
    errors = contract.validate_body(schema_name, data)
    # The one rule beyond the schema: PostgreSQL text cannot hold U+0000, so no stored string may either.
    known = {error.pointer for error in errors}
    errors += [error for error in nul_character_errors(data) if error.pointer not in known]
    if errors:
        raise ValidationFailed(sorted(errors, key=lambda error: error.pointer))
    return cast(dict[str, Any], data)
