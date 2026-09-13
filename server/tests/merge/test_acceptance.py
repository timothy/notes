"""Acceptance scenarios for the "Text merge" row of the design guide (section 6).

The row: "Unchanged current note accepts the proposal. Changes in separate regions combine; identical
changes appear once. Incompatible title changes, overlapping replacement/deletion, and competing
insertions are conflicts. Cover empty bodies, Unicode, CRLF/LF text, missing final newline, and empty
base ranges." Plus the engine's own promises: determinism and speed on a large body.
"""

import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from notes_api.merge.three_way import Conflict, Content, LineRange, merge_body, preview

pytestmark = pytest.mark.acceptance("Text merge")

BASE = Content("Release checklist", "## Release\n\n- Run tests\n- Deploy\n")


def test_unchanged_current_note_accepts_the_proposal() -> None:
    proposed = Content(BASE.title, "## Release\n\n- Run tests\n- Review metrics\n- Deploy\n")
    result = preview(base=BASE, current=BASE, proposed=proposed)
    assert result.can_merge and result.conflicts == []
    assert result.candidate == proposed
    assert result.merge_diff is not None and result.merge_diff.body == result.proposal_diff.body.replace(
        "base/body", "current/body"
    ).replace("proposed/body", "candidate/body")


def test_changes_in_separate_regions_combine() -> None:
    current = Content("Release checklist (2026)", BASE.body)
    proposed = Content(BASE.title, "## Release\n\n- Run tests\n- Deploy\n- Announce\n")
    result = preview(base=BASE, current=current, proposed=proposed)
    assert result.candidate == Content(
        "Release checklist (2026)", "## Release\n\n- Run tests\n- Deploy\n- Announce\n"
    )
    assert result.conflicts == []


def test_identical_changes_appear_once() -> None:
    same = Content("Release runbook", "## Release\n\n- Run tests\n- Review metrics\n- Deploy\n")
    result = preview(base=BASE, current=same, proposed=same)
    assert result.candidate == same
    assert result.candidate is not None and result.candidate.body.count("- Review metrics\n") == 1


def test_incompatible_title_changes_conflict() -> None:
    result = preview(
        base=BASE,
        current=Content("Release checklist (2026)", BASE.body),
        proposed=Content("Release runbook", BASE.body),
    )
    assert not result.can_merge and result.candidate is None and result.merge_diff is None
    assert result.conflicts == [
        Conflict("title", None, "Release checklist", "Release checklist (2026)", "Release runbook")
    ]


def test_overlapping_replacement_and_deletion_conflict() -> None:
    merged, conflicts = merge_body(
        BASE.body, "## Release\n\n- Run tests\n- Deploy to production\n", "## Release\n\n- Run tests\n"
    )
    assert merged is None
    assert conflicts == [Conflict("body", LineRange(4, 5), "- Deploy\n", "- Deploy to production\n", "")]


def test_competing_insertions_conflict_with_an_empty_base_range() -> None:
    merged, conflicts = merge_body(
        BASE.body,
        "## Release\n\n- Run tests\n- Lint\n- Deploy\n",
        "## Release\n\n- Run tests\n- Review metrics\n- Deploy\n",
    )
    assert merged is None
    assert conflicts == [Conflict("body", LineRange(4, 4), "", "- Lint\n", "- Review metrics\n")]
    assert (
        conflicts[0].base_range is not None and conflicts[0].base_range.start == conflicts[0].base_range.end
    )


def test_empty_bodies() -> None:
    assert merge_body("", "", "First line\n") == ("First line\n", [])
    assert merge_body("", "", "") == ("", [])
    assert merge_body("Only line\n", "Only line\n", "") == ("", [])
    merged, conflicts = merge_body("", "Mine\n", "Yours\n")
    assert merged is None and conflicts[0].base_range == LineRange(1, 1)


def test_unicode_is_compared_by_code_points_without_normalization() -> None:
    precomposed, decomposed = "café\n", "café\n"
    assert merge_body(precomposed, precomposed, decomposed) == (decomposed, [])
    merged, conflicts = merge_body(precomposed, decomposed, "CAFÉ\n")
    assert merged is None and len(conflicts) == 1
    rtl = "שלום\n"
    assert merge_body("hello\n" + rtl, "hello!\n" + rtl, "hello\n" + rtl + "🙂 emoji\n") == (
        "hello!\n" + rtl + "🙂 emoji\n",
        [],
    )


def test_crlf_and_lf_are_preserved_as_written() -> None:
    base = "one\r\ntwo\nthree\r\n"
    assert merge_body(base, base, "one\r\ntwo changed\nthree\r\n") == ("one\r\ntwo changed\nthree\r\n", [])
    assert merge_body(base, "one\r\ntwo\r\nthree\r\n", base) == ("one\r\ntwo\r\nthree\r\n", [])


def test_missing_final_newline_is_content() -> None:
    assert merge_body("a\nb", "a\nb", "a\nb\n") == ("a\nb\n", [])
    merged, conflicts = merge_body("a\nb", "a\nb\nc", "a\nb\n")
    assert merged is None
    assert conflicts == [Conflict("body", LineRange(2, 3), "b", "b\nc", "b\n")]


def test_conflict_ranges_cover_the_whole_changed_run() -> None:
    base = "1\n2\n3\n4\n5\n6\n"
    merged, conflicts = merge_body(base, "1\nx\ny\n4\n5\n6\n", "1\n2\nz\n4\n5\n6\n")
    assert merged is None
    assert conflicts == [Conflict("body", LineRange(2, 4), "2\n3\n", "x\ny\n", "2\nz\n")]


LINES = st.lists(st.sampled_from(["a\n", "b\n", "c\n", "d\n", "e"]), max_size=10).map("".join)


@settings(max_examples=100, derandomize=True)
@given(base=LINES, current=LINES, proposed=LINES)
def test_merging_is_a_pure_function(base: str, current: str, proposed: str) -> None:
    first = merge_body(base, current, proposed)
    second = merge_body(base, current, proposed)
    assert first == second
    merged, conflicts = first
    assert (merged is None) == bool(conflicts)


def test_a_five_thousand_line_body_previews_in_under_a_second() -> None:
    lines = [f"line {i}\n" for i in range(5000)]
    current, proposed = list(lines), list(lines)
    current[100] = "line 100 edited here\n"
    proposed[4900] = "line 4900 edited there\n"
    started = time.perf_counter()
    result = preview(
        base=Content("T", "".join(lines)),
        current=Content("T", "".join(current)),
        proposed=Content("T", "".join(proposed)),
    )
    elapsed = time.perf_counter() - started
    assert result.can_merge and result.candidate is not None
    assert (
        "line 100 edited here\n" in result.candidate.body
        and "line 4900 edited there\n" in result.candidate.body
    )
    assert elapsed < 1.0, f"took {elapsed:.2f}s"
