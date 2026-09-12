#!/usr/bin/env python3
"""Reproducible contract checks for the Notes API.

Run from the project root:

    .venv/bin/python scripts/validate_contract.py

Every check is static. The script validates the OpenAPI document, its references, every component
schema, the examples in both ``openapi.yaml`` and ``docs/design-guide.md``, authentication coverage,
conditional-mutation coverage, the endpoint inventory, required response headers and media types, the
must-fail / must-pass fixtures in ``tests/negative_cases.yaml``, the ``ErrorCode`` vocabulary against the
Problem examples, and the ownership and approval invariants JSON Schema cannot express. It never runs a
backend, so it proves nothing about authorization, transactions, or the merge algorithm (design guide,
section 6).

Exit status is 0 when every check passes and 1 otherwise.
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

try:
    import yaml
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
except ImportError as exc:  # pragma: no cover - environment problem, not a contract problem
    sys.stderr.write(
        f"missing dependency: {exc}\n"
        "install with: .venv/bin/python -m pip install -r requirements-dev.txt\n"
    )
    sys.exit(2)

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "openapi.yaml"
GUIDE_PATH = ROOT / "docs" / "design-guide.md"
CASES_PATH = ROOT / "tests" / "negative_cases.yaml"

# Absolute URI under which the document is registered so that '#/components/...' references resolve.
SPEC_URI = "https://notes-api.example.com/openapi.yaml"

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# ---------------------------------------------------------------------------------------------------
# Expectations derived from docs/design-guide.md. Change the guide first, then these tables.
# ---------------------------------------------------------------------------------------------------

EXPECTED_OPERATIONS: dict[tuple[str, str], str] = {
    ("GET", "/me"): "getCurrentUser",
    ("GET", "/users"): "listUsers",
    ("GET", "/users/{userId}"): "getUser",
    ("POST", "/teams"): "createTeam",
    ("GET", "/teams"): "listTeams",
    ("GET", "/teams/{teamId}"): "getTeam",
    ("PATCH", "/teams/{teamId}"): "updateTeam",
    ("DELETE", "/teams/{teamId}"): "deleteTeam",
    ("GET", "/teams/{teamId}/members"): "listMemberships",
    ("POST", "/teams/{teamId}/members"): "addMembership",
    ("PATCH", "/teams/{teamId}/members/{userId}"): "updateMembership",
    ("DELETE", "/teams/{teamId}/members/{userId}"): "removeMembership",
    ("POST", "/notes"): "createNote",
    ("GET", "/notes"): "listNotes",
    ("GET", "/notes/{noteId}"): "getNote",
    ("PATCH", "/notes/{noteId}"): "updateNote",
    ("DELETE", "/notes/{noteId}"): "trashNote",
    ("POST", "/notes/{noteId}/restore"): "restoreNote",
    ("POST", "/notes/{noteId}/owners"): "addOwner",
    ("DELETE", "/notes/{noteId}/owners/{userId}"): "removeOwner",
    ("PATCH", "/notes/{noteId}/review-policy"): "updateReviewPolicy",
    ("GET", "/notes/{noteId}/shares"): "listShares",
    ("POST", "/notes/{noteId}/shares"): "createShare",
    ("GET", "/notes/{noteId}/shares/{shareId}"): "getShare",
    ("PATCH", "/notes/{noteId}/shares/{shareId}"): "updateShare",
    ("DELETE", "/notes/{noteId}/shares/{shareId}"): "deleteShare",
    ("GET", "/notes/{noteId}/comments"): "listComments",
    ("POST", "/notes/{noteId}/comments"): "createComment",
    ("GET", "/notes/{noteId}/comments/{commentId}"): "getComment",
    ("PATCH", "/notes/{noteId}/comments/{commentId}"): "updateComment",
    ("DELETE", "/notes/{noteId}/comments/{commentId}"): "deleteComment",
    ("POST", "/notes/{noteId}/edit-requests"): "createEditRequest",
    ("GET", "/notes/{noteId}/edit-requests"): "listNoteEditRequests",
    ("GET", "/edit-requests"): "listEditRequests",
    ("GET", "/edit-requests/{requestId}"): "getEditRequest",
    ("PATCH", "/edit-requests/{requestId}"): "reviseEditRequest",
    ("POST", "/edit-requests/{requestId}/preview"): "previewEditRequest",
    ("POST", "/edit-requests/{requestId}/approve"): "approveEditRequest",
    ("POST", "/edit-requests/{requestId}/revoke-approval"): "revokeEditRequestApproval",
    ("POST", "/edit-requests/{requestId}/merge"): "mergeEditRequest",
    ("POST", "/edit-requests/{requestId}/reject"): "rejectEditRequest",
    ("POST", "/edit-requests/{requestId}/withdraw"): "withdrawEditRequest",
    ("GET", "/edit-requests/{requestId}/comments"): "listEditRequestComments",
    ("POST", "/edit-requests/{requestId}/comments"): "createEditRequestComment",
    ("GET", "/edit-requests/{requestId}/comments/{commentId}"): "getEditRequestComment",
    ("PATCH", "/edit-requests/{requestId}/comments/{commentId}"): "updateEditRequestComment",
    ("DELETE", "/edit-requests/{requestId}/comments/{commentId}"): "deleteEditRequestComment",
}

# Operations that mutate an existing ETag-bearing resource: If-Match required, 412 and 428 declared.
CONDITIONAL_OPERATIONS = frozenset(
    {
        "updateNote",
        "trashNote",
        "restoreNote",
        "addOwner",
        "removeOwner",
        "updateReviewPolicy",
        "updateComment",
        "deleteComment",
        "reviseEditRequest",
        "approveEditRequest",
        "revokeEditRequestApproval",
        "mergeEditRequest",
        "rejectEditRequest",
        "withdrawEditRequest",
        "updateEditRequestComment",
        "deleteEditRequestComment",
    }
)

# (operationId, status) pairs whose response must carry an ETag header. No other response may.
ETAG_RESPONSES = frozenset(
    {
        ("createNote", "201"),
        ("getNote", "200"),
        ("updateNote", "200"),
        ("trashNote", "204"),
        ("restoreNote", "200"),
        ("addOwner", "200"),
        ("removeOwner", "200"),
        ("updateReviewPolicy", "200"),
        ("createComment", "201"),
        ("getComment", "200"),
        ("updateComment", "200"),
        ("createEditRequest", "201"),
        ("getEditRequest", "200"),
        ("reviseEditRequest", "200"),
        ("approveEditRequest", "200"),
        ("revokeEditRequestApproval", "200"),
        ("mergeEditRequest", "200"),
        ("rejectEditRequest", "200"),
        ("withdrawEditRequest", "200"),
        ("createEditRequestComment", "201"),
        ("getEditRequestComment", "200"),
        ("updateEditRequestComment", "200"),
    }
)

CREATE_OPERATIONS = frozenset(
    {
        "createTeam",
        "addMembership",
        "createNote",
        "createShare",
        "createComment",
        "createEditRequest",
        "createEditRequestComment",
    }
)

SUCCESS_MEDIA_TYPE = "application/json"
PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_PREFIX = "https://notes-api.example.com/problems/"
GUIDE_BLOCK = re.compile(r"<!--\s*schema:\s*([A-Za-z0-9_]+)\s*-->\s*```json\s*\n(.*?)```", re.DOTALL)


# ---------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def resolve_pointer(doc: Any, ref: str) -> Any:
    """Resolve a local '#/a/b' reference inside ``doc``. Raises KeyError when it points nowhere."""
    if not ref.startswith("#/"):
        raise KeyError(ref)
    node = doc
    for raw in ref[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        elif isinstance(node, dict) and token in node:
            node = node[token]
        else:
            raise KeyError(ref)
    return node


def deref(doc: Any, node: Any) -> Any:
    """Follow ``$ref`` chains for OpenAPI objects (responses, parameters, examples, headers)."""
    seen: set[str] = set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen:
            raise ValueError(f"circular $ref {ref}")
        seen.add(ref)
        node = resolve_pointer(doc, ref)
    return node


def iter_refs(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}/{escape_pointer_token(str(key))}"
            if key == "$ref" and isinstance(value, str):
                yield child, value
            else:
                yield from iter_refs(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_refs(value, f"{path}/{index}")


def iter_operations(spec: dict) -> Iterator[tuple[str, str, dict, dict]]:
    """Yield (METHOD, path, operation, path_item) for every operation."""
    for path, item in (spec.get("paths") or {}).items():
        for method in HTTP_METHODS:
            if method in item:
                yield method.upper(), path, item[method], item


def operation_parameters(spec: dict, operation: dict, path_item: dict) -> list[dict]:
    params = [deref(spec, p) for p in path_item.get("parameters", [])]
    params += [deref(spec, p) for p in operation.get("parameters", [])]
    return params


def header_names(headers: dict | None) -> set[str]:
    return {name.lower() for name in (headers or {})}


def format_error(error: Any) -> str:
    where = "/" + "/".join(str(p) for p in error.absolute_path) if error.absolute_path else "(root)"
    return f"{where}: {error.message}"


class Contract:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        resource = Resource.from_contents(spec, default_specification=DRAFT202012)
        self.registry = Registry().with_resource(SPEC_URI, resource)

    def validator_for_pointer(self, pointer: str) -> Draft202012Validator:
        """A validator for the schema located at JSON pointer ``pointer`` (e.g. '/components/schemas/Note')."""
        return Draft202012Validator(
            {"$ref": f"{SPEC_URI}#{pointer}"},
            registry=self.registry,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )

    def validator_for_schema(self, name: str) -> Draft202012Validator:
        return self.validator_for_pointer(f"/components/schemas/{name}")

    def errors_for(self, pointer: str, instance: Any) -> list[str]:
        validator = self.validator_for_pointer(pointer)
        return [format_error(e) for e in sorted(validator.iter_errors(instance), key=str)]


# ---------------------------------------------------------------------------------------------------
# Checks. Each returns a list of problem strings; empty means PASS.
# ---------------------------------------------------------------------------------------------------


def check_document(spec: dict) -> list[str]:
    problems: list[str] = []
    version = spec.get("openapi")
    if version != "3.1.2":
        problems.append(f"openapi field is {version!r}, expected '3.1.2'")
    try:
        from openapi_spec_validator import validate as validate_openapi  # type: ignore
    except ImportError:  # pragma: no cover - older library versions
        from openapi_spec_validator import validate_spec as validate_openapi  # type: ignore
    try:
        validate_openapi(spec)
    except Exception as exc:  # the library raises a family of validation errors
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
        problems.append(f"openapi-spec-validator: {message}")
    return problems


def check_references(spec: dict) -> list[str]:
    problems: list[str] = []
    for location, ref in iter_refs(spec):
        if not ref.startswith("#/"):
            problems.append(f"{location}: non-local reference {ref!r}")
            continue
        try:
            resolve_pointer(spec, ref)
        except (KeyError, IndexError, ValueError):
            problems.append(f"{location}: dangling reference {ref!r}")
    return problems


def check_component_schemas(contract: Contract) -> list[str]:
    problems: list[str] = []
    schemas = contract.spec.get("components", {}).get("schemas", {})
    if not schemas:
        return ["no component schemas found"]
    for name, schema in schemas.items():
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            problems.append(f"components/schemas/{name}: {exc.message}")
            continue
        try:
            # Force reference resolution by validating a throwaway instance.
            list(contract.validator_for_schema(name).iter_errors({}))
        except Exception as exc:  # unresolvable references surface here
            problems.append(f"components/schemas/{name}: {exc!r}")
    return problems


def check_operation_ids(spec: dict) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for method, path, operation, _ in iter_operations(spec):
        op_id = operation.get("operationId")
        where = f"{method} {path}"
        if not op_id:
            problems.append(f"{where}: missing operationId")
            continue
        if op_id in seen:
            problems.append(f"{where}: duplicate operationId {op_id!r} (also {seen[op_id]})")
        seen[op_id] = where
    return problems


def _media_type_locations(spec: dict) -> Iterator[tuple[str, str, dict]]:
    """Yield (label, schema_pointer, media_type_object) for every request/response media type."""
    for method, path, operation, _ in iter_operations(spec):
        op_id = operation.get("operationId", f"{method} {path}")
        body = operation.get("requestBody")
        if isinstance(body, dict):
            body_pointer = f"/paths/{escape_pointer_token(path)}/{method.lower()}/requestBody"
            for media, obj in (body.get("content") or {}).items():
                yield (f"{op_id} request {media}", f"{body_pointer}/content/{escape_pointer_token(media)}", obj)
        for status, response in (operation.get("responses") or {}).items():
            if isinstance(response, dict) and "$ref" in response:
                ref = response["$ref"]
                pointer = ref[1:]
                response = resolve_pointer(spec, ref)
                label = f"{op_id} {status} via {ref.rsplit('/', 1)[-1]}"
            else:
                pointer = f"/paths/{escape_pointer_token(path)}/{method.lower()}/responses/{status}"
                label = f"{op_id} {status}"
            for media, obj in (response.get("content") or {}).items():
                yield (label, f"{pointer}/content/{escape_pointer_token(media)}", obj)


def check_examples(contract: Contract, guide_text: str) -> list[str]:
    spec = contract.spec
    problems: list[str] = []
    referenced_examples: set[str] = set()
    seen_media_types: set[str] = set()

    for label, pointer, media_obj in _media_type_locations(spec):
        if pointer in seen_media_types:
            continue
        seen_media_types.add(pointer)
        schema = media_obj.get("schema")
        if not isinstance(schema, dict) or set(schema) != {"$ref"}:
            problems.append(f"{label}: media type schema must be a bare $ref to a component schema")
            continue
        schema_pointer = schema["$ref"][1:]
        examples = media_obj.get("examples") or {}
        if "example" in media_obj:
            problems.append(f"{label}: use 'examples' (map) instead of the singular 'example'")
        if not examples:
            problems.append(f"{label}: no examples declared")
        for name, example in examples.items():
            if isinstance(example, dict) and "$ref" in example:
                referenced_examples.add(example["$ref"].rsplit("/", 1)[-1])
            example = deref(spec, example)
            if "value" not in example:
                problems.append(f"{label} example {name!r}: missing 'value'")
                continue
            for err in contract.errors_for(schema_pointer, example["value"]):
                problems.append(f"{label} example {name!r}: {err}")

    # Schema-level `examples` arrays anywhere under components/schemas.
    def walk_schema_examples(node: Any, pointer: str) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("examples"), list) and pointer.startswith("/components/schemas/"):
                for index, value in enumerate(node["examples"]):
                    for err in contract.errors_for(pointer, value):
                        problems.append(f"{pointer} examples[{index}]: {err}")
            for key, value in node.items():
                if key in ("examples", "enum", "const", "default", "required"):
                    continue
                walk_schema_examples(value, f"{pointer}/{escape_pointer_token(str(key))}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk_schema_examples(value, f"{pointer}/{index}")

    walk_schema_examples(spec.get("components", {}).get("schemas", {}), "/components/schemas")

    # Every component example must be used somewhere.
    for name in spec.get("components", {}).get("examples", {}):
        if name not in referenced_examples:
            problems.append(f"components/examples/{name}: never referenced")

    # Design guide code blocks tagged with <!-- schema: Name -->.
    blocks = GUIDE_BLOCK.findall(guide_text)
    if not blocks:
        problems.append("design guide contains no '<!-- schema: X -->' JSON blocks")
    schemas = spec.get("components", {}).get("schemas", {})
    for index, (schema_name, raw_json) in enumerate(blocks, start=1):
        label = f"design guide block {index} ({schema_name})"
        if schema_name not in schemas:
            problems.append(f"{label}: unknown schema")
            continue
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            problems.append(f"{label}: invalid JSON ({exc})")
            continue
        for err in contract.errors_for(f"/components/schemas/{schema_name}", payload):
            problems.append(f"{label}: {err}")
    return problems


def check_authentication(spec: dict) -> list[str]:
    problems: list[str] = []
    schemes = spec.get("components", {}).get("securitySchemes", {})
    bearer = schemes.get("bearerAuth")
    if not bearer or bearer.get("type") != "http" or bearer.get("scheme") != "bearer":
        problems.append("components/securitySchemes/bearerAuth must be an http bearer scheme")
    if spec.get("security") != [{"bearerAuth": []}]:
        problems.append("top-level security must be exactly [{bearerAuth: []}]")
    for method, path, operation, _ in iter_operations(spec):
        where = f"{method} {path}"
        if "security" in operation:
            problems.append(f"{where}: operation-level security override is not allowed")
        response = (operation.get("responses") or {}).get("401")
        if response is None:
            problems.append(f"{where}: missing 401 response")
            continue
        response = deref(spec, response)
        if "www-authenticate" not in header_names(response.get("headers")):
            problems.append(f"{where}: 401 response lacks WWW-Authenticate header")
    return problems


def check_conditional_mutations(spec: dict) -> list[str]:
    problems: list[str] = []
    for method, path, operation, path_item in iter_operations(spec):
        op_id = operation.get("operationId", f"{method} {path}")
        if_match = [
            p
            for p in operation_parameters(spec, operation, path_item)
            if p.get("in") == "header" and str(p.get("name", "")).lower() == "if-match"
        ]
        responses = operation.get("responses") or {}
        if op_id in CONDITIONAL_OPERATIONS:
            if not if_match:
                problems.append(f"{op_id}: conditional operation lacks an If-Match header parameter")
            elif not if_match[0].get("required"):
                problems.append(f"{op_id}: If-Match must be required")
            for status in ("412", "428", "400"):
                if status not in responses:
                    problems.append(f"{op_id}: conditional operation lacks a {status} response")
        else:
            if if_match:
                problems.append(f"{op_id}: only conditional operations may declare If-Match")
            if "428" in responses:
                problems.append(f"{op_id}: 428 is reserved for conditional operations")
            if "412" in responses and op_id != "createEditRequest":
                problems.append(f"{op_id}: 412 is only expected on conditional operations and createEditRequest")
    if "412" not in ((next((op for m, p, op, _ in iter_operations(spec) if op.get("operationId") == "createEditRequest"), {}) or {}).get("responses") or {}):
        problems.append("createEditRequest: must declare 412 for a stale baseNoteETag")
    return problems


def check_inventory(spec: dict) -> list[str]:
    problems: list[str] = []
    actual: dict[tuple[str, str], str] = {}
    for method, path, operation, _ in iter_operations(spec):
        actual[(method, path)] = operation.get("operationId", "")
    for key, expected_id in EXPECTED_OPERATIONS.items():
        if key not in actual:
            problems.append(f"missing operation {key[0]} {key[1]} ({expected_id})")
        elif actual[key] != expected_id:
            problems.append(f"{key[0]} {key[1]}: operationId {actual[key]!r}, expected {expected_id!r}")
    for key, op_id in actual.items():
        if key not in EXPECTED_OPERATIONS:
            problems.append(f"unexpected operation {key[0]} {key[1]} ({op_id})")
    if not problems and len(actual) != len(EXPECTED_OPERATIONS):
        problems.append(f"operation count {len(actual)} != {len(EXPECTED_OPERATIONS)}")
    return problems


def check_headers_and_media_types(spec: dict) -> list[str]:
    problems: list[str] = []
    for method, path, operation, _ in iter_operations(spec):
        op_id = operation.get("operationId", f"{method} {path}")
        body = operation.get("requestBody")
        if isinstance(body, dict):
            media = set((body.get("content") or {}).keys())
            if media != {SUCCESS_MEDIA_TYPE}:
                problems.append(f"{op_id}: request body media types {sorted(media)} != ['{SUCCESS_MEDIA_TYPE}']")
            if "415" not in (operation.get("responses") or {}):
                problems.append(f"{op_id}: operations with a request body must declare 415")
        elif "415" in (operation.get("responses") or {}):
            problems.append(f"{op_id}: 415 declared without a request body")
        for status, response in (operation.get("responses") or {}).items():
            response = deref(spec, response)
            names = header_names(response.get("headers"))
            where = f"{op_id} {status}"
            if "cache-control" not in names:
                problems.append(f"{where}: missing Cache-Control header")
            if status == "201":
                if "location" not in names:
                    problems.append(f"{where}: 201 lacks Location header")
                if op_id not in CREATE_OPERATIONS:
                    problems.append(f"{where}: 201 on a non-create operation")
            elif op_id in CREATE_OPERATIONS and status.startswith("2"):
                problems.append(f"{where}: create operations succeed with 201, not {status}")
            expects_etag = (op_id, status) in ETAG_RESPONSES
            if expects_etag and "etag" not in names:
                problems.append(f"{where}: missing ETag header")
            if not expects_etag and "etag" in names:
                problems.append(f"{where}: unexpected ETag header")
            content = response.get("content") or {}
            code = int(status)
            if code == 204:
                if content:
                    problems.append(f"{where}: 204 must not have a body")
            elif code < 400:
                if set(content) != {SUCCESS_MEDIA_TYPE}:
                    problems.append(f"{where}: success media types {sorted(content)} != ['{SUCCESS_MEDIA_TYPE}']")
            else:
                if set(content) != {PROBLEM_MEDIA_TYPE}:
                    problems.append(f"{where}: error media types {sorted(content)} != ['{PROBLEM_MEDIA_TYPE}']")
                schema = (content.get(PROBLEM_MEDIA_TYPE) or {}).get("schema")
                if schema != {"$ref": "#/components/schemas/Problem"}:
                    problems.append(f"{where}: error body must be the Problem schema")
    # Header components must be required and Cache-Control pinned to no-store.
    headers = spec.get("components", {}).get("headers", {})
    cache = headers.get("CacheControl", {})
    if cache.get("schema", {}).get("const") != "no-store":
        problems.append("components/headers/CacheControl must pin const no-store")
    for name, header in headers.items():
        if not header.get("required"):
            problems.append(f"components/headers/{name}: must be required")
    return problems


def check_negative_cases(contract: Contract, cases: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(cases, dict) or not cases.get("must_fail") or not cases.get("must_pass"):
        return ["tests/negative_cases.yaml needs non-empty 'must_fail' and 'must_pass' lists"]
    schemas = contract.spec.get("components", {}).get("schemas", {})
    for bucket, should_pass in (("must_fail", False), ("must_pass", True)):
        for index, case in enumerate(cases[bucket], start=1):
            schema_name = case.get("schema")
            label = f"{bucket}[{index}] {schema_name}: {case.get('description', '(no description)')}"
            if schema_name not in schemas:
                problems.append(f"{label}: unknown schema")
                continue
            if "payload" not in case:
                problems.append(f"{label}: missing 'payload'")
                continue
            errors = contract.errors_for(f"/components/schemas/{schema_name}", case["payload"])
            if should_pass and errors:
                problems.append(f"{label}: expected valid but got {errors[0]}")
            if not should_pass and not errors:
                problems.append(f"{label}: expected rejection but payload validated")
    return problems


def check_error_codes(spec: dict) -> list[str]:
    """Every ErrorCode has a Problem example; every Problem example uses a known code and the matching
    ``type`` URI; a 409 component carrying several codes names each of them in its description."""
    problems: list[str] = []
    components = spec.get("components", {})
    codes = set((components.get("schemas", {}).get("ErrorCode") or {}).get("enum") or [])
    if not codes:
        return ["components/schemas/ErrorCode has no enum"]
    covered: set[str] = set()
    for name, example in components.get("examples", {}).items():
        value = example.get("value") if isinstance(example, dict) else None
        if not isinstance(value, dict) or "code" not in value:
            continue
        code = value["code"]
        covered.add(code)
        if code not in codes:
            problems.append(f"components/examples/{name}: code {code!r} is not in ErrorCode")
        if value.get("type") != f"{PROBLEM_TYPE_PREFIX}{code}":
            problems.append(f"components/examples/{name}: type must be {PROBLEM_TYPE_PREFIX}{code}")
    for code in sorted(codes - covered):
        problems.append(f"ErrorCode {code!r} has no Problem example")
    used_409: set[str] = set()
    for _, _, operation, _ in iter_operations(spec):
        response = (operation.get("responses") or {}).get("409")
        if isinstance(response, dict) and "$ref" in response:
            used_409.add(response["$ref"].rsplit("/", 1)[-1])
    for name in sorted(used_409):
        response = components.get("responses", {}).get(name) or {}
        media = (response.get("content") or {}).get(PROBLEM_MEDIA_TYPE) or {}
        carried: set[str] = set()
        for example in (media.get("examples") or {}).values():
            value = deref(spec, example).get("value")
            if isinstance(value, dict) and "code" in value:
                carried.add(value["code"])
        if len(carried) < 2:
            continue
        for code in sorted(carried):
            if f"`{code}`" not in response.get("description", ""):
                problems.append(f"components/responses/{name}: description does not name `{code}`")
    return problems


def _walk_objects(node: Any, path: str) -> Iterator[tuple[str, dict]]:
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk_objects(value, f"{path}/{escape_pointer_token(str(key))}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_objects(value, f"{path}/{index}")


def check_ownership_invariants(spec: dict) -> list[str]:
    """Cross-field rules JSON Schema cannot express, checked on every example value: note owners start
    with the author and are unique, the policy never exceeds the owner count, approvals exclude the
    proposer and stay unique and sorted, and a merged request satisfied its requirement counting the
    merger (design guide, sections 2 and 3). Shapes are recognised by key presence, which is safe because
    check 5 already validates every example against its closed schema."""
    problems: list[str] = []
    for name, example in spec.get("components", {}).get("examples", {}).items():
        if not isinstance(example, dict):
            continue
        for path, obj in _walk_objects(example.get("value"), f"components/examples/{name}"):
            if "authorId" in obj and isinstance(obj.get("ownerIds"), list):
                owners = obj["ownerIds"]
                if not owners or owners[0] != obj["authorId"]:
                    problems.append(f"{path}: ownerIds must start with authorId")
                if len(set(owners)) != len(owners):
                    problems.append(f"{path}: ownerIds repeats an owner")
                required = (obj.get("reviewPolicy") or {}).get("requiredApprovals")
                if isinstance(required, int) and required > len(owners):
                    problems.append(f"{path}: reviewPolicy.requiredApprovals exceeds the owner count")
            if "proposerId" in obj and isinstance(obj.get("approvals"), list):
                approvals = [a for a in obj["approvals"] if isinstance(a, dict)]
                approvers = [a.get("userId") for a in approvals]
                if obj["proposerId"] in approvers:
                    problems.append(f"{path}: the proposer cannot approve their own request")
                if len(set(approvers)) != len(approvers):
                    problems.append(f"{path}: duplicate approver")
                order = [(a.get("approvedAt"), a.get("userId")) for a in approvals]
                if order != sorted(order):
                    problems.append(f"{path}: approvals must be sorted approvedAt ASC, userId ASC")
                record = obj.get("mergeRecord")
                if isinstance(record, dict):
                    merger = record.get("mergedBy")
                    counted = len(approvals) + int(merger not in approvers and merger != obj["proposerId"])
                    required = obj.get("requiredApprovals", 0)
                    if counted < required:
                        problems.append(f"{path}: merged with {counted} counted approvals, {required} required")
    return problems


# ---------------------------------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------------------------------


def main() -> int:
    for path in (SPEC_PATH, GUIDE_PATH, CASES_PATH):
        if not path.exists():
            sys.stderr.write(f"missing file: {path.relative_to(ROOT)}\n")
            return 1

    spec = load_yaml(SPEC_PATH)
    guide_text = GUIDE_PATH.read_text(encoding="utf-8")
    cases = load_yaml(CASES_PATH)
    contract = Contract(spec)

    checks: list[tuple[str, Callable[[], list[str]]]] = [
        ("OpenAPI 3.1.2 document validates", lambda: check_document(spec)),
        ("All $ref values are local and resolve", lambda: check_references(spec)),
        ("Every component schema compiles", lambda: check_component_schemas(contract)),
        ("Operation IDs present and unique", lambda: check_operation_ids(spec)),
        ("Examples validate (openapi.yaml and design guide)", lambda: check_examples(contract, guide_text)),
        ("Bearer authentication covers every operation", lambda: check_authentication(spec)),
        ("Conditional mutations declare If-Match, 412, 428", lambda: check_conditional_mutations(spec)),
        ("Endpoint inventory matches the design guide", lambda: check_inventory(spec)),
        ("Required headers and media types", lambda: check_headers_and_media_types(spec)),
        ("Negative and positive schema fixtures", lambda: check_negative_cases(contract, cases)),
        ("Every ErrorCode has a Problem example and vice versa", lambda: check_error_codes(spec)),
        ("Ownership and approval invariants in examples", lambda: check_ownership_invariants(spec)),
    ]

    failed = 0
    for number, (title, fn) in enumerate(checks, start=1):
        try:
            problems = fn()
        except Exception as exc:  # a crashing check is a failing check
            problems = [f"check crashed: {exc!r}"]
        status = "PASS" if not problems else "FAIL"
        if problems:
            failed += 1
        print(f"[{status}] {number:>2}. {title}")
        for problem in problems[:40]:
            print(f"         - {problem}")
        if len(problems) > 40:
            print(f"         - ... {len(problems) - 40} more")

    total = len(checks)
    print(f"\n{total - failed}/{total} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
