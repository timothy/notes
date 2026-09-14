"""Property-based conformance: every implemented operation against ``openapi.yaml``.

Schemathesis generates requests from the contract, valid and deliberately invalid, and checks that each
response is a declared status with the declared headers, media type, and body schema. The include list
grows one slice at a time; an id that matches nothing fails the run loudly. One note, one team, one
membership, one share, one comment, and one open edit request are seeded so that path parameters point at
real resources and the 2xx, 409, and 412 branches are reached as well as the 404s. Every operation runs
as a subtest over that one database in document order; nothing a generated request can do closes or
removes the seeded rows, because the conditional mutations need an ``If-Match`` equal to a stored version
and the generated values never are.

Two checks are excluded on purpose: ``positive_data_acceptance`` rejects the ladder's own 400, 412, 422,
and 428 answers to schema-valid but semantically wrong input, and ``ignored_auth`` triples every 2xx to
re-probe the 401 that ``tests/test_auth.py`` already pins. The coverage phase runs positive-only and does
not probe undeclared methods, which this server answers with the contract's 404 by design.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import schemathesis
import yaml
from fastapi import FastAPI
from hypothesis import HealthCheck, settings
from schemathesis import GenerationMode
from schemathesis.config import CoveragePhaseConfig, GenerationConfig

from notes_api.contract import DEFAULT_CONTRACT_PATH
from tests.contract_client import ContractClient
from tests.support import Persona

OPERATIONS = [
    "getCurrentUser",
    "listUsers",
    "getUser",
    "createTeam",
    "listTeams",
    "getTeam",
    "updateTeam",
    "deleteTeam",
    "listMemberships",
    "addMembership",
    "updateMembership",
    "removeMembership",
    "createNote",
    "listNotes",
    "getNote",
    "updateNote",
    "trashNote",
    "restoreNote",
    "listShares",
    "createShare",
    "getShare",
    "updateShare",
    "deleteShare",
    "listComments",
    "createComment",
    "getComment",
    "updateComment",
    "deleteComment",
    "createEditRequest",
    "listNoteEditRequests",
    "listEditRequests",
    "getEditRequest",
    "reviseEditRequest",
    "withdrawEditRequest",
    "rejectEditRequest",
    "previewEditRequest",
    "mergeEditRequest",
]
EXCLUDED_CHECKS = ["positive_data_acceptance", "ignored_auth"]
MISSING_HEADER_STATUSES = ["400", "401", "403", "406", "415", "422", "428"]

RAW: dict[str, Any] = yaml.safe_load(Path(DEFAULT_CONTRACT_PATH).read_text(encoding="utf-8"))


@pytest.fixture
def conformance_schema(
    app: FastAPI, client: ContractClient, ada: Persona, ben: Persona
) -> schemathesis.BaseSchema:
    created = client.post("/v1/notes", auth=ada, json={"title": "Conformance seed", "tags": ["seed"]})
    note = created.json()
    team = client.post("/v1/teams", auth=ada, json={"name": "Conformance"}).json()
    ben_id = client.get("/v1/me", auth=ben).json()["id"]
    assert (
        client.post(f"/v1/teams/{team['id']}/members", auth=ada, json={"userId": ben_id}).status_code == 201
    )

    share = client.post(
        f"/v1/notes/{note['id']}/shares",
        auth=ada,
        json={"recipient": {"type": "user", "id": ben_id}, "permissions": ["comment", "propose_edit"]},
    ).json()
    comment = client.post(
        f"/v1/notes/{note['id']}/comments", auth=ada, json={"body": "Conformance seed comment"}
    ).json()
    edit_request = client.post(
        f"/v1/notes/{note['id']}/edit-requests",
        auth=ben,
        json={
            "baseNoteETag": created.headers["ETag"],
            "proposedContent": {"title": "Conformance seed", "body": "A proposed body.\n"},
        },
    ).json()

    schema = schemathesis.openapi.from_dict(RAW)
    schema.app = app  # the document's server path "/v1" is kept; the ASGI transport supplies the host
    schema.config.update(
        headers=ada.headers,
        parameters={
            "path.noteId": note["id"],
            "path.teamId": team["id"],
            "path.userId": ben_id,
            "path.shareId": share["id"],
            "path.commentId": comment["id"],
            "path.requestId": edit_request["id"],
        },
    )
    schema.config.generation.update(with_security_parameters=False)
    schema.config.checks.update(excluded_check_names=EXCLUDED_CHECKS)
    schema.config.checks.missing_required_header.expected_statuses = MISSING_HEADER_STATUSES
    schema.config.phases.coverage = CoveragePhaseConfig(
        unexpected_methods=set(),
        generation=GenerationConfig(modes=[GenerationMode.POSITIVE], with_security_parameters=False),
    )
    return schema


# The include filter must sit on the lazy schema: one applied inside the fixture is discarded.
conformance = schemathesis.pytest.from_fixture("conformance_schema").include(operation_id=OPERATIONS)


@conformance.parametrize()
@settings(
    max_examples=25,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_every_implemented_operation_conforms(case: schemathesis.Case[Any]) -> None:
    case.call_and_validate()
