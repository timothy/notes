"""Split text into lines without losing a byte.

Only ``\\n`` ends a line, and each line keeps its own ending, so ``\\r\\n`` survives inside the line and
``join_lines(split_lines(text)) == text`` for every text. ``str.splitlines`` is deliberately not used:
it also breaks on ``\\x0b``, ``\\u2028``, and other separators that a diff must treat as content.
"""

from __future__ import annotations

from collections.abc import Iterable


def split_lines(text: str) -> list[str]:
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def join_lines(lines: Iterable[str]) -> str:
    return "".join(lines)
