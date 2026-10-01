"""Exports and plain-text standings tables (30-aggregate-report.md sections 6-7).

Builds the six standings tables via `snipebot.aggregate`, resolves display names via
`snipebot.report.NameResolver`, and renders them three ways: CSV files, one XLSX
workbook, and the fixed-width text table the `report` CLI prints to stdout. Names are
resolved, never escaped -- every context here is plain text, not Slack mrkdwn.

Each table's raw cell values are built once per format-independent row (ints stay
int, `best_day` may be `None`); the three renderers below differ only in how they
print a `None` and whether an int stays numeric (XLSX) or becomes text (CSV, table).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import AbstractSet

from openpyxl import Workbook
from openpyxl.styles import Font

from snipebot.aggregate import (
    Eligibility,
    GroupRow,
    MostSnipedRow,
    PairRow,
    PersonRow,
    SnipeRow,
    DailyRow,
    build_daily_table,
    build_groups_table,
    build_most_sniped_table,
    build_pairs_table,
    build_people_table,
    build_snipes_table,
)
from snipebot.config import Roster
from snipebot.report import NameResolver

# Table names in the fixed order every multi-table output (XLSX sheets, `--by`
# listing) follows (30 section 2 / section 6).
TABLE_ORDER: tuple[str, ...] = (
    "snipes", "daily", "people", "groups", "most_sniped", "pairs",
)

# CSV/table display headers, exactly the section 6 order (section 6 table).
CSV_HEADERS: dict[str, tuple[str, ...]] = {
    "snipes": ("date", "time", "sniper", "target", "sniper_group", "target_group"),
    "daily": ("date", "snipes", "unique_snipers", "unique_targets",
              "cooldown_rejections", "points"),
    "people": ("person", "group", "snipes_made", "times_sniped", "unique_targets",
               "unique_snipers", "best_day", "points"),
    "groups": ("group", "members", "made", "sniped", "made_per_member",
               "sniped_per_member", "intra_group", "cross_group", "points",
               "points_per_member"),
    "most_sniped": ("rank", "person", "group", "times_sniped", "top_sniper_of_them"),
    "pairs": ("sniper", "target", "count", "points"),
}

# `report --by` name -> table name (section 7 table).
BY_TO_TABLE: dict[str, str] = {
    "day": "daily",
    "person": "people",
    "group": "groups",
    "target": "most_sniped",
    "snipes": "snipes",
    "pairs": "pairs",
}

# Column alignment for the text table: True = right-justified (an integer
# column), False = left-justified (an identifier, a date/time, a name, or a
# str-typed per-member column).
_ALIGN: dict[str, tuple[bool, ...]] = {
    "snipes": (False, False, False, False, False, False),
    "daily": (False, True, True, True, True, True),
    "people": (False, False, True, True, True, True, False, True),
    "groups": (False, True, True, True, False, False, True, True, True, False),
    "most_sniped": (True, False, False, True, False),
    "pairs": (False, False, True, True),
}

# Header keys whose column holds a resolved display name rather than an ID,
# a count, or a date -- the cells S19 requires guarding against spreadsheet
# formula/command injection and against XML-illegal control characters.
_NAME_HEADER_KEYS: frozenset[str] = frozenset(
    {"sniper", "target", "person", "top_sniper_of_them"}
)

_NAME_COLUMN_INDICES: dict[str, frozenset[int]] = {
    table: frozenset(i for i, h in enumerate(headers) if h in _NAME_HEADER_KEYS)
    for table, headers in CSV_HEADERS.items()
}

# Everything outside the XML 1.0 `Char` production, which XML (and so openpyxl)
# cannot hold: C0 controls other than tab/LF/CR, lone surrogates, U+FFFE and
# U+FFFF (S19, E-W4-13).
_ILLEGAL_XML_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

# A leading one of these makes a spreadsheet read the cell as a formula or
# command rather than plain text (S19).
_INJECTION_LEAD_CHARS: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def _strip_illegal_xml_chars(s: str) -> str:
    """Drop characters XML can't hold, so openpyxl never raises
    `IllegalCharacterError` (S19)."""
    return _ILLEGAL_XML_CHARS_RE.sub("", s)


def _needs_text_guard(s: str) -> bool:
    """True if a spreadsheet application would read `s` as a formula/command
    rather than literal text (S19)."""
    return bool(s) and s[0] in _INJECTION_LEAD_CHARS


# --------------------------------------------------------------------------- #
# Table building
# --------------------------------------------------------------------------- #

def build_all_tables(
    elig: Eligibility, roster: Roster, opted_out: AbstractSet[str]
) -> dict[str, tuple]:
    """The six tables in `TABLE_ORDER`, keyed by table name."""
    return {
        "snipes": build_snipes_table(elig, roster),
        "daily": build_daily_table(elig),
        "people": build_people_table(elig, roster),
        "groups": build_groups_table(elig, roster, opted_out),
        "most_sniped": build_most_sniped_table(elig, roster),
        "pairs": build_pairs_table(elig),
    }


def collect_display_ids(tables: Mapping[str, Sequence]) -> set[str]:
    """Every user ID appearing in any of the given tables (sniper/target/person/
    top_sniper_of_them columns) -- the set to resolve through `NameResolver`."""
    ids: set[str] = set()
    for row in tables.get("snipes", ()):
        ids.add(row.sniper)
        ids.add(row.target)
    for row in tables.get("people", ()):
        ids.add(row.person)
    for row in tables.get("most_sniped", ()):
        ids.add(row.person)
        ids.add(row.top_sniper_of_them)
    for row in tables.get("pairs", ()):
        ids.add(row.sniper)
        ids.add(row.target)
    return ids


# --------------------------------------------------------------------------- #
# Raw row values (format-independent; `None` only ever appears for `best_day`)
# --------------------------------------------------------------------------- #

def _snipes_values(row: SnipeRow, names: Mapping[str, str]) -> tuple:
    return (row.date, row.time, names[row.sniper], names[row.target],
            row.sniper_group, row.target_group)


def _daily_values(row: DailyRow, names: Mapping[str, str]) -> tuple:
    return (row.date, row.snipes, row.unique_snipers, row.unique_targets,
            row.cooldown_rejections, row.points)


def _people_values(row: PersonRow, names: Mapping[str, str]) -> tuple:
    return (names[row.person], row.group, row.snipes_made, row.times_sniped,
            row.unique_targets, row.unique_snipers, row.best_day, row.points)


def _groups_values(row: GroupRow, names: Mapping[str, str]) -> tuple:
    return (row.group, row.members, row.made, row.sniped,
            row.made_per_member(), row.sniped_per_member(), row.intra_group,
            row.cross_group, row.points, row.points_per_member())


def _most_sniped_values(row: MostSnipedRow, names: Mapping[str, str]) -> tuple:
    return (row.rank, names[row.person], row.group, row.times_sniped,
            names[row.top_sniper_of_them])


def _pairs_values(row: PairRow, names: Mapping[str, str]) -> tuple:
    return (names[row.sniper], names[row.target], row.count, row.points)


_ROW_VALUES = {
    "snipes": _snipes_values,
    "daily": _daily_values,
    "people": _people_values,
    "groups": _groups_values,
    "most_sniped": _most_sniped_values,
    "pairs": _pairs_values,
}


def table_values(table: str, rows: Sequence, names: Mapping[str, str]) -> list[tuple]:
    """Raw per-row cell values for `table`, in `CSV_HEADERS[table]` order."""
    build = _ROW_VALUES[table]
    return [build(row, names) for row in rows]


# --------------------------------------------------------------------------- #
# Format-specific cell rendering
# --------------------------------------------------------------------------- #

def csv_cells(table: str, values: Sequence[tuple]) -> list[tuple[str, ...]]:
    """`table_values` cells rendered for CSV: name columns (S19) have illegal
    XML control characters dropped and get a leading `'` if the cleaned value
    would otherwise read as a formula/command when opened in a spreadsheet."""
    name_cols = _NAME_COLUMN_INDICES[table]
    rows = []
    for row in values:
        cells = []
        for i, v in enumerate(row):
            cell = "" if v is None else str(v)
            if i in name_cols and cell:
                cell = _strip_illegal_xml_chars(cell)
                if _needs_text_guard(cell):
                    cell = "'" + cell
            cells.append(cell)
        rows.append(tuple(cells))
    return rows


def text_cells(values: Sequence[tuple]) -> list[tuple[str, ...]]:
    return [tuple("—" if v is None else str(v) for v in row) for row in values]


def xlsx_cells(table: str, values: Sequence[tuple]) -> list[tuple]:
    """`table_values` cells rendered for XLSX: name columns (S19) have illegal
    XML control characters dropped so openpyxl never raises
    `IllegalCharacterError`; the string-cell (text) guard is applied
    separately in `write_xlsx` via the cell's `data_type`."""
    name_cols = _NAME_COLUMN_INDICES[table]
    rows = []
    for row in values:
        cells = []
        for i, v in enumerate(row):
            if v is None:
                cells.append("")
            elif i in name_cols and isinstance(v, str):
                cells.append(_strip_illegal_xml_chars(v))
            else:
                cells.append(v)
        rows.append(tuple(cells))
    return rows


# --------------------------------------------------------------------------- #
# Text table (section 7: the `report` CLI's stdout tables)
# --------------------------------------------------------------------------- #

def render_table_text(table: str, rows: Sequence, names: Mapping[str, str]) -> str:
    """The fixed-width text table for `table`: header row, a `-` rule line sized
    to each column, then one line per row, columns separated by two spaces.
    Every line is right-stripped (S20); a column's width is its code-point
    count (`len`, S20); headers are always left-justified, regardless of the
    column's own alignment (S20)."""
    headers = CSV_HEADERS[table]
    align = _ALIGN[table]
    cells = text_cells(table_values(table, rows, names))

    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths).rstrip())
    for row in cells:
        lines.append("  ".join(
            (cell.rjust(widths[i]) if align[i] else cell.ljust(widths[i]))
            for i, cell in enumerate(row)
        ).rstrip())
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CSV (section 6)
# --------------------------------------------------------------------------- #

def write_csv(path: Path, table: str, rows: Sequence, names: Mapping[str, str]) -> None:
    """One `<semester>_<table>.csv`: `csv.writer`, `QUOTE_MINIMAL` (the default),
    `\\n` line endings, `utf-8-sig` so Excel renders accented/emoji names."""
    headers = CSV_HEADERS[table]
    cells = csv_cells(table, table_values(table, rows, names))
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(cells)


# --------------------------------------------------------------------------- #
# XLSX (section 6)
# --------------------------------------------------------------------------- #

def write_xlsx(path: Path, tables: Mapping[str, tuple], names: Mapping[str, str]) -> None:
    """One workbook, one sheet per table in `TABLE_ORDER`, bold frozen header row.
    Integer columns stay numeric; the per-member ratio columns are the "%.2f" (or
    "—") strings the CSV also carries. Every string in a name column (S19) is
    forced to a string cell via `data_type` after assignment, so a leading `=`
    is never evaluated and a name spelled like an error code ('#N/A') stays
    text rather than becoming an error cell."""
    wb = Workbook()
    wb.remove(wb.active)
    for table in TABLE_ORDER:
        ws = wb.create_sheet(title=table)
        ws.append(list(CSV_HEADERS[table]))
        for cell in ws[1]:
            cell.font = Font(bold=True)
        name_cols = _NAME_COLUMN_INDICES[table]
        for row in xlsx_cells(table, table_values(table, tables[table], names)):
            ws.append(list(row))
            r = ws.max_row
            for idx in name_cols:
                val = row[idx]
                if isinstance(val, str):
                    ws.cell(row=r, column=idx + 1).data_type = "s"
        ws.freeze_panes = "A2"
    wb.save(str(path))


# --------------------------------------------------------------------------- #
# Top-level: `snipebot export`
# --------------------------------------------------------------------------- #

def export_all(
    elig: Eligibility,
    roster: Roster,
    opted_out: AbstractSet[str],
    resolver: NameResolver,
    semester_name: str,
    out_dir: Path,
) -> None:
    """Writes the six `<semester>_<table>.csv` files plus `<semester>.xlsx` into
    `out_dir`, creating it if absent (section 6)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = build_all_tables(elig, roster, opted_out)
    names = resolver.resolve_all(collect_display_ids(tables))

    for table in TABLE_ORDER:
        write_csv(out_dir / f"{semester_name}_{table}.csv", table, tables[table], names)
    write_xlsx(out_dir / f"{semester_name}.xlsx", tables, names)
