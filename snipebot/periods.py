"""Report period keys and the most-recent-due selection (00-data section 8).

Every digest renders from a period key and an anchor instant, never from `now`,
so a late run still reports the right period. A period key is prefixed with the
report name, then a cadence-specific tail; the anchor is a local wall-clock time
resolved to integer microseconds. No float ever touches a ts: local->UTC goes
through `timedelta`, never `datetime.timestamp()`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from snipebot.config import Cadence, ReportSpec, Semester, Weekday
from snipebot.ts import US_PER_SECOND

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_DAY = timedelta(days=1)

_WEEKDAY_INDEX = {
    Weekday.MON: 0,
    Weekday.TUE: 1,
    Weekday.WED: 2,
    Weekday.THU: 3,
    Weekday.FRI: 4,
    Weekday.SAT: 5,
    Weekday.SUN: 6,
}


@dataclass(frozen=True)
class DuePeriod:
    anchor_us: int
    period_key: str        # e.g. "daily:2026-09-18" (00-data section 8; report-name prefixed)
    semester: str


def _local_to_us(naive: datetime, tz: ZoneInfo) -> int:
    """Local wall-clock -> UTC integer microseconds, without any float.

    `fold=0`: a fall-back overlap takes the earlier (first) instant, and a
    spring-forward gap takes the earliest valid instant at or after the requested
    time (00-data section 8). The configured post times never fall in the DST
    window, so this only pins the anchor for unusual `at` values.
    """
    aware = naive.replace(tzinfo=tz)
    delta = aware.astimezone(timezone.utc) - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _local_date(ts_us: int, tz: ZoneInfo) -> date:
    return datetime.fromtimestamp(ts_us // US_PER_SECOND, tz=timezone.utc).astimezone(tz).date()


def _anchor(d: date, report: ReportSpec, tz: ZoneInfo) -> int:
    naive = datetime(d.year, d.month, d.day, report.at_hour, report.at_minute)
    return _local_to_us(naive, tz)


def _candidates(report: ReportSpec, sem: Semester, tz: ZoneInfo):
    """Yield (anchor_us, period_key) for every period this report emits in `sem`."""
    start = _local_date(sem.start_us, tz)
    end = _local_date(sem.end_us, tz)

    if report.cadence is Cadence.DAILY:
        d = start
        while d <= end:
            yield _anchor(d, report, tz), f"{report.name}:{d.isoformat()}"
            d += _DAY

    elif report.cadence is Cadence.WEEKLY:
        target = _WEEKDAY_INDEX[report.weekday]
        d = start
        while d <= end:
            # A weekly period is emitted only if its anchor weekday falls within the
            # semester; the trailing partial week is covered by `final`, not here.
            if d.weekday() == target:
                iso = d.isocalendar()
                yield _anchor(d, report, tz), f"{report.name}:{iso[0]:04d}-W{iso[1]:02d}"
            d += _DAY

    elif report.cadence is Cadence.FINAL:
        # The day AFTER the semester end, at the report's post time.
        yield _anchor(end + _DAY, report, tz), f"{report.name}:{sem.name}"


def most_recent_due(
    report: ReportSpec,
    now_us: int,
    tz: ZoneInfo,
    semesters: Sequence[Semester],
) -> "DuePeriod | None":
    """The most recent period whose anchor instant <= now_us, with its period key and
    the semester it covers, computed per 00-data section 8. None if no period is due
    (before the first anchor)."""
    best: DuePeriod | None = None
    for sem in semesters:
        for anchor_us, period_key in _candidates(report, sem, tz):
            if anchor_us <= now_us and (best is None or anchor_us > best.anchor_us):
                best = DuePeriod(anchor_us=anchor_us, period_key=period_key, semester=sem.name)
    return best
