"""Unified diffs in the contract's format: three context lines, git-style hunk headers, and
``\\ No newline at end of file`` after any line that lacks one.

``difflib.unified_diff`` gets the hunk headers right but cannot emit the missing-newline marker, so the
formatting is done here on top of ``SequenceMatcher``'s grouped opcodes. ``autojunk`` is off so the
result depends only on the two texts.
"""

from __future__ import annotations

from collections.abc import Sequence
from difflib import SequenceMatcher

from notes_api.merge.lines import split_lines

CONTEXT_LINES = 3
NO_NEWLINE_MARKER = "\\ No newline at end of file\n"


def unified_diff(a: Sequence[str], b: Sequence[str], from_label: str, to_label: str) -> str:
    """The unified diff between two line sequences, or ``""`` when they are equal."""
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    groups = list(matcher.get_grouped_opcodes(CONTEXT_LINES))
    if not groups:
        return ""
    out = [f"--- {from_label}\n", f"+++ {to_label}\n"]
    for group in groups:
        first, last = group[0], group[-1]
        out.append(f"@@ -{_hunk_range(first[1], last[2])} +{_hunk_range(first[3], last[4])} @@\n")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out.extend(_diff_line(" ", line) for line in a[i1:i2])
                continue
            if tag in ("replace", "delete"):
                out.extend(_diff_line("-", line) for line in a[i1:i2])
            if tag in ("replace", "insert"):
                out.extend(_diff_line("+", line) for line in b[j1:j2])
    return "".join(out)


def field_diff(a: str, b: str, from_label: str, to_label: str) -> str:
    """Diff of two body texts; an unchanged field is the empty string."""
    if a == b:
        return ""
    return unified_diff(split_lines(a), split_lines(b), from_label, to_label)


def title_diff(a: str, b: str, from_label: str, to_label: str) -> str:
    """A title is one value diffed as a single line with a virtual terminating newline."""
    if a == b:
        return ""
    return unified_diff([a + "\n"], [b + "\n"], from_label, to_label)


def _diff_line(prefix: str, line: str) -> str:
    if line.endswith("\n"):
        return prefix + line
    return prefix + line + "\n" + NO_NEWLINE_MARKER


def _hunk_range(start: int, stop: int) -> str:
    """Git's range notation: ``start`` alone for one line, ``start,length`` otherwise; empty ranges
    point at the line before them."""
    beginning = start + 1
    length = stop - start
    if length == 1:
        return str(beginning)
    if length == 0:
        beginning -= 1
    return f"{beginning},{length}"
