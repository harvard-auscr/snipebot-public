"""Period keys and most-recent-due selection (00-data section 8; 20 section 6.1).

Every anchor is computed by hand from `calendar.timegm` (a UTC seconds count) so the
assertions never lean on the module under test to say what the right instant is. The
DST cases use a real observing zone and pin the earliest-valid-instant rule; the
configured post times never land in the DST window, but the rule is exercised anyway.
"""

from __future__ import annotations

import calendar
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from zoneinfo import ZoneInfo

from snipebot.config import Cadence, Section, Semester, Weekday
from snipebot.periods import DuePeriod, most_recent_due
from snipebot.ts import US_PER_SECOND
from tests._helpers_digest import SEMESTER, TZ, report_of, secs

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def us(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    """UTC wall-clock -> integer microseconds (no float touches a ts)."""
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) * US_PER_SECOND


def local_us(dt: datetime) -> int:
    """A tz-aware datetime -> integer microseconds, via timedelta only."""
    delta = dt.astimezone(timezone.utc) - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def ny_semester() -> Semester:
    """The fall term with its edges pinned to America/New_York local midnight/eod."""
    ny = ZoneInfo("America/New_York")
    start = local_us(datetime(2026, 9, 1, 0, 0, 0, tzinfo=ny))
    end = local_us(datetime(2026, 12, 18, 23, 59, 59, 999_999, tzinfo=ny))
    return Semester(name="fall-2026", start_us=start, end_us=end)


# --------------------------------------------------------------------------- #
# DuePeriod shape
# --------------------------------------------------------------------------- #

def test_due_period_is_frozen_with_the_three_fields():
    dp = DuePeriod(anchor_us=1, period_key="daily:2026-09-18", semester="fall-2026")
    assert (dp.anchor_us, dp.period_key, dp.semester) == (1, "daily:2026-09-18", "fall-2026")
    assert dataclasses.is_dataclass(dp)
    with pytest.raises(dataclasses.FrozenInstanceError):
        dp.anchor_us = 2  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Daily cadence (SEMESTER is UTC, so an anchor is that date at `at` UTC)
# --------------------------------------------------------------------------- #

def _daily(at_hour: int = 21, name: str = "daily"):
    return report_of((Section.DAY,), name=name, at_hour=at_hour)


def test_daily_key_anchor_and_semester_at_exact_anchor():
    report = _daily()
    now = us(2026, 9, 18, 21, 0, 0)
    due = most_recent_due(report, now, TZ, (SEMESTER,))
    assert due is not None
    assert due.period_key == "daily:2026-09-18"
    assert due.anchor_us == us(2026, 9, 18, 21, 0, 0)
    assert due.semester == "fall-2026"


def test_daily_none_before_first_anchor():
    # The first daily anchor is the semester start date at 21:00; one second earlier
    # nothing is due yet.
    report = _daily()
    assert most_recent_due(report, us(2026, 9, 1, 20, 59, 59), TZ, (SEMESTER,)) is None


def test_daily_picks_latest_anchor_at_or_before_now():
    # Mid-morning on the 18th: the 17th's 21:00 anchor is the most recent that has fired.
    report = _daily()
    due = most_recent_due(report, us(2026, 9, 18, 9, 0, 0), TZ, (SEMESTER,))
    assert due is not None
    assert due.period_key == "daily:2026-09-17"
    assert due.anchor_us == us(2026, 9, 17, 21, 0, 0)


def test_daily_after_semester_end_holds_the_last_day():
    # Past the final in-semester anchor (12-18 21:00); the last daily period stays due.
    report = _daily()
    due = most_recent_due(report, us(2026, 12, 25, 0, 0, 0), TZ, (SEMESTER,))
    assert due is not None
    assert due.period_key == "daily:2026-12-18"
    assert due.anchor_us == us(2026, 12, 18, 21, 0, 0)


def test_report_name_prefixes_the_period_key():
    # Two reports posting to the same channel must not collide on their period key.
    now = us(2026, 9, 18, 22, 0, 0)
    a = most_recent_due(_daily(name="daily"), now, TZ, (SEMESTER,))
    b = most_recent_due(_daily(name="morning"), now, TZ, (SEMESTER,))
    assert a is not None and b is not None
    assert a.period_key == "daily:2026-09-18"
    assert b.period_key == "morning:2026-09-18"


# --------------------------------------------------------------------------- #
# Weekly cadence (ISO year + ISO week; anchor is that week's weekday at `at`)
# --------------------------------------------------------------------------- #

def _weekly(weekday: Weekday = Weekday.SUN, at_hour: int = 20):
    return report_of((Section.WEEK,), name="weekly", cadence=Cadence.WEEKLY,
                     at_hour=at_hour, weekday=weekday)


def test_weekly_key_is_iso_year_and_week():
    # 2026-09-13 is a Sunday in ISO week 37.
    report = _weekly()
    due = most_recent_due(report, us(2026, 9, 13, 20, 0, 0), TZ, (SEMESTER,))
    assert due is not None
    assert due.period_key == "weekly:2026-W37"
    assert due.anchor_us == us(2026, 9, 13, 20, 0, 0)


def test_weekly_none_before_first_sunday_anchor():
    # First Sunday on/after the 09-01 start is 09-06; before its 20:00 anchor: nothing.
    report = _weekly()
    assert most_recent_due(report, us(2026, 9, 6, 19, 59, 59), TZ, (SEMESTER,)) is None


def test_weekly_holds_most_recent_sunday_midweek():
    # Wednesday the 16th: the most recent fired anchor is Sunday the 13th (week 37).
    report = _weekly()
    due = most_recent_due(report, us(2026, 9, 16, 12, 0, 0), TZ, (SEMESTER,))
    assert due is not None
    assert due.period_key == "weekly:2026-W37"
    assert due.anchor_us == us(2026, 9, 13, 20, 0, 0)


# --------------------------------------------------------------------------- #
# Final cadence (the day AFTER semester end, keyed by semester name)
# --------------------------------------------------------------------------- #

def test_final_anchor_is_the_day_after_end():
    report = report_of((Section.SEMESTER,), name="final", cadence=Cadence.FINAL, at_hour=12)
    # Semester ends 2026-12-18; the final anchor is 2026-12-19 at 12:00.
    due = most_recent_due(report, us(2026, 12, 19, 12, 0, 0), TZ, (SEMESTER,))
    assert due is not None
    assert due.period_key == "final:fall-2026"
    assert due.anchor_us == us(2026, 12, 19, 12, 0, 0)
    assert due.semester == "fall-2026"


def test_final_none_before_its_anchor():
    report = report_of((Section.SEMESTER,), name="final", cadence=Cadence.FINAL, at_hour=12)
    assert most_recent_due(report, us(2026, 12, 19, 11, 59, 59), TZ, (SEMESTER,)) is None


# --------------------------------------------------------------------------- #
# Multiple semesters
# --------------------------------------------------------------------------- #

def _spring() -> Semester:
    return Semester(
        name="spring-2027",
        start_us=us(2027, 1, 20, 0, 0, 0),
        end_us=us(2027, 5, 15, 23, 59, 59) + 999_999,
    )


def test_selection_spans_semesters_and_reports_the_covering_one():
    report = _daily()
    sems = (SEMESTER, _spring())
    # A February instant lands in spring; the covering semester name follows.
    due = most_recent_due(report, us(2027, 2, 10, 22, 0, 0), TZ, sems)
    assert due is not None
    assert due.period_key == "daily:2027-02-10"
    assert due.semester == "spring-2027"


def test_between_semesters_holds_the_prior_semesters_last_day():
    report = _daily()
    sems = (SEMESTER, _spring())
    # Winter break: after fall's last anchor, before spring's first — the last fall day.
    due = most_recent_due(report, us(2027, 1, 5, 0, 0, 0), TZ, sems)
    assert due is not None
    assert due.period_key == "daily:2026-12-18"
    assert due.semester == "fall-2026"


# --------------------------------------------------------------------------- #
# DST: anchors resolve local wall-clock to instant per 00-data section 8
# --------------------------------------------------------------------------- #

def test_dst_ordinary_evening_anchor_uses_the_right_offset():
    # A daily report at 21:00 America/New_York on 2026-09-18 (EDT, -04:00) is 01:00 UTC
    # the next calendar day; day bucketing stays on the local date.
    ny = ZoneInfo("America/New_York")
    report = _daily(at_hour=21)
    now = us(2026, 9, 19, 2, 0, 0)  # just after the anchor, in UTC
    due = most_recent_due(report, now, ny, (ny_semester(),))
    assert due is not None
    assert due.period_key == "daily:2026-09-18"
    assert due.anchor_us == us(2026, 9, 19, 1, 0, 0)


def test_dst_fall_back_overlap_takes_the_earlier_instant():
    # 2026-11-01 (Sunday, ISO week 44) is the fall-back day. A weekly report anchored at
    # 01:00 falls inside the 01:00-02:00 overlap; fold=0 takes the earlier EDT instant
    # (01:00 -04:00 = 05:00 UTC), not the later EST one (06:00 UTC).
    ny = ZoneInfo("America/New_York")
    report = _weekly(weekday=Weekday.SUN, at_hour=1)
    earlier = us(2026, 11, 1, 5, 0, 0)
    later = us(2026, 11, 1, 6, 0, 0)
    due = most_recent_due(report, later, ny, (ny_semester(),))
    assert due is not None
    assert due.period_key == "weekly:2026-W44"
    assert due.anchor_us == earlier
    # Confirm the earlier instant is genuinely what fold=0 yields for that wall-clock.
    assert earlier == local_us(datetime(2026, 11, 1, 1, 0, 0, tzinfo=ny))
