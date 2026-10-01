"""ts properties: L2-TS-roundtrip-a, L2-TS-roundtrip-b, L2-TS-ordering."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from snipebot.ts import (
    US_PER_MINUTE,
    US_PER_SECOND,
    TsFormatError,
    format_ts,
    parse_ts,
)

_secs = st.integers(min_value=0, max_value=10 ** 13)
_frac = st.integers(min_value=0, max_value=999_999)


def test_constants() -> None:
    assert US_PER_SECOND == 1_000_000
    assert US_PER_MINUTE == 60_000_000
    assert US_PER_MINUTE == 60 * US_PER_SECOND


def test_parse_rule_exact() -> None:
    # 1758210000.000199 -> 1758210000*10**6 + 199
    assert parse_ts("1758210000.000199") == 1758210000 * 10 ** 6 + 199


@given(secs=_secs, frac=_frac)
def test_ts_roundtrip_a(secs: int, frac: int) -> None:
    # L2-TS-roundtrip-a: format_ts(parse_ts(s)) == s for every grammar-valid s
    # (canonical seconds, as Slack emits and format_ts produces).
    s = f"{secs}.{frac:06d}"
    assert format_ts(parse_ts(s)) == s


@given(t=st.integers(min_value=0, max_value=10 ** 19))
def test_ts_roundtrip_b(t: int) -> None:
    # L2-TS-roundtrip-b: parse_ts(format_ts(t)) == t for t >= 0.
    assert parse_ts(format_ts(t)) == t


@given(a_secs=_secs, a_frac=_frac, b_secs=_secs, b_frac=_frac)
def test_ts_ordering(a_secs: int, a_frac: int, b_secs: int, b_frac: int) -> None:
    # L2-TS-ordering: chronological order (seconds, then same-second counter)
    # equals integer < on the parsed Ts.
    a = f"{a_secs}.{a_frac:06d}"
    b = f"{b_secs}.{b_frac:06d}"
    assert (parse_ts(a) < parse_ts(b)) == ((a_secs, a_frac) < (b_secs, b_frac))


def test_ordering_digit_width_trap() -> None:
    # The exact break the property guards: a 10-digit seconds field compares
    # GREATER than an 11-digit one as raw strings, but earlier as parsed Ts.
    earlier = "9999999999.000000"    # 10-digit seconds
    later = "10000000000.000000"     # 11-digit seconds, chronologically later
    assert parse_ts(earlier) < parse_ts(later)
    assert earlier > later           # raw string comparison misorders


def test_same_second_counter_orders() -> None:
    assert parse_ts("1758210000.000001") < parse_ts("1758210000.000002")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "1758210000",           # no dot
        "1758210000.",          # empty fraction
        ".000199",              # no seconds
        "1758210000.0001",      # fraction too short
        "1758210000.0001999",   # fraction too long
        "1758210000.00019",     # five-digit fraction
        "-1.000000",            # sign
        "+1.000000",
        "1e3.000000",           # exponent
        "1.5e3.000000",
        " 1758210000.000199",   # leading whitespace
        "1758210000.000199 ",   # trailing whitespace
        "1758210000.00019a",    # non-digit in fraction
        "1758210000,000199",    # wrong separator
        "1758210000.000199.000000",
    ],
)
def test_parse_ts_rejects(bad: str) -> None:
    with pytest.raises(TsFormatError):
        parse_ts(bad)


def test_format_ts_rejects_negative() -> None:
    with pytest.raises(ValueError):
        format_ts(-1)


def test_format_ts_pads_fraction() -> None:
    assert format_ts(1758210000 * US_PER_SECOND + 199) == "1758210000.000199"
    assert format_ts(0) == "0.000000"
