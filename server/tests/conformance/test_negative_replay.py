"""Every request-schema fixture in ``tests/negative_cases.yaml`` replayed through the endpoint that uses it.

``tests/test_contract_loader.py`` proves each fixture's verdict through ``Contract.validate_body``. This
module proves the endpoints: for every operation whose request body is a component schema (read from
``openapi.yaml``, so a new body-taking operation joins by itself), each must-fail fixture of that schema is
sent to the operation and must be the contract's ``422 validation_failed`` with body-located errors, and each
must-pass fixture must land with a 2xx, so no endpoint validates against the wrong schema or refuses a valid
body.

Each case runs against its own freshly seeded database: Ada's note with Dan as co-owner, a proposal-only share
to Ben, one open request by Ben, a note comment and a request comment by Ada, a team with Ben as member, and
Cara provisioned but holding nothing. The fixtures' placeholder identifiers are replaced by the live ones, and
the seed is shaped so that every must-pass payload is semantically valid as well: two owners keep
``requiredApprovals: 2`` within the owner count while ``self_merge`` lets the merge land without approvals;
the note's body differs from both proposed bodies, so no proposal equals its base; Cara is a valid new owner,
share recipient, and team member; and a second submission by the same proposer is allowed. No fixture needs an
exception today; a payload a correct server must refuse for a semantic reason would be listed here with its
expected status rather than the seed being bent around it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pytest
import yaml

from notes_api.contract import DEFAULT_CONTRACT_PATH
from tests.contract_client import ContractClient
from tests.support import Persona
from tests.test_edit_requests import proposal
from tests.test_notes import CREATE_NOTE_REQUEST, me

DOCUMENT: dict[str, Any] = yaml.safe_load(DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8"))
FIXTURES: dict[str, list[dict[str, Any]]] = yaml.safe_load(
    (DEFAULT_CONTRACT_PATH.parent / "tests" / "negative_cases.yaml").read_text(encoding="utf-8")
)
VERDICTS = ("must_fail", "must_pass")
METHODS = frozenset({"get", "put", "post", "patch", "delete"})
IF_MATCH = "#/components/parameters/IfMatch"

# Fixture schemas that describe responses, not request bodies. The loader test validates their verdicts and
# the ContractClient checks every response body in the suite against them, so the replay skips them.
RESPONSE_SCHEMAS = frozenset(
    {
        "Approval",
        "Conflict",
        "EditRequest",
        "FieldError",
        "NextCursor",
        "Note",
        "NoteSummary",
        "PreviewResult",
        "Problem",
        "RequestComment",
        "Share",
        "Tag",
        "Timestamp",
        "User",
        "Uuid",
    }
)
# Body-taking operations whose schema (UpdateTeam, UpdateComment) has no fixtures. The fixture file belongs to
# the contract, so the gap is pinned here rather than filled by the server.
WITHOUT_FIXTURES = frozenset({"updateTeam", "updateComment", "updateEditRequestComment"})

# The fixture file's placeholder identifiers and the seeded value each one becomes.
PLACEHOLDERS = {
    "11111111-1111-4111-8111-111111111111": "ada",
    "22222222-2222-4222-8222-222222222222": "cara",
    "99999999-9999-4999-8999-999999999999": "cara",
    "33333333-3333-4333-8333-333333333333": "team",
    "44444444-4444-4444-8444-444444444444": "note",
}
# Quoted only: the unquoted ``note-v1`` and the weak ``W/"note-v1"`` are must-fail pattern cases.
NOTE_ETAG_PLACEHOLDER = re.compile(r'^"note-v\d+"$')
PROPOSER_OPERATIONS = frozenset({"createEditRequest", "reviseEditRequest"})


@dataclass(frozen=True)
class Operation:
    id: str
    method: str
    template: str
    schema: str
    conditional: bool  # declares the If-Match parameter


def body_operations(document: dict[str, Any]) -> dict[str, Operation]:
    """Every operation with a JSON request body, keyed by operationId, in document order."""
    found: dict[str, Operation] = {}
    for template, item in document["paths"].items():
        for method, operation in item.items():
            if method not in METHODS or "requestBody" not in operation:
                continue
            ref: str = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            conditional = any(p.get("$ref") == IF_MATCH for p in operation.get("parameters", []))
            found[operation["operationId"]] = Operation(
                operation["operationId"], method.upper(), template, ref.rsplit("/", 1)[1], conditional
            )
    return found


OPERATIONS = body_operations(DOCUMENT)


def fixture_schemas() -> set[str]:
    return {case["schema"] for verdict in VERDICTS for case in FIXTURES[verdict]}


def replay_cases() -> list[Any]:
    params: list[Any] = []
    for operation in OPERATIONS.values():
        for verdict in VERDICTS:
            for case in FIXTURES[verdict]:
                if case["schema"] != operation.schema:
                    continue
                slug = re.sub(r"[^a-z0-9]+", "-", case["description"].lower()).strip("-")
                params.append(
                    pytest.param(operation.id, verdict, case, id=f"{operation.id}-{verdict}-{slug}")
                )
    return params


@dataclass(frozen=True)
class Seed:
    ids: dict[str, str]  # ada, ben, cara, dan, note, request, share, team, note_comment, request_comment
    etags: dict[str, str]  # note, request, note_comment, request_comment

    def comment_kind(self, operation: Operation) -> str:
        return "request_comment" if operation.template.startswith("/edit-requests") else "note_comment"

    def path(self, operation: Operation) -> str:
        return "/v1" + operation.template.format(
            noteId=self.ids["note"],
            requestId=self.ids["request"],
            teamId=self.ids["team"],
            shareId=self.ids["share"],
            userId=self.ids["ben"],
            commentId=self.ids[self.comment_kind(operation)],
        )

    def etag(self, operation: Operation) -> str | None:
        """The precondition the operation needs: the innermost resource's current ETag."""
        if not operation.conditional:
            return None
        if operation.template.endswith("{commentId}"):
            return self.etags[self.comment_kind(operation)]
        if "{requestId}" in operation.template:
            return self.etags["request"]
        return self.etags["note"]

    def substitute(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.substitute(item) for item in value]
        if isinstance(value, str):
            if value in PLACEHOLDERS:
                return self.ids[PLACEHOLDERS[value]]
            if NOTE_ETAG_PLACEHOLDER.match(value):
                return self.etags["note"]
        return value


def seed(client: ContractClient, ada: Persona, ben: Persona, cara: Persona, dan: Persona) -> Seed:
    ids = {"ada": me(client, ada), "ben": me(client, ben), "cara": me(client, cara), "dan": me(client, dan)}
    created = client.post("/v1/notes", auth=ada, json=CREATE_NOTE_REQUEST)
    note_id = created.json()["id"]
    protected = client.post(
        f"/v1/notes/{note_id}/owners", auth=ada, if_match=created.headers["ETag"], json={"userId": ids["dan"]}
    )
    share = client.post(
        f"/v1/notes/{note_id}/shares",
        auth=ada,
        json={"recipient": {"type": "user", "id": ids["ben"]}, "permissions": ["propose_edit"]},
    )
    submitted = client.post(
        f"/v1/notes/{note_id}/edit-requests", auth=ben, json=proposal(protected.headers["ETag"])
    )
    request_id = submitted.json()["id"]
    note_comment = client.post(f"/v1/notes/{note_id}/comments", auth=ada, json={"body": "A note comment."})
    request_comment = client.post(
        f"/v1/edit-requests/{request_id}/comments", auth=ada, json={"body": "A request comment."}
    )
    team = client.post("/v1/teams", auth=ada, json={"name": "Replay"})
    member = client.post(f"/v1/teams/{team.json()['id']}/members", auth=ada, json={"userId": ids["ben"]})
    for response in (created, protected, share, submitted, note_comment, request_comment, team, member):
        assert response.status_code in (200, 201), response.text
    ids.update(
        note=note_id,
        request=request_id,
        share=share.json()["id"],
        team=team.json()["id"],
        note_comment=note_comment.json()["id"],
        request_comment=request_comment.json()["id"],
    )
    etags = {
        "note": protected.headers["ETag"],
        "request": submitted.headers["ETag"],
        "note_comment": note_comment.headers["ETag"],
        "request_comment": request_comment.headers["ETag"],
    }
    return Seed(ids, etags)


@pytest.mark.parametrize(("operation_id", "verdict", "case"), replay_cases())
def test_fixture_replayed_through_its_operation(
    client: ContractClient,
    ada: Persona,
    ben: Persona,
    cara: Persona,
    dan: Persona,
    operation_id: str,
    verdict: str,
    case: dict[str, Any],
) -> None:
    seeded = seed(client, ada, ben, cara, dan)
    operation = OPERATIONS[operation_id]
    sender = ben if operation_id in PROPOSER_OPERATIONS else ada
    response = client.request(
        operation.method,
        seeded.path(operation),
        auth=sender,
        json=seeded.substitute(case["payload"]),
        if_match=seeded.etag(operation),
    )
    if verdict == "must_fail":
        assert response.status_code == 422, response.text
        problem = response.json()
        assert problem["code"] == "validation_failed", problem
        assert problem["errors"], problem
        assert {error["location"] for error in problem["errors"]} == {"body"}, problem
    else:
        assert response.status_code < 300, response.text


def test_every_fixture_schema_is_a_request_body_or_a_response_shape() -> None:
    assert all(FIXTURES[verdict] for verdict in VERDICTS)
    schemas = fixture_schemas()
    assert schemas <= set(DOCUMENT["components"]["schemas"])
    request_schemas = {operation.schema for operation in OPERATIONS.values()}
    assert schemas - request_schemas == RESPONSE_SCHEMAS


def test_every_body_operation_is_replayed_except_the_known_three() -> None:
    replayed = {operation_id for operation_id, _, _ in (param.values for param in replay_cases())}
    assert set(OPERATIONS) - replayed == WITHOUT_FIXTURES
    assert {OPERATIONS[operation_id].schema for operation_id in WITHOUT_FIXTURES} == {
        "UpdateTeam",
        "UpdateComment",
    }
