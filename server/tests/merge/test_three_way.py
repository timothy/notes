"""The three-way merge: the contract's preview examples byte for byte, and the guide's conflict rules."""

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from notes_api.contract import Contract
from notes_api.merge.three_way import Conflict, Content, LineRange, merge_body, merge_title, preview

EXAMPLES = {
    name: example["value"] for name, example in Contract.load().document["components"]["examples"].items()
}


def content(example: dict[str, Any]) -> Content:
    return Content(title=example["title"], body=example["body"])


def preview_fields(result_dict: dict[str, Any]) -> dict[str, Any]:
    keys = ("canMerge", "usedFinalContent", "candidate", "conflicts", "proposalDiff", "mergeDiff")
    return {key: result_dict[key] for key in keys}


# -- the contract's own examples -----------------------------------------------------------------


def test_preview_clean_matches_the_contract_example() -> None:
    request = EXAMPLES["EditRequestOpen"]
    result = preview(
        base=content(request["baseContent"]),
        current=content(EXAMPLES["NoteV1Owner"]),
        proposed=content(request["proposedContent"]),
    )
    assert result.to_dict() == preview_fields(EXAMPLES["PreviewClean"])


def test_preview_with_final_content_matches_the_contract_example() -> None:
    request = EXAMPLES["EditRequestOpen"]
    expected = EXAMPLES["PreviewWithFinalContent"]
    result = preview(
        base=content(request["baseContent"]),
        current=content(EXAMPLES["NoteV1Owner"]),
        proposed=content(request["proposedContent"]),
        final=content(expected["candidate"]),
    )
    assert result.to_dict() == preview_fields(expected)


def test_preview_conflict_matches_the_contract_example() -> None:
    request = EXAMPLES["EditRequestConflicting"]
    result = preview(
        base=content(request["baseContent"]),
        current=content(EXAMPLES["NoteV3Owner"]),
        proposed=content(request["proposedContent"]),
    )
    assert result.to_dict() == preview_fields(EXAMPLES["PreviewConflict"])


def test_preview_conflict_resolved_by_final_content_matches_the_contract_example() -> None:
    request = EXAMPLES["EditRequestConflicting"]
    expected = EXAMPLES["PreviewConflictResolved"]
    result = preview(
        base=content(request["baseContent"]),
        current=content(EXAMPLES["NoteV3Owner"]),
        proposed=content(request["proposedContent"]),
        final=content(expected["candidate"]),
    )
    assert result.to_dict() == preview_fields(expected)


# -- title rule -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "proposed", "expected"),
    [
        ("Base", "Base", "Base"),
        ("Base", "Proposed", "Proposed"),
        ("Current", "Base", "Current"),
        ("Same change", "Same change", "Same change"),
    ],
)
def test_title_takes_the_changed_side_or_keeps_agreement(current: str, proposed: str, expected: str) -> None:
    assert merge_title("Base", current, proposed) == (expected, None)


def test_title_changed_differently_on_both_sides_is_a_conflict() -> None:
    merged, conflict = merge_title("Base", "Current", "Proposed")
    assert merged is None
    assert conflict == Conflict(
        field="title", base_range=None, base="Base", current="Current", proposed="Proposed"
    )
    assert conflict.to_dict()["baseRange"] is None


# -- body rules -----------------------------------------------------------------------------------

BASE = "a\nb\nc\nd\ne\n"


def test_changes_in_separate_regions_combine() -> None:
    assert merge_body(BASE, "A\nb\nc\nd\ne\n", "a\nb\nc\nd\nE\n") == ("A\nb\nc\nd\nE\n", [])


def test_identical_changes_appear_once() -> None:
    assert merge_body(BASE, "a\nb\nC\nd\ne\n", "a\nb\nC\nd\ne\n") == ("a\nb\nC\nd\ne\n", [])


def test_the_same_insertion_by_both_sides_appears_once() -> None:
    assert merge_body(BASE, "a\nb\nx\nc\nd\ne\n", "a\nb\nx\nc\nd\ne\n") == ("a\nb\nx\nc\nd\ne\n", [])


def test_competing_insertions_at_the_same_place_conflict_with_an_empty_range() -> None:
    merged, conflicts = merge_body(BASE, "a\nb\nx\nc\nd\ne\n", "a\nb\ny\nc\nd\ne\n")
    assert merged is None
    assert conflicts == [
        Conflict(field="body", base_range=LineRange(start=3, end=3), base="", current="x\n", proposed="y\n")
    ]
    assert conflicts[0].to_dict()["baseRange"] == {"start": 3, "end": 3}


def test_replacing_and_deleting_the_same_line_conflicts() -> None:
    merged, conflicts = merge_body(BASE, "a\nB\nc\nd\ne\n", "a\nc\nd\ne\n")
    assert merged is None
    assert conflicts == [
        Conflict(field="body", base_range=LineRange(start=2, end=3), base="b\n", current="B\n", proposed="")
    ]


def test_an_insertion_touching_a_replaced_line_conflicts_as_one_segment() -> None:
    merged, conflicts = merge_body(BASE, "a\nb\nx\nc\nd\ne\n", "a\nb\nC\nd\ne\n")
    assert merged is None
    assert conflicts == [
        Conflict(
            field="body", base_range=LineRange(start=3, end=4), base="c\n", current="x\nc\n", proposed="C\n"
        )
    ]


def test_a_deletion_on_one_side_applies() -> None:
    assert merge_body(BASE, "a\nc\nd\ne\n", BASE) == ("a\nc\nd\ne\n", [])
    assert merge_body(BASE, BASE, "a\nb\nc\ne\n") == ("a\nb\nc\ne\n", [])


def test_two_conflicts_are_reported_in_base_order() -> None:
    merged, conflicts = merge_body(BASE, "A\nb\nc\nd\nE\n", "1\nb\nc\nd\n5\n")
    assert merged is None
    assert [c.base_range for c in conflicts] == [LineRange(1, 2), LineRange(5, 6)]


def test_empty_bodies() -> None:
    assert merge_body("", "", "a\n") == ("a\n", [])
    assert merge_body("", "a\n", "") == ("a\n", [])
    assert merge_body("a\n", "", "a\n") == ("", [])
    merged, conflicts = merge_body("", "a\n", "b\n")
    assert merged is None
    assert conflicts == [
        Conflict(field="body", base_range=LineRange(1, 1), base="", current="a\n", proposed="b\n")
    ]


def test_line_endings_and_missing_final_newlines_are_preserved() -> None:
    assert merge_body("a\r\nb\r\n", "a\r\nb\r\n", "a\r\nc\r\n") == ("a\r\nc\r\n", [])
    assert merge_body("a\nb", "a\nb", "a\nb\n") == ("a\nb\n", [])
    assert merge_body("a\nb", "a\nb\n", "a\nb") == ("a\nb\n", [])


def test_unicode_content_merges_by_line() -> None:
    base = "Grüße\n日本語\n🙂\n"
    assert merge_body(base, "Grüße!\n日本語\n🙂\n", "Grüße\n日本語\n🙃\n") == ("Grüße!\n日本語\n🙃\n", [])


def test_preview_without_a_proposal_change_yields_the_current_note() -> None:
    base = Content("T", "a\nb\n")
    current = Content("T", "a\nb\nc\n")
    result = preview(base=base, current=current, proposed=base)
    assert result.can_merge and result.candidate == current
    assert result.merge_diff is not None and result.merge_diff.body == ""
    assert result.proposal_diff.body == ""


LINES = st.lists(st.sampled_from(["a\n", "b\n", "c\n", "d\n"]), max_size=8).map("".join)


@settings(max_examples=200)
@given(base=LINES, other=LINES)
def test_merging_with_an_unchanged_side_returns_the_other_side(base: str, other: str) -> None:
    assert merge_body(base, base, other) == (other, [])
    assert merge_body(base, other, base) == (other, [])


@settings(max_examples=200)
@given(base=LINES, other=LINES)
def test_identical_sides_return_themselves(base: str, other: str) -> None:
    assert merge_body(base, other, other) == (other, [])
