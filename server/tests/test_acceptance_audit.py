"""Every row of the design guide's section 6 acceptance table has at least one test tagged with it, and no
test claims a row the table does not have.

``--strict-markers`` checks the mark's name, not its argument, so this is the only place a misspelt row would
show. The audit needs the whole collection, so it skips itself when pytest was pointed at specific paths or
filtered with ``-k`` or ``-m``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from notes_api.contract import DEFAULT_CONTRACT_PATH

GUIDE = Path(DEFAULT_CONTRACT_PATH).parent / "docs" / "design-guide.md"
TABLE_HEADER = "| Area | Required scenarios and observable results |"
ROW_COUNT = 18


def acceptance_rows() -> set[str]:
    """The first cell of every row of the section 6 table, read from the guide itself."""
    table = GUIDE.read_text(encoding="utf-8").split(TABLE_HEADER, 1)[1]
    rows: set[str] = set()
    for line in table.splitlines():
        if not line.startswith("| "):
            if rows:
                break
            continue
        cell = line.split("|")[1].strip()
        if set(cell) <= {"-"}:
            continue  # the separator row
        rows.add(cell)
    return rows


def test_the_guide_lists_eighteen_acceptance_rows() -> None:
    assert len(acceptance_rows()) == ROW_COUNT


def test_every_acceptance_row_has_a_test_and_every_mark_names_a_row(request: pytest.FixtureRequest) -> None:
    config = request.config
    partial = config.args_source is not pytest.Config.ArgsSource.TESTPATHS
    if partial or config.getoption("keyword") or config.getoption("markexpr"):
        pytest.skip("the acceptance audit needs the whole collection")
    rows = acceptance_rows()
    covered = {
        str(row)
        for item in request.session.items
        for mark in item.iter_markers("acceptance")
        for row in mark.args
    }
    assert rows - covered == set(), f"rows without a test: {sorted(rows - covered)}"
    assert covered - rows == set(), f"marks naming no row: {sorted(covered - rows)}"
