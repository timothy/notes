"""Three-way merge of a note's title and body (design guide, section 3, "Three-way preview").

Base is the snapshot captured when a request was submitted, current is the live note, proposed is the
request's latest content. The title is one value: agreement or a single changed side wins, otherwise it
is a conflict. The body is merged line by line in the manner of diff3: every run of base lines that at
least one side changed is a chunk; a chunk one side left alone takes the other side's version, identical
edits appear once, and anything else is a conflict reported in one-based, half-open base coordinates
(``start == end`` is an insertion before ``start``). Nothing is ever resolved silently in either side's
favour, and Markdown is never parsed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal

from notes_api.merge.lines import join_lines, split_lines
from notes_api.merge.unified import field_diff, title_diff

Opcode = tuple[str, int, int, int, int]


@dataclass(frozen=True, slots=True)
class Content:
    title: str
    body: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body}


@dataclass(frozen=True, slots=True)
class LineRange:
    start: int
    end: int

    def to_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True, slots=True)
class Conflict:
    field: Literal["title", "body"]
    base_range: LineRange | None
    base: str
    current: str
    proposed: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "baseRange": self.base_range.to_dict() if self.base_range is not None else None,
            "base": self.base,
            "current": self.current,
            "proposed": self.proposed,
        }


@dataclass(frozen=True, slots=True)
class FieldDiff:
    title: str
    body: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body}


@dataclass(frozen=True, slots=True)
class PreviewComputation:
    """The content half of a ``PreviewResult``; the ETags are added by the service."""

    can_merge: bool
    used_final_content: bool
    candidate: Content | None
    conflicts: list[Conflict]
    proposal_diff: FieldDiff
    merge_diff: FieldDiff | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "canMerge": self.can_merge,
            "usedFinalContent": self.used_final_content,
            "candidate": self.candidate.to_dict() if self.candidate is not None else None,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "proposalDiff": self.proposal_diff.to_dict(),
            "mergeDiff": self.merge_diff.to_dict() if self.merge_diff is not None else None,
        }


def merge_title(base: str, current: str, proposed: str) -> tuple[str | None, Conflict | None]:
    if current == proposed:
        return current, None
    if current == base:
        return proposed, None
    if proposed == base:
        return current, None
    return None, Conflict("title", None, base, current, proposed)


def merge_body(base: str, current: str, proposed: str) -> tuple[str | None, list[Conflict]]:
    """The merged body and no conflicts, or ``None`` and the conflicts in base order."""
    base_lines, current_lines, proposed_lines = split_lines(base), split_lines(current), split_lines(proposed)
    current_ops = SequenceMatcher(None, base_lines, current_lines, autojunk=False).get_opcodes()
    proposed_ops = SequenceMatcher(None, base_lines, proposed_lines, autojunk=False).get_opcodes()
    merged: list[str] = []
    conflicts: list[Conflict] = []
    cursor = 0
    for start, end in _unstable_regions(current_ops, proposed_ops):
        merged.extend(base_lines[cursor:start])
        base_chunk = base_lines[start:end]
        current_chunk = current_lines[_enter(current_ops, start) : _leave(current_ops, end)]
        proposed_chunk = proposed_lines[_enter(proposed_ops, start) : _leave(proposed_ops, end)]
        if current_chunk == base_chunk:
            merged.extend(proposed_chunk)
        elif proposed_chunk == base_chunk or current_chunk == proposed_chunk:
            merged.extend(current_chunk)
        else:
            conflicts.append(
                Conflict(
                    "body",
                    LineRange(start + 1, end + 1),
                    join_lines(base_chunk),
                    join_lines(current_chunk),
                    join_lines(proposed_chunk),
                )
            )
        cursor = end
    merged.extend(base_lines[cursor:])
    if conflicts:
        return None, conflicts
    return join_lines(merged), []


def preview(
    base: Content, current: Content, proposed: Content, final: Content | None = None
) -> PreviewComputation:
    proposal_diff = FieldDiff(
        title=title_diff(base.title, proposed.title, "base/title", "proposed/title"),
        body=field_diff(base.body, proposed.body, "base/body", "proposed/body"),
    )
    merged_title, title_conflict = merge_title(base.title, current.title, proposed.title)
    merged_body, body_conflicts = merge_body(base.body, current.body, proposed.body)
    conflicts = ([title_conflict] if title_conflict is not None else []) + body_conflicts
    if final is not None:
        candidate, used_final = final, True
    elif conflicts:
        return PreviewComputation(False, False, None, conflicts, proposal_diff, None)
    else:
        assert merged_title is not None and merged_body is not None
        candidate, used_final = Content(merged_title, merged_body), False
    merge_diff = FieldDiff(
        title=title_diff(current.title, candidate.title, "current/title", "candidate/title"),
        body=field_diff(current.body, candidate.body, "current/body", "candidate/body"),
    )
    return PreviewComputation(True, used_final, candidate, conflicts, proposal_diff, merge_diff)


def _unstable_regions(current_ops: Sequence[Opcode], proposed_ops: Sequence[Opcode]) -> list[tuple[int, int]]:
    """Base ranges at least one side changed, merged when they overlap or touch, in base order."""
    ranges = sorted(
        (i1, i2) for ops in (current_ops, proposed_ops) for tag, i1, i2, _, _ in ops if tag != "equal"
    )
    regions: list[tuple[int, int]] = []
    for start, end in ranges:
        if regions and start <= regions[-1][1]:
            regions[-1] = (regions[-1][0], max(regions[-1][1], end))
        else:
            regions.append((start, end))
    return regions


def _enter(ops: Sequence[Opcode], base_index: int) -> int:
    """The other side's position where a region starting at ``base_index`` begins."""
    for tag, i1, _, j1, _ in ops:
        if tag != "equal" and i1 == base_index:
            return j1
    return _aligned(ops, base_index)


def _leave(ops: Sequence[Opcode], base_index: int) -> int:
    """The other side's position where a region ending at ``base_index`` ends."""
    for tag, _, i2, _, j2 in ops:
        if tag != "equal" and i2 == base_index:
            return j2
    return _aligned(ops, base_index)


def _aligned(ops: Sequence[Opcode], base_index: int) -> int:
    for tag, i1, i2, j1, _ in ops:
        if tag == "equal" and i1 <= base_index <= i2:
            return j1 + (base_index - i1)
    return 0
