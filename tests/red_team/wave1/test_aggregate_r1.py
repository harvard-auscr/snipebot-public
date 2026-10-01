"""Adversarial spec-conformance probes for ``snipebot.aggregate`` (30 section 2).

Each test builds a ledger by hand, runs it through the single boundary
``eligible_snipes`` and then attacks one table builder against the exact wording
of ``spec/30-aggregate-report.md``. A test here is expected to FAIL: it encodes
what the spec sentence in its docstring requires, and the current code disagrees.

Inputs are minimal Candidate/roster/rule dataclasses (never ``parse``); the
scaffolding mirrors ``tests/test_aggregate.py`` so a reviewer can diff the two.
"""

from __future__ import annotations

import calendar
from zoneinfo import ZoneInfo

from snipebot.aggregate import build_groups_table, eligible_snipes
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
from snipebot.parse import Candidate
from snipebot.ts import US_PER_MINUTE, US_PER_SECOND

TZ = ZoneInfo("UTC")


def secs(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def mkts(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> str:
    return f"{secs(y, mo, d, h, mi, s)}.000000"


SEM_F26 = Semester(
    name="F26",
    start_us=secs(2026, 1, 1) * US_PER_SECOND,
    end_us=secs(2027, 1, 1) * US_PER_SECOND - 1,
)


def cand(ts_str: str, sender: str, targets) -> Candidate:
    return Candidate(
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


def rule() -> ResolvedRule:
    return ResolvedRule(
        effective_from_us=0,
        cooldown=CooldownRule(
            microseconds=15 * US_PER_MINUTE,
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
        selfie_bonus=False,
    )


DR = DatedRules(entries=(rule(),))


def _roster(groups) -> Roster:
    entries = {
        u: RosterEntry(user=u, join_us=0, group=g, is_bot=False)
        for u, g in groups.items()
    }
    return Roster(entries=entries, count_intra_group=True)


def test_group_all_members_opted_out_and_no_snipe_is_absent():
    """30 section 2.4, Rows: "one row per real sibling group that has any member or
    any counted snipe".

    ``members`` is defined (30 section 2.4) as "rostered players in the group **not
    in `opted_out`** at report time". A sibling group whose only rostered player has
    opted out therefore has zero members, and if no counted snipe touches it, it has
    neither "any member" nor "any counted snipe" -- so the table must not carry a row
    for it. ``build_groups_table`` emits a spurious ``members == 0`` row for such a
    group anyway.
    """
    groups = {"a1": "sibA", "b1": "sibB", "c1": "sibC"}
    ros = _roster(groups)
    opted = frozenset({"c1"})
    # Only an a1 -> b1 snipe; nothing touches sibC, and its sole member c1 opted out.
    ledger = [cand(mkts(2026, 9, 14, 10), "a1", ("b1",))]
    elig = eligible_snipes(ledger, DR, ros, opted, (SEM_F26,), TZ, SEM_F26)

    rows = build_groups_table(elig, ros, opted)
    present = {r.group for r in rows}
    assert "sibC" not in present, (
        "sibC has no member (its only rostered player opted out) and no counted "
        f"snipe, so 30 section 2.4 forbids a row for it; got groups {sorted(present)}"
    )
