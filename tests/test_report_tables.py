"""Unit tests for the text-table renderer (30-aggregate-report.md section 7):
`--by` table selection, name resolution, alignment and the `best_day` dash.
"""

from __future__ import annotations

from snipebot.aggregate import build_daily_table, build_people_table, build_snipes_table
from snipebot.export import (
    BY_TO_TABLE,
    CSV_HEADERS,
    TABLE_ORDER,
    build_all_tables,
    collect_display_ids,
    render_table_text,
    table_values,
    text_cells,
)

from tests._helpers_export import ROS, cand, elig_of, make_resolver, mkts, selfie


def _sample_elig():
    ledger = [
        cand(mkts(2026, 9, 14, 10, 0, 0), "a1", ("b1",)),
        cand(mkts(2026, 9, 15, 11, 0, 0), "a1", ("b2",)),
        selfie(mkts(2026, 9, 15, 9, 0, 0), "x1", ("a2",)),
        cand(mkts(2026, 9, 14, 9, 0, 0), "a2", ("a1",)),
    ]
    return elig_of(ledger)


def _names_for(tables):
    resolver = make_resolver()
    return resolver.resolve_all(collect_display_ids(tables))


def _widths_and_cells(table, rows, names):
    """Column widths and text cells, computed the same way the renderer
    computes them, so tests can locate a column by exact offset instead of
    guessing at a two-space split (data can be wider than its header)."""
    headers = CSV_HEADERS[table]
    cells = text_cells(table_values(table, rows, names))
    widths = [len(h) for h in headers]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    return widths, cells


def _offsets(widths):
    """(start, end) character span of each column, given the two-space
    separator between columns."""
    spans = []
    pos = 0
    for w in widths:
        spans.append((pos, pos + w))
        pos += w + 2
    return spans


# --------------------------------------------------------------------------- #
# `--by` mapping (section 7 table)
# --------------------------------------------------------------------------- #

def test_by_flag_maps_to_the_right_table():
    assert BY_TO_TABLE == {
        "day": "daily",
        "person": "people",
        "group": "groups",
        "target": "most_sniped",
        "snipes": "snipes",
        "pairs": "pairs",
    }


# --------------------------------------------------------------------------- #
# render_table_text: shape and formatting
# --------------------------------------------------------------------------- #

def test_render_table_header_then_rule_then_one_line_per_row():
    elig = _sample_elig()
    rows = build_people_table(elig, ROS)
    names = _names_for({"people": rows})
    widths, cells = _widths_and_cells("people", rows, names)
    text = render_table_text("people", rows, names)
    lines = text.split("\n")

    expected_header = "  ".join(
        h.ljust(widths[i]) for i, h in enumerate(CSV_HEADERS["people"])
    ).rstrip()
    expected_rule = "  ".join("-" * w for w in widths).rstrip()
    assert lines[0] == expected_header
    assert lines[1] == expected_rule
    # header, rule, one line per row, plus the trailing newline's empty split.
    assert len(lines) == 2 + len(rows) + 1
    assert lines[-1] == ""
    assert len(cells) == len(rows)


def test_render_table_uses_resolved_names_not_raw_ids():
    elig = _sample_elig()
    rows = build_people_table(elig, ROS)
    names = _names_for({"people": rows})
    text = render_table_text("people", rows, names)
    for person_id, display in names.items():
        # Every resolved display name for a person who appears must show up.
        if any(r.person == person_id for r in rows):
            assert display in text


def test_render_table_best_day_dash_for_never_sniped():
    elig = _sample_elig()
    rows = build_people_table(elig, ROS)
    names = _names_for({"people": rows})
    widths, cells = _widths_and_cells("people", rows, names)
    spans = _offsets(widths)
    best_day_idx = CSV_HEADERS["people"].index("best_day")
    start, end = spans[best_day_idx]

    text = render_table_text("people", rows, names)
    lines = text.split("\n")
    saw_dash = False
    for row, data_line in zip(rows, lines[2:]):
        rendered = data_line[start:end]
        if row.best_day is None:
            assert rendered.strip() == "—"
            saw_dash = True
        else:
            assert rendered.strip() == row.best_day
    assert saw_dash  # b1/b2 in this ledger never snipe, so this must trigger


def test_render_table_snipes_columns_all_left_justified():
    elig = _sample_elig()
    rows = build_snipes_table(elig, ROS)
    names = _names_for({"snipes": rows})
    widths, cells = _widths_and_cells("snipes", rows, names)
    spans = _offsets(widths)

    text = render_table_text("snipes", rows, names)
    lines = text.split("\n")
    for cell_row, data_line in zip(cells, lines[2:]):
        for i, cell in enumerate(cell_row):
            start, end = spans[i]
            # left-justified: the value starts flush at the column's left edge.
            assert data_line[start:start + len(cell)] == cell


def test_render_table_daily_points_column_right_justified():
    elig = _sample_elig()
    rows = build_daily_table(elig)
    names = {}
    widths, cells = _widths_and_cells("daily", rows, names)
    spans = _offsets(widths)
    points_idx = CSV_HEADERS["daily"].index("points")
    start, end = spans[points_idx]

    text = render_table_text("daily", rows, names)
    lines = text.split("\n")
    for cell_row, data_line in zip(cells, lines[2:]):
        cell = cell_row[points_idx]
        # right-justified: the value ends flush at the column's right edge.
        assert data_line[end - len(cell):end] == cell
        assert data_line[start:end - len(cell)] == " " * (end - len(cell) - start)


def test_render_table_empty_rows_still_prints_header_and_rule():
    widths, _ = _widths_and_cells("pairs", (), {})
    text = render_table_text("pairs", (), {})
    lines = text.split("\n")
    assert lines[0] == "  ".join(
        h.ljust(widths[i]) for i, h in enumerate(CSV_HEADERS["pairs"])
    ).rstrip()
    assert lines[1] == "  ".join("-" * w for w in widths).rstrip()
    assert lines[2] == ""


# --------------------------------------------------------------------------- #
# table_values: raw values feed both the CSV writer and the text renderer
# --------------------------------------------------------------------------- #

def test_table_values_people_column_order_matches_headers():
    elig = _sample_elig()
    rows = build_people_table(elig, ROS)
    names = _names_for({"people": rows})
    values = table_values("people", rows, names)
    assert len(CSV_HEADERS["people"]) == 8
    for row_values in values:
        assert len(row_values) == 8


def test_table_values_never_exposes_raw_id_for_resolved_columns():
    elig = _sample_elig()
    rows = build_people_table(elig, ROS)
    names = _names_for({"people": rows})
    values = table_values("people", rows, names)
    raw_ids = {r.person for r in rows}
    for row_values in values:
        person_cell = row_values[0]
        assert person_cell not in raw_ids or person_cell.startswith("[")


# --------------------------------------------------------------------------- #
# Adversarial: every table, not just the cherry-picked ones above -- a header/
# alignment-tuple length mismatch on an untested table would otherwise raise
# only at runtime, never in this suite.
# --------------------------------------------------------------------------- #

def test_every_table_renders_without_error_and_matches_its_own_header_width():
    elig = _sample_elig()
    tables = build_all_tables(elig, ROS, frozenset())
    names = _names_for(tables)
    for table in TABLE_ORDER:
        rows = tables[table]
        widths, _ = _widths_and_cells(table, rows, names)
        text = render_table_text(table, rows, names)
        lines = text.split("\n")
        expected_header = "  ".join(
            h.ljust(widths[i]) for i, h in enumerate(CSV_HEADERS[table])
        ).rstrip()
        assert lines[0] == expected_header, table
        assert lines[1] == "  ".join("-" * w for w in widths).rstrip(), table
        assert len(lines) == 2 + len(rows) + 1, table


def test_every_table_values_row_width_matches_its_header():
    elig = _sample_elig()
    tables = build_all_tables(elig, ROS, frozenset())
    names = _names_for(tables)
    for table in TABLE_ORDER:
        for row_values in table_values(table, tables[table], names):
            assert len(row_values) == len(CSV_HEADERS[table]), table


# --------------------------------------------------------------------------- #
# Adversarial: a genuine display-name collision (same name, same group) must
# not crash the table renderer and must still disambiguate to distinct text.
# --------------------------------------------------------------------------- #

def test_render_table_survives_same_name_same_group_collision():
    ledger = [
        cand(mkts(2026, 9, 14, 10, 0, 0), "b1", ("b2",)),
        cand(mkts(2026, 9, 14, 11, 0, 0), "b2", ("b1",)),
    ]
    elig = elig_of(ledger)
    rows = build_people_table(elig, ROS)
    names = _names_for({"people": rows})
    # b1 and b2 share the cached display name "Sam" and the same sibling
    # group -> NameResolver must fall back to the bracketed raw ID.
    assert names["b1"] != names["b2"]
    text = render_table_text("people", rows, names)
    assert names["b1"] in text
    assert names["b2"] in text
