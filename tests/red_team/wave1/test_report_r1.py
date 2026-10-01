"""Adversarial spec-conformance probes for ``snipebot.report`` and ``snipebot.periods``.

Each test builds inputs by hand (an ``Eligibility`` of ``EligibleSnipe`` rows, a
``Roster``, a ``ReportSpec``) and attacks one behaviour against the exact wording of
``spec/30-aggregate-report.md`` / ``spec/00-data.md``. A test here is expected to FAIL:
its docstring quotes the spec sentence the expectation rests on, and the current code
disagrees. Scaffolding mirrors ``tests/_helpers_digest.py`` so a reviewer can diff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from snipebot.aggregate import Eligibility, EligibleSnipe
from snipebot.config import Cadence, Section, Semester
from snipebot.periods import most_recent_due
from snipebot.report import DigestTooLargeError, NameResolver, render_digest

from tests._helpers_digest import SEMESTER, TZ, report_of, roster_of

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _local_us(dt: datetime) -> int:
    """A tz-aware datetime -> integer microseconds, via timedelta only (no float ts)."""
    delta = dt.astimezone(timezone.utc) - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _kwargs(report, elig, roster, cache, *, selfie_emoji="selfie", revision=0):
    return dict(
        report=report,
        period_key="daily:2026-09-18",
        semester=SEMESTER,
        elig=elig,
        roster=roster,
        opted_out=frozenset(),
        names=NameResolver(cache, roster),
        tz=TZ,
        revision=revision,
        selfie_emoji=selfie_emoji,
    )


def _top_snipers_text(render) -> str:
    for b in render.blocks:
        if b.get("type") == "section":
            t = b["text"]["text"]
            if t.startswith("*Top snipers*"):
                return t
    raise AssertionError("no top_snipers block found")


# --------------------------------------------------------------------------- #
# report.py — the character-budget collapse line must name the shared metric V
# --------------------------------------------------------------------------- #

def test_overflow_line_states_cutoff_value_when_top_n_exceeds_row_count():
    """30 section 3 case 1: "`len(rows) <= top_n` -> show all; no overflow"; the head
    (ranks 1..top_n) is always shown and only boundary-tie rows are ever collapsed
    (E-W4-20a). 30 section 5.5: a limit the head cannot meet is a DigestTooLargeError,
    never a silently truncated digest.

    ``top_n`` is only bounded below (>= 1; 40 section 2.3), so a report may set ``top_n``
    >= the number of ranked rows. Here 60 snipers all tie at 1 point with ``top_n=100``:
    every row is head, so there is no boundary tie and no collapse line. The rows do not
    fit ``MAX_SECTION_TEXT``, so the render either shows every row or raises
    DigestTooLargeError; it never hides a head row under "...and +K others tied at V".
    """
    base = 1_758_182_400  # 2026-09-18 08:00:00 UTC
    snipes = []
    cache = {"victim": ("Victim " + "x" * 40)[:40]}
    groups = {"victim": "sibV"}
    for i in range(60):
        u = f"s{i:02d}"
        groups[u] = "sibS"
        cache[u] = ("Sniper%02d " % i + "x" * 40)[:40]
        snipes.append(
            EligibleSnipe(
                ts=f"{base + i}.000000", ts_us=(base + i) * 1_000_000,
                date="2026-09-18", time="08:00:00",
                sniper=u, target="victim",
                sniper_group="sibS", target_group="sibV", selfie=False,
            )
        )
    elig = Eligibility(semester=SEMESTER, snipes=tuple(snipes), rejections=())
    roster = roster_of(groups)
    report = report_of((Section.DAY, Section.TOP_SNIPERS), top_n=100)

    try:
        render = render_digest(**_kwargs(report, elig, roster, cache))
    except DigestTooLargeError:
        return  # the spec-sanctioned outcome when the head cannot fit
    text = _top_snipers_text(render)
    assert "others tied at" not in text, (
        f"rows <= top_n, so nothing may collapse: {text.rsplit(chr(10), 1)[-1]!r}"
    )
    missing = [u for u in cache if u != "victim" and cache[u] not in text]
    assert not missing, f"{len(missing)} head rows were silently dropped"
