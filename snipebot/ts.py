"""Slack timestamp parsing and formatting.

This is the only place a Slack ts is parsed. A ts is carried as integer
microseconds since the Unix epoch (`Ts`), never a float: string comparison of
raw ts values misorders as soon as the seconds field grows a digit, so all
ordering and cooldown arithmetic runs on parsed `Ts` values.
"""

from __future__ import annotations

import re

Ts = int  # integer microseconds since the Unix epoch. NOT a float, ever.

US_PER_SECOND: int = 1_000_000
US_PER_MINUTE: int = 60_000_000

# `<digits>.<exactly six digits>`, ASCII only: a stray unicode digit that int()
# would otherwise accept is rejected loudly instead of silently miscomparing.
_TS_RE = re.compile(r"^(?:0|[1-9]\d*)\.\d{6}\Z", re.ASCII)   # no leading zero, so format_ts round-trips


class TsFormatError(ValueError):
    """Raised when a string is not a well-formed Slack ts."""


def parse_ts(s: str) -> Ts:
    """Slack ts string -> integer microseconds.

    Accepts exactly ``<digits>.<6 digits>`` (regex ``^\\d+\\.\\d{6}\\Z``). Splits on
    '.' and does integer arithmetic: seconds * 1_000_000 + fractional. Never
    calls float(). Raises TsFormatError on any other shape (missing dot, wrong
    fraction width, sign, exponent, whitespace, empty).
    """
    if not isinstance(s, str) or _TS_RE.match(s) is None:
        raise TsFormatError(f"not a Slack ts: {s!r}")
    seconds, fraction = s.split(".")
    return int(seconds) * US_PER_SECOND + int(fraction)


def format_ts(t: Ts) -> str:
    """Integer microseconds -> Slack ts string, inverse of parse_ts.

    Emits exactly six fractional digits. Raises ValueError if t < 0.
    """
    if t < 0:
        raise ValueError(f"ts microseconds must be non-negative: {t!r}")
    return f"{t // US_PER_SECOND}.{t % US_PER_SECOND:06d}"
