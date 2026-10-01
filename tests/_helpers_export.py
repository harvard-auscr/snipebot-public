"""Shared fixture builders for test_export.py and test_report_tables.py.

Candidate ledgers are hand-built and run through the single boundary
`eligible_snipes`, same discipline as test_aggregate.py. Kept private to this
writer's two test modules so a shared-fixture change never risks another
writer's file.
"""

from __future__ import annotations

import calendar
from zoneinfo import ZoneInfo

from snipebot.aggregate import Eligibility, eligible_snipes
from snipebot.config import (
    CooldownRule,
    DatedRules,
    MultiTag,
    ResolvedRule,
    Roster,
    RosterEntry,
    Scope,
    Semester,
)
from snipebot.parse import Candidate, SelfieOverride, VetoSource
from snipebot.report import NameResolver
from snipebot.ts import US_PER_MINUTE, US_PER_SECOND

TZ = ZoneInfo("UTC")


def secs(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def mkts(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0, micro: int = 0) -> str:
    return f"{secs(y, mo, d, h, mi, s)}.{micro:06d}"


SEM_F26 = Semester(name="F26", start_us=secs(2026, 1, 1) * US_PER_SECOND,
                   end_us=secs(2027, 1, 1) * US_PER_SECOND - 1)
SEMS = (SEM_F26,)


def cand(ts_str: str, sender: str, targets, **over) -> Candidate:
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
    """A sib-tagged message with a durable SELFIE override, so it classifies
    SELFIE without any face facts (matches test_aggregate.py's helper)."""
    return cand(
        ts_str,
        sender,
        targets,
        selfie_override=SelfieOverride(value=True, by="ADMIN", source=VetoSource.CLI),
    )


def rule(*, selfie_bonus: bool = True, cooldown_min: int = 15) -> ResolvedRule:
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


DR_SELFIE = DatedRules(entries=(rule(selfie_bonus=True),))

# groups: sibA = a1,a2,a3 ; sibB = b1,b2 ; extras (ungrouped) = x1
GROUPS = {
    "a1": "sibA", "a2": "sibA", "a3": "sibA",
    "b1": "sibB", "b2": "sibB",
    "x1": None,
}


def make_roster(count_intra_group: bool = True, extra=None) -> Roster:
    members = dict(GROUPS)
    if extra:
        members.update(extra)
    entries = {
        u: RosterEntry(user=u, join_us=0, group=g, is_bot=False)
        for u, g in members.items()
    }
    return Roster(entries=entries, count_intra_group=count_intra_group)


ROS = make_roster(True)

# Display names for the roster IDs above, plus one deliberate collision
# ("Sam" shared by b1 and b2, different groups) to exercise disambiguation.
NAME_CACHE = {
    "a1": "Alex",
    "a2": "Avery",
    "a3": "Ash",
    "b1": "Sam",
    "b2": "Sam",
    "x1": "Xan",
}


def make_resolver(roster: Roster = ROS, cache=None) -> NameResolver:
    return NameResolver(cache if cache is not None else NAME_CACHE, roster)


def elig_of(ledger, *, rules=None, roster=None, opted=frozenset(),
            semester=SEM_F26, semesters=SEMS) -> Eligibility:
    return eligible_snipes(
        ledger,
        rules or DR_SELFIE,
        roster or ROS,
        opted,
        semesters,
        TZ,
        semester,
    )
