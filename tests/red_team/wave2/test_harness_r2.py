"""Red-team wave 2 -- attacks on the *test harnesses* (module = tests).

Every test here asserts the behaviour the specification requires of the harness and
FAILS on the harness as shipped, proving a defect in the harness (not in production code).
Targets: tests/oracle/replay.py and tests/test_crash_matrix.py.
"""
from __future__ import annotations

import calendar

from snipebot.ts import parse_ts

from tests.oracle import replay


def _mk(h: int, mi: int, s: int = 0) -> str:
    """A Slack ts string on 2026-09-18 (UTC), micros pinned so parse_ts is exact."""
    return f"{calendar.timegm((2026, 9, 18, h, mi, s, 0, 0, 0))}.000001"


def _timeline() -> replay.Timeline:
    """The same shape the shipped harness uses: two counted snipes minutes apart on one
    day (cf. tests/oracle/test_schedule_replay._placeholder_timeline). The generators under
    attack are pure functions of the event list, so config is irrelevant here."""
    events = (
        replay.AuthoringEvent(at=_mk(12, 0), kind="post", payload={}),
        replay.AuthoringEvent(at=_mk(12, 5), kind="post", payload={}),
    )
    return replay.Timeline(
        config=None,  # type: ignore[arg-type]  # generators never touch config
        events=events,
        channels=("C0MAIN01",),
        horizon_days=90,
    )


def test_span_tail_is_scan_days_not_seven_hours():
    """50 section 4.2: "Each generator spans `[first_event, last_event + scan_days]` so
    every change is observable at least once."

    `replay._span_us` caps the tail at `OBSERVE_TAIL_US` (7 h) rather than `scan_days`
    (14 days), so the observation window past the last event is ~48x too short. This makes
    the 50 section 4.3 out-of-bound row-1 case -- "a change lands day 10-14 after its
    message ... honoured iff some scheduled sync fell in `[change, day 14]`" -- unreachable:
    no schedule ever ticks more than 7 h after the last event.
    """
    tl = _timeline()
    _first, end = replay._span_us(tl)
    last = parse_ts(tl.events[-1].at)
    assert end - last == replay.SCAN_DAYS * replay.DAY_US, (
        f"observation tail is {(end - last) / replay.HOUR_US:.2f} h, "
        f"spec requires {replay.SCAN_DAYS} days"
    )


def test_three_day_outage_injects_a_real_three_day_gap():
    """50 section 4.2: `three_day_outage(tl, at)` produces "hourly, but no tick for a 3-day
    window (a gap at the section 2 bound edge)."

    A genuine *gap* requires scheduled ticks on BOTH sides of the excised 3-day window --
    that is the whole point of 50 section 4.3 row 3 ("a successful-sync gap > 3 days spans a
    change ... asserted to still pull the change in on the next sync"). Because `_span_us`
    caps the span at 7 h, excising a 3-day window from the harness's own timeline removes
    every tick from the outage onward, leaving a degenerate truncation with no >=3-day gap
    (and no post-outage recovery tick to assert against). The recovery-watermark case can
    never be exercised.
    """
    tl = _timeline()
    at = _mk(12, 2)  # outage starts just after the first event; a tick at 12:00 survives
    schedule = replay.three_day_outage(tl, at)
    ticks_us = [parse_ts(t) for t in schedule]
    max_gap = max((b - a for a, b in zip(ticks_us, ticks_us[1:])), default=0)
    assert max_gap >= 3 * replay.DAY_US, (
        f"three_day_outage produced {len(schedule)} tick(s) with a max gap of "
        f"{max_gap / replay.DAY_US:.2f} days -- no real 3-day outage was injected"
    )
