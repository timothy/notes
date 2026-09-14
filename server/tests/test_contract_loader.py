"""The spec's own JSON Schemas validate request bodies and produce contract-shaped field errors."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from notes_api.contract import Contract, FieldError

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES: dict[str, list[dict[str, Any]]] = yaml.safe_load(
    (REPO_ROOT / "tests" / "negative_cases.yaml").read_text()
)


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [f"{case['schema']}: {case['description']}" for case in cases]


@pytest.fixture(scope="module")
def contract() -> Contract:
    return Contract.load(REPO_ROOT / "openapi.yaml")


@pytest.mark.parametrize("case", CASES["must_fail"], ids=_ids(CASES["must_fail"]))
def test_must_fail_payloads_are_rejected(contract: Contract, case: dict[str, Any]) -> None:
    errors = contract.validate_body(case["schema"], case["payload"])
    assert errors, "the payload must be rejected"
    assert all(error.location == "body" for error in errors)


@pytest.mark.parametrize("case", CASES["must_pass"], ids=_ids(CASES["must_pass"]))
def test_must_pass_payloads_are_accepted(contract: Contract, case: dict[str, Any]) -> None:
    assert contract.validate_body(case["schema"], case["payload"]) == []


def test_unknown_field_and_blank_title_match_the_problem_example(contract: Contract) -> None:
    errors = contract.validate_body(
        "CreateNote", {"title": "   ", "authorId": "11111111-1111-4111-8111-111111111111"}
    )
    assert [(error.location, error.pointer) for error in errors] == [
        ("body", "/authorId"),
        ("body", "/title"),
    ]
    assert next(error.detail for error in errors if error.pointer == "/authorId") == "unknown field"


def test_missing_required_property_points_at_the_property(contract: Contract) -> None:
    errors = contract.validate_body("CreateNote", {"body": "x"})
    assert [(error.pointer, error.detail) for error in errors] == [("/title", "is required")]


def test_nested_pointer_is_an_rfc6901_json_pointer(contract: Contract) -> None:
    payload = {"recipient": {"type": "user", "id": "not-a-uuid"}, "permissions": ["read"]}
    errors = contract.validate_body("CreateShare", payload)
    assert [error.pointer for error in errors] == ["/recipient/id"]


def test_conditional_rule_in_review_policy_is_enforced(contract: Contract) -> None:
    errors = contract.validate_body("ReviewPolicy", {"mode": "self_merge", "requiredApprovals": 2})
    assert [error.pointer for error in errors] == ["/requiredApprovals"]


def test_empty_patch_is_reported_at_the_body_root(contract: Contract) -> None:
    errors = contract.validate_body("UpdateNote", {})
    assert [error.pointer for error in errors] == [""]


def test_unknown_schema_name_is_a_programming_error(contract: Contract) -> None:
    with pytest.raises(KeyError):
        contract.validate_body("NoSuchSchema", {})


def test_nul_characters_are_reported_at_their_pointers() -> None:
    from notes_api.contract import nul_character_errors

    instance = {
        "title": "a\x00",
        "tags": ["fine", "b\x00c"],
        "nested": {"deep": ["\x00"]},
        "n": 1,
        "ok": "text",
    }
    assert [(error.location, error.pointer, error.detail) for error in nul_character_errors(instance)] == [
        ("body", "/title", "must not contain NUL characters"),
        ("body", "/tags/1", "must not contain NUL characters"),
        ("body", "/nested/deep/0", "must not contain NUL characters"),
    ]
    assert nul_character_errors({"title": "clean"}) == []
    assert nul_character_errors("\x00") == [FieldError("body", "", "must not contain NUL characters")]
