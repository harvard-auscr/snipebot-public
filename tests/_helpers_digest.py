"""Shared builders for the digest golden and period tests.

Every scenario builds its ledger by hand from `cand` / `selfie`, runs it through the
real `eligible_snipes` boundary, and renders with `render_digest`, so the goldens
exercise the whole path (evaluate -> eligibility -> block assembly), not a stub.
"""

from __future__ import annotations

import calendar
import json
import os
from pathlib import Path

from zoneinfo import ZoneInfo

from snipebot.config import (
    Cadence,
    CooldownRule,
    DatedRules,
    MultiTag,
    ReportSpec,
    ResolvedRule,
    Roster,
    RosterEntry,
    Scope,
    Section,
    Semester,
)
from snipebot.parse import Candidate, SelfieOverride, VetoSource
from snipebot.ts import US_PER_MINUTE, US_PER_SECOND

TZ = ZoneInfo("UTC")

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "blocks"

# A fall semester: 2026-09-01 .. 2026-12-18 local.
SEMESTER = Semester(
    name="fall-2026",
    start_us=calendar.timegm((2026, 9, 1, 0, 0, 0, 0, 0, 0)) * US_PER_SECOND,
    end_us=calendar.timegm((2026, 12, 18, 23, 59, 59, 0, 0, 0)) * US_PER_SECOND + 999_999,
)


def secs(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def mkts(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0, micro: int = 0) -> str:
    return f"{secs(y, mo, d, h, mi, s)}.{micro:06d}"


def cand(ts_str: str, sender: str, targets, **over) -> Candidate:
    """A minimal, otherwise-countable candidate; override any field by name."""
    base = dict(
        ts=ts_str,
        sender=sender,
        subtype=None,
        thread_ts=None,
        targets=tuple(targets),
        live_images=1,
        live_image_ids=(),
        live_videos=0,
        linked_images=0,
        last_edit_ts=None,
        file_sigs=(),
        vetoes=(),
        missing_runs=0,
        first_seen_targets=frozenset(targets),
        first_sight_edited=False,
        target_edited_in=(),
    )
    base.update(over)
    return Candidate(**base)


def selfie(ts_str: str, sender: str, targets) -> Candidate:
    """A sib-tagged message with a durable SELFIE override, so it classifies SELFIE
    without any face facts."""
    return cand(
        ts_str,
        sender,
        targets,
        selfie_override=SelfieOverride(value=True, by="ADMIN", source=VetoSource.CLI),
    )


def rule(*, selfie_bonus: bool = False, cooldown_min: int = 15) -> ResolvedRule:
    return ResolvedRule(
        effective_from_us=0,
        cooldown=CooldownRule(
            microseconds=cooldown_min * US_PER_MINUTE,
            scope=Scope.PAIR,
            rejected_attempts_reset=False,
        ),
        multi_tag=MultiTag.PER_TARGET,
        max_targets_per_message=None,
        edit_grace_us=10 * US_PER_MINUTE,
        max_snipes_per_target_per_day=None,
        allow_self=False,
        allow_bots=False,
        count_thread_replies=False,
        count_image_links=False,
        allow_video=False,
        selfie_bonus=selfie_bonus,
    )


def dated(*rs: ResolvedRule) -> DatedRules:
    return DatedRules(entries=tuple(sorted(rs, key=lambda r: r.effective_from_us)))


def roster_of(groups: dict[str, str | None], *, count_intra_group: bool = True) -> Roster:
    entries = {
        u: RosterEntry(user=u, join_us=0, group=g, is_bot=False)
        for u, g in groups.items()
    }
    return Roster(entries=entries, count_intra_group=count_intra_group)


def report_of(sections, *, name: str = "daily", cadence: Cadence = Cadence.DAILY,
              at_hour: int = 21, at_minute: int = 0, weekday=None,
              post_to=None, top_n: int = 5) -> ReportSpec:
    return ReportSpec(
        name=name,
        cadence=cadence,
        at_hour=at_hour,
        at_minute=at_minute,
        weekday=weekday,
        post_to=post_to,
        sections=tuple(sections),
        top_n=top_n,
    )


ALL_SECTIONS = (
    Section.DAY,
    Section.TOP_SNIPERS,
    Section.MOST_SNIPED,
    Section.GROUPS,
    Section.PAIRS,
)


def dump_blocks(blocks) -> str:
    return json.dumps(list(blocks), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def check_golden(name: str, blocks) -> None:
    """Compare the rendered block list against the stored golden, or rewrite it when
    UPDATE_GOLDEN=1. Never auto-writes on mismatch (30 section 8)."""
    rendered = dump_blocks(blocks)
    path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"missing golden {path}; regenerate with UPDATE_GOLDEN=1"
    stored = path.read_text(encoding="utf-8")
    assert rendered == stored, f"golden mismatch for {name}"


def assert_limits(blocks) -> None:
    """The Block Kit limits from 30 section 5.5; a golden must be a valid payload."""
    assert len(blocks) <= 50
    for b in blocks:
        if b["type"] == "header":
            assert len(b["text"]["text"]) <= 150
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= 3000
            fields = b.get("fields", [])
            assert len(fields) <= 10
            for f in fields:
                assert len(f["text"]) <= 2000
