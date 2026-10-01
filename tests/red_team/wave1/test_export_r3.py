"""Round 3 (invariants and interactions) breaks against snipebot/export.py.

Round 3 targets properties that must hold across functions and across the
aggregate -> resolve -> render pipeline: determinism/purity, conservation,
order-independence, cross-format consistency, and the section 6/7 rendering
contract. Every candidate below was built by hand, pushed through the real
renderers, and checked against spec/30-aggregate-report.md sections 6-7.

Result: no spec-anchored invariant break survived verification. Each probe the
module passed is documented here (not left as a passing test) so the negative
result is auditable; the two genuine spec gaps found are reported separately as
spec issues, not as findings.

Probes that the current code satisfies (so they are NOT tests here):

  Column contract (section 6, lines 945-957 / section 7, lines 995-998)
  - CSV_HEADERS and every _*_values tuple match the section 6 header rows and
    the section 2 column order for all six tables (points / points_per_member
    appended last for daily/people/groups/pairs; snipes and most_sniped raw).
  - _ALIGN encodes the section 7 binary rule exactly for all six tables: every
    section 2 `str` column (incl. best_day and the three per-member `str`
    columns) is left-justified, every `int` column right-justified. The round-1
    per-member right-justify break is fixed and stays fixed.
  - Sheet order/names, CSV file names and the .xlsx file name all match
    section 6 (lines 938-966).

  Cross-format consistency (the three renderers share table_values)
  - CSV, text and XLSX render identical underlying values; they differ only as
    the spec prescribes: best_day None -> "" (CSV, section 6 line 943), "—"
    (text, section 7 line 998), "" (XLSX); ints stay numeric in XLSX and become
    text in CSV/text; per-member figures are the "%.2f" string (or "—") in all
    three, and stay string-typed cells in XLSX (not coerced to numbers).
  - Name resolution is applied once over collect_display_ids(all tables) and the
    one names map feeds every table, so a collision disambiguation (e.g.
    "Sam (sibB) [b1]") is byte-identical across snipes/people/pairs/most_sniped.
    collect_display_ids covers every id any renderer indexes (sniper, target,
    person, top_sniper_of_them), so export_all never raises KeyError.

  Edge inputs
  - Names carrying comma/quote/unicode are QUOTE_MINIMAL-quoted in CSV, kept as
    plain (unescaped) text cells in XLSX/text per section 4 line 636, and
    numeric- or date-looking names ("12345", "2026-09-14", "10:00:00") are not
    coerced to numbers/dates in XLSX.
  - CSV bytes carry the utf-8-sig BOM once and use bare "\n" (no CRLF) per
    section 6 line 959; empty tables still print header + rule; export_all
    creates a missing (nested) output directory.

  Purity / determinism
  - The renderers do not mutate their inputs; CSV output is byte-identical across
    two runs. (XLSX is not byte-identical across runs -- see spec issue -- but
    section 6 imposes no reproducibility requirement on exports, which are never
    committed, line 935, so this is a spec gap, not a code break.)

No test functions: a passing assertion is not a finding.
"""

from __future__ import annotations
