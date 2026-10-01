"""Round 1 red-team tests: spec-conformance breaks in snipebot/parse.py and
snipebot/ts.py.

Each test is a proof that the shipped code violates a stated rule in the spec.
Inputs are built by hand; every test is expected to FAIL against the current
code (a passing test would not be a finding).
"""

from __future__ import annotations

import pytest

from snipebot.ts import TsFormatError, parse_ts


def test_parse_ts_rejects_trailing_newline_whitespace() -> None:
    """parse_ts must reject a ts carrying trailing whitespace.

    spec 00-data.md section 1 (ts.py), parse_ts docstring:
    "Raises TsFormatError on any other shape (missing dot, wrong fraction width,
    sign, exponent, whitespace, empty)."
    The stated rationale (same section): rejecting any other shape "turns a
    malformed or float-mangled ts into a loud TsFormatError instead of a silent
    miscompare."

    A trailing newline is whitespace, so the string is not a well-formed Slack
    ts and parse_ts must raise. The grammar is anchored with `$`, which in
    Python matches immediately before a single trailing newline, so
    `^\\d+\\.\\d{6}$` accepts "1758210000.000100\\n"; the split then feeds
    int("000100\\n"), and int() strips whitespace, so the malformed ts is parsed
    silently instead of raising. (A correct anchor is `\\Z`.)
    """
    with pytest.raises(TsFormatError):
        parse_ts("1758210000.000100\n")
