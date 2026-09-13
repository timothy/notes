"""Line splitting that round-trips any text, and unified diffs that match the contract byte for byte."""

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from notes_api.contract import Contract
from notes_api.merge.lines import join_lines, split_lines
from notes_api.merge.unified import field_diff, title_diff, unified_diff

CONTRACT = Contract.load()
EDIT_REQUEST_EXAMPLES = {
    name: example["value"]
    for name, example in CONTRACT.document["components"]["examples"].items()
    if isinstance(example.get("value"), dict)
    and {"baseContent", "proposedContent", "proposalDiff"} <= example["value"].keys()
}


def test_split_keeps_line_endings_and_only_breaks_on_newline() -> None:
    assert split_lines("") == []
    assert split_lines("a") == ["a"]
    assert split_lines("a\n") == ["a\n"]
    assert split_lines("a\r\nb\r\n") == ["a\r\n", "b\r\n"]
    exotic = "a\x0bb" + chr(0x2028) + "c\n"  # vertical tab and LINE SEPARATOR are content
    assert split_lines(exotic) == [exotic]
    assert split_lines("\n\n") == ["\n", "\n"]


@settings(max_examples=200)
@given(st.text())
def test_split_and_join_round_trip_any_text(text: str) -> None:
    lines = split_lines(text)
    assert join_lines(lines) == text
    assert all("\n" not in line[:-1] for line in lines)


def test_equal_fields_have_an_empty_diff() -> None:
    assert field_diff("same\n", "same\n", "base/body", "proposed/body") == ""
    assert title_diff("Same", "Same", "base/title", "proposed/title") == ""


def test_an_appended_line_produces_a_git_style_hunk() -> None:
    expected = "--- base/body\n+++ proposed/body\n@@ -1,2 +1,3 @@\n a\n b\n+c\n"
    assert field_diff("a\nb\n", "a\nb\nc\n", "base/body", "proposed/body") == expected


def test_a_missing_final_newline_is_marked_on_both_sides() -> None:
    expected = "--- base/body\n+++ proposed/body\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+a\n"
    assert field_diff("a", "a\n", "base/body", "proposed/body") == expected
    expected_reverse = "--- base/body\n+++ proposed/body\n@@ -1 +1 @@\n-a\n+a\n\\ No newline at end of file\n"
    assert field_diff("a\n", "a", "base/body", "proposed/body") == expected_reverse


def test_crlf_lines_are_preserved_inside_the_diff() -> None:
    expected = "--- base/body\n+++ proposed/body\n@@ -1,2 +1,2 @@\n a\r\n-b\r\n+c\r\n"
    assert field_diff("a\r\nb\r\n", "a\r\nc\r\n", "base/body", "proposed/body") == expected


def test_an_empty_body_growing_uses_the_zero_range() -> None:
    expected = "--- base/body\n+++ proposed/body\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    assert field_diff("", "a\nb\n", "base/body", "proposed/body") == expected
    expected_shrink = "--- base/body\n+++ proposed/body\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    assert field_diff("a\nb\n", "", "base/body", "proposed/body") == expected_shrink


def test_three_context_lines_and_separate_hunks() -> None:
    lines = [f"{i}\n" for i in range(1, 21)]
    changed = list(lines)
    changed[2], changed[17] = "three\n", "eighteen\n"
    diff = field_diff("".join(lines), "".join(changed), "base/body", "proposed/body")
    assert diff.count("@@") == 4, "two changes 15 lines apart are two hunks"
    assert "@@ -1,6 +1,6 @@\n 1\n 2\n-3\n+three\n 4\n 5\n 6\n" in diff
    assert "@@ -15,6 +15,6 @@\n 15\n 16\n 17\n-18\n+eighteen\n 19\n 20\n" in diff


def test_a_title_is_diffed_as_one_line_with_a_virtual_newline() -> None:
    expected = "--- base/title\n+++ proposed/title\n@@ -1 +1 @@\n-Release checklist\n+Release runbook\n"
    assert title_diff("Release checklist", "Release runbook", "base/title", "proposed/title") == expected


def test_unified_diff_accepts_line_lists_directly() -> None:
    diff = unified_diff(["a\n"], ["b\n"], "current/body", "candidate/body")
    assert diff == "--- current/body\n+++ candidate/body\n@@ -1 +1 @@\n-a\n+b\n"


@pytest.mark.parametrize("name", sorted(EDIT_REQUEST_EXAMPLES), ids=sorted(EDIT_REQUEST_EXAMPLES))
def test_proposal_diffs_in_the_contract_examples_are_reproduced_byte_for_byte(name: str) -> None:
    example: dict[str, Any] = EDIT_REQUEST_EXAMPLES[name]
    base, proposed = example["baseContent"], example["proposedContent"]
    assert (
        title_diff(base["title"], proposed["title"], "base/title", "proposed/title")
        == example["proposalDiff"]["title"]
    )
    assert (
        field_diff(base["body"], proposed["body"], "base/body", "proposed/body")
        == example["proposalDiff"]["body"]
    )


def test_the_contract_has_examples_to_check() -> None:
    assert len(EDIT_REQUEST_EXAMPLES) >= 4
