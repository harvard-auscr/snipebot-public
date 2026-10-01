"""Round 1 spec-conformance breaks against snipebot/export.py.

Each test builds a table by hand, renders it through the real export renderer,
and asserts what spec/30-aggregate-report.md states -- so a failing test marks a
place the code diverges from the spec, not merely from itself. Inputs reuse the
shared builders in tests/_helpers_export.py.
"""

from __future__ import annotations

from snipebot.export import (
    CSV_HEADERS,
    build_groups_table,
    render_table_text,
)

from tests._helpers_export import (
    cand,
    elig_of,
    make_resolver,
    make_roster,
    mkts,
)


def _column_spans(headers, cells):
    """(start, end) character span of each column in a rendered line, computed the
    way the renderer sizes columns (header/cell max) with a two-space separator."""
    widths = [len(h) for h in headers]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    spans = []
    pos = 0
    for w in widths:
        spans.append((pos, pos + w))
        pos += w + 2
    return spans, widths


def test_groups_made_per_member_column_is_left_justified():
    """spec/30-aggregate-report.md section 7 (line 997): "String columns are
    left-justified, integer columns right-justified". `made_per_member` is typed
    `str` in section 2.4 (line 317: "| 5 | `made_per_member` | `str` | ..."), so
    it is a string column and must be left-justified; the code right-justifies it.
    """
    ros = make_roster(True)
    ledger = [
        cand(mkts(2026, 9, 14, 10, 0, 0), "a1", ("a2",)),
        cand(mkts(2026, 9, 14, 11, 0, 0), "a2", ("b1",)),
    ]
    elig = elig_of(ledger, roster=ros)
    rows = build_groups_table(elig, ros, frozenset())
    names = make_resolver(ros).resolve_all(set())

    headers = CSV_HEADERS["groups"]
    col = headers.index("made_per_member")
    text = render_table_text("groups", rows, names)
    lines = text.split("\n")

    # Build the same text cells the renderer built, to locate the column exactly.
    from snipebot.export import table_values, text_cells

    cells = text_cells(table_values("groups", rows, names))
    spans, widths = _column_spans(headers, cells)
    start, end = spans[col]
    width = widths[col]

    # There must be a group whose made_per_member is a formatted figure narrower
    # than the 15-char header, so left/right justification is observable.
    assert any(len(c[col]) < width for c in cells), "no observable per-member cell"

    for cell_row, data_line in zip(cells, lines[2:]):
        value = cell_row[col]
        rendered = data_line[start:end]
        # A left-justified string column places the value flush at the column's
        # left edge, padding only on the right.
        assert rendered == value.ljust(width), (
            f"made_per_member should be left-justified per section 7; "
            f"got {rendered!r} for value {value!r}"
        )
