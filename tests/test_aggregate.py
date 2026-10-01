"""Unit tests for the aggregation layer (30-aggregate-report.md sections 1-3).

Candidate ledgers are built by hand (never via `parse`) and run through the single
boundary `eligible_snipes`; the table builders are then exercised for shape,
ordering, the two conservation identities in both units, the points columns, the
ranking tiebreaks and the `top_n` cutoff. Times are Slack ts strings; the module
under test does integer-microsecond arithmetic only.

Covers the test-matrix rows CTL-tables / L2-PR-conservation / L2-PR-points-
conservation / L2-PR-optout-closure / L2-PR-repost-hash / L2-PR-semester-coverage
as they land in `test_aggregate.py`.
"""

from __future__ import annotations

import calendar
from zoneinfo import ZoneInfo

from snipebot.aggregate import (
    UNGROUPED,
    Eligibility,
    build_daily_table,
    build_groups_table,
    build_most_sniped_table,
    build_pairs_table,
    build_people_table,
    build_snipes_table,
    eligible_snipes,
    rank_most_sniped,
    rank_pairs,
    rank_top_snipers,
    top_n_cutoff,
)
from snipebot.config import (
    CooldownRule,
    DatedRules,
    ResolvedRule,
    Roster,
    RosterEntry,
    Scope,
    MultiTag,
    Semester,
)
from snipebot.parse import Candidate, SelfieOverride, VetoSource
from snipebot.ts import US_PER_MINUTE, US_PER_SECOND

TZ = ZoneInfo("UTC")


def secs(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def mkts(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0, micro: int = 0) -> str:
    return f"{secs(y, mo, d, h, mi, s)}.{micro:06d}"


SEM_F26 = Semester(name="F26", start_us=secs(2026, 1, 1) * US_PER_SECOND,
                   end_us=secs(2027, 1, 1) * US_PER_SECOND - 1)
SEM_S27 = Semester(name="S27", start_us=secs(2027, 1, 1) * US_PER_SECOND,
                   end_us=secs(2028, 1, 1) * US_PER_SECOND - 1)
SEMS = (SEM_F26, SEM_S27)


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


# groups: sibA = a1,a2,a3 ; sibB = b1,b2 ; extras (ungrouped) = x1,x2
GROUPS = {
    "a1": "sibA", "a2": "sibA", "a3": "sibA",
    "b1": "sibB", "b2": "sibB",
    "x1": None, "x2": None,
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
DR_PLAIN = dated(rule())
DR_SELFIE = dated(rule(selfie_bonus=True))


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


# --------------------------------------------------------------------------- #
# eligible_snipes shape / derivations
# --------------------------------------------------------------------------- #

def test_eligible_snipes_derives_date_time_and_groups():
    e = elig_of([cand(mkts(2026, 9, 14, 10, 30, 5), "a1", ("b1",), )],
                rules=DR_PLAIN)
    assert len(e.snipes) == 1
    s = e.snipes[0]
    assert s.date == "2026-09-14"
    assert s.time == "10:30:05"
    assert s.sniper == "a1" and s.target == "b1"
    assert s.sniper_group == "sibA" and s.target_group == "sibB"
    assert s.selfie is False
    assert e.rejections == ()


def test_cooldown_pair_lands_in_rejections_not_snipes():
    # same pair 5 minutes apart, cooldown 15 minutes -> second is COOLDOWN
    ledger = [
        cand(mkts(2026, 9, 14, 10, 0, 0), "a1", ("b1",)),
        cand(mkts(2026, 9, 14, 10, 5, 0), "a1", ("b1",)),
    ]
    e = elig_of(ledger, rules=DR_PLAIN)
    assert len(e.snipes) == 1
    assert len(e.rejections) == 1
    r = e.rejections[0]
    assert r.date == "2026-09-14"
    assert r.sniper == "a1" and r.target == "b1"
    assert r.blocked_by == mkts(2026, 9, 14, 10, 0, 0)


# --------------------------------------------------------------------------- #
# 2.1 snipes table: one row per snipe, chronological
# --------------------------------------------------------------------------- #

def test_snipes_table_chronological_and_keyed():
    ledger = [
        cand(mkts(2026, 9, 14, 10), "a1", ("b1",)),
        cand(mkts(2026, 9, 15, 11), "b1", ("a1",)),
        cand(mkts(2026, 9, 14, 9), "x1", ("a2",)),
    ]
    rows = build_snipes_table(elig_of(ledger, rules=DR_PLAIN), ROS)
    assert [r.ts_us for r in rows] == sorted(r.ts_us for r in rows)
    assert (rows[0].sniper, rows[0].target) == ("x1", "a2")
    assert rows[0].sniper_group == UNGROUPED and rows[0].target_group == "sibA"
    assert [r.date for r in rows] == ["2026-09-14", "2026-09-14", "2026-09-15"]


# --------------------------------------------------------------------------- #
# 2.2 daily table
# --------------------------------------------------------------------------- #

def test_daily_table_buckets_and_columns():
    ledger = [
        cand(mkts(2026, 9, 14, 10), "a1", ("b1",)),
        cand(mkts(2026, 9, 14, 12), "x1", ("a2",)),
        cand(mkts(2026, 9, 14, 10, 5), "a1", ("b1",)),   # cooldown rejection, same day
        cand(mkts(2026, 9, 15, 11), "b1", ("a1",)),
    ]
    rows = build_daily_table(elig_of(ledger, rules=DR_PLAIN))
    assert [r.date for r in rows] == ["2026-09-14", "2026-09-15"]
    d14, d15 = rows
    assert d14.snipes == 2
    assert d14.unique_snipers == 2
    assert d14.unique_targets == 2
    assert d14.cooldown_rejections == 1
    assert d14.points == 2                     # two plain snipe points
    assert (d15.snipes, d15.cooldown_rejections, d15.points) == (1, 0, 1)


# --------------------------------------------------------------------------- #
# 2.3 people table: ordering and best_day
# --------------------------------------------------------------------------- #

def test_people_table_order_and_best_day():
    ledger = [
        cand(mkts(2026, 9, 14, 10), "a1", ("b1",)),
        cand(mkts(2026, 9, 15, 10), "a1", ("b2",)),
        cand(mkts(2026, 9, 15, 12), "a1", ("x1",)),
        cand(mkts(2026, 9, 16, 10), "b1", ("a2",)),
    ]
    rows = build_people_table(elig_of(ledger, rules=DR_PLAIN), ROS)
    by_person = {r.person: r for r in rows}
    # a1: 3 made; peaks on 2026-09-15 (two that day)
    assert rows[0].person == "a1"
    assert by_person["a1"].snipes_made == 3
    assert by_person["a1"].best_day == "2026-09-15"
    assert by_person["a1"].unique_targets == 3
    # a pure target has best_day None
    assert by_person["b2"].snipes_made == 0
    assert by_person["b2"].best_day is None
    assert by_person["b2"].times_sniped == 1


def test_people_table_order_tiebreak_first_snipe_then_id():
    # two people each made exactly one snipe; earliest snipe breaks the tie
    ledger = [
        cand(mkts(2026, 9, 14, 9), "b1", ("x1",)),
        cand(mkts(2026, 9, 14, 10), "a1", ("x2",)),
    ]
    rows = build_people_table(elig_of(ledger, rules=DR_PLAIN), ROS)
    made = [r.person for r in rows if r.snipes_made == 1]
    assert made == ["b1", "a1"]      # b1 sniped earlier, so ranks first


# --------------------------------------------------------------------------- #
# 2.4 groups table: ordering, UNGROUPED last, per-member
# --------------------------------------------------------------------------- #

def test_groups_order_by_points_per_member_then_ungrouped_last():
    # sibA (2 members) earns 1 point; sibB (1 member) earns 0; x* ungrouped touched
    ledger = [
        cand(mkts(2026, 9, 14, 10), "a1", ("b1",)),   # a1 (sibA) plain -> sibA 1 pt
        cand(mkts(2026, 9, 14, 12), "x1", ("x2",)),   # ungrouped intra
    ]
    ros = make_roster(True)
    rows = build_groups_table(elig_of(ledger, rules=DR_PLAIN), ros, frozenset())
    keys = [r.group for r in rows]
    assert keys[-1] == UNGROUPED                      # ungrouped always last
    assert keys.index("sibA") < keys.index("sibB")    # 0.5/member beats 0/member
    sibA = next(r for r in rows if r.group == "sibA")
    assert sibA.members == 3 and sibA.points == 1
    assert sibA.points_per_member() == "0.33"


def test_groups_name_tiebreak_when_metrics_equal():
    # both sibA and sibB only receive snipes (made 0, points 0); order by name
    ledger = [
        cand(mkts(2026, 9, 14, 10), "x1", ("a1",)),
        cand(mkts(2026, 9, 14, 11), "x2", ("b1",)),
    ]
    rows = build_groups_table(elig_of(ledger, rules=DR_PLAIN), ROS, frozenset())
    real = [r.group for r in rows if r.group != UNGROUPED]
    assert real == ["sibA", "sibB"]                   # alphabetical on the tie


def test_groups_members_excludes_opted_out():
    ledger = [cand(mkts(2026, 9, 14, 10), "a1", ("b2",))]
    rows = build_groups_table(elig_of(ledger, rules=DR_PLAIN, opted=frozenset({"b1"})),
                              ROS, frozenset({"b1"}))
    sibB = next(r for r in rows if r.group == "sibB")
    assert sibB.members == 1                          # b1 dropped, only b2 counts


# --------------------------------------------------------------------------- #
# 2.5 most_sniped: competition ranking and top sniper
# --------------------------------------------------------------------------- #

def test_most_sniped_competition_rank_and_top_sniper():
    people = {u: None for u in ("t", "u", "v", "w", "p1", "p2", "p3")}
    ros = make_roster(True, extra=people)
    ledger = [
        # t sniped 3 times (p1 earliest -> top sniper); u and v twice; w once
        cand(mkts(2026, 9, 14, 8), "p1", ("t",)),
        cand(mkts(2026, 9, 14, 9), "p2", ("t",)),
        cand(mkts(2026, 9, 14, 10), "p3", ("t",)),
        cand(mkts(2026, 9, 14, 8, 30), "p1", ("u",)),
        cand(mkts(2026, 9, 14, 9, 30), "p2", ("u",)),
        cand(mkts(2026, 9, 14, 11), "p1", ("v",)),
        cand(mkts(2026, 9, 14, 12), "p2", ("v",)),
        cand(mkts(2026, 9, 14, 13), "p1", ("w",)),
    ]
    rows = build_most_sniped_table(elig_of(ledger, rules=DR_PLAIN, roster=ros), ros)
    seq = [(r.person, r.rank, r.times_sniped) for r in rows if r.person in
           {"t", "u", "v", "w"}]
    assert seq == [("t", 1, 3), ("u", 2, 2), ("v", 2, 2), ("w", 4, 1)]
    top_of_t = next(r for r in rows if r.person == "t")
    assert top_of_t.top_sniper_of_them == "p1"


# --------------------------------------------------------------------------- #
# 2.6 pairs: points == count invariant, ordering
# --------------------------------------------------------------------------- #

def test_pairs_points_equals_count_and_order():
    ledger = [
        selfie(mkts(2026, 9, 14, 10), "a1", ("a2",)),   # intra selfie, pair a1->a2
        cand(mkts(2026, 9, 16, 10), "a1", ("a2",)),     # plain, same pair, later day
        cand(mkts(2026, 9, 15, 10), "b1", ("x1",)),     # unrelated pair
    ]
    rows = build_pairs_table(elig_of(ledger))
    for r in rows:
        assert r.points == r.count           # the L2 invariant, every pair
    top = rows[0]
    assert (top.sniper, top.target, top.count) == ("a1", "a2", 2)


# --------------------------------------------------------------------------- #
# Conservation identities (both units) on the owner's worked examples
# --------------------------------------------------------------------------- #

def _assert_conservation(e: Eligibility, roster: Roster):
    N = len(e.snipes)
    daily = build_daily_table(e)
    people = build_people_table(e, roster)
    groups = build_groups_table(e, roster, frozenset())
    pairs = build_pairs_table(e)

    assert N == sum(d.snipes for d in daily)
    assert N == sum(p.snipes_made for p in people)
    assert N == sum(p.times_sniped for p in people)
    assert N == sum(pr.count for pr in pairs)

    P = sum(1 for s in e.snipes if not s.selfie)
    F = len({s.ts for s in e.snipes if s.selfie})
    Q = sum(1 for s in e.snipes if s.selfie)
    assert P + Q == N
    total = P + F + Q

    assert sum(p.points for p in people) == total
    assert sum(g.points for g in groups) == total
    assert sum(pr.points for pr in pairs) + F == total
    assert sum(d.points for d in daily) == total

    # every person in exactly one group row
    covered = sum(1 for p in people)
    seen = set()
    for p in people:
        assert p.person not in seen
        seen.add(p.person)
    assert covered == len(seen)

    I = sum(g.intra_group for g in groups)
    if roster.count_intra_group:
        assert sum(g.made for g in groups) == N
        assert sum(g.sniped for g in groups) == N
        for g in groups:
            assert g.intra_group + g.cross_group == g.made
    else:
        assert sum(g.made for g in groups) == N - I
        assert sum(g.sniped for g in groups) == N - I
        for g in groups:
            assert g.cross_group == g.made


def test_conservation_two_sib_selfie_equals_two():
    # a1 posts a selfie tagging sib a2: P=0, F=1, Q=1 -> sibfam total 2
    e = elig_of([selfie(mkts(2026, 9, 14, 10), "a1", ("a2",))])
    _assert_conservation(e, ROS)
    groups = build_groups_table(e, ROS, frozenset())
    sibA = next(g for g in groups if g.group == "sibA")
    assert sibA.points == 2
    people = {p.person: p for p in build_people_table(e, ROS)}
    assert people["a1"].points == 1 and people["a2"].points == 1
    pairs = build_pairs_table(e)
    assert sum(pr.points for pr in pairs) == 1     # + F(1) == 2


def test_conservation_three_way_selfie_equals_three():
    # a1 posts one photo tagging sibs a2 and a3: P=0, F=1, Q=2 -> total 3, 1 each
    e = elig_of([selfie(mkts(2026, 9, 14, 10), "a1", ("a2", "a3"))])
    _assert_conservation(e, ROS)
    people = {p.person: p for p in build_people_table(e, ROS)}
    assert people["a1"].points == 1
    assert people["a2"].points == 1
    assert people["a3"].points == 1
    groups = build_groups_table(e, ROS, frozenset())
    sibA = next(g for g in groups if g.group == "sibA")
    assert sibA.points == 3
    pairs = build_pairs_table(e)
    assert sum(pr.points for pr in pairs) == 2     # (a1->a2)=1,(a1->a3)=1; + F(1) == 3


def _mixed_ledger():
    return [
        cand(mkts(2026, 9, 14, 10), "a1", ("b1",)),          # cross plain
        cand(mkts(2026, 9, 14, 11), "b1", ("a1",)),          # cross plain
        selfie(mkts(2026, 9, 15, 10), "a1", ("a2",)),        # intra selfie
        selfie(mkts(2026, 9, 16, 10), "a1", ("a2", "a3")),   # intra three-way selfie
        cand(mkts(2026, 9, 14, 12), "x1", ("x2",)),          # ungrouped intra plain
        cand(mkts(2026, 9, 17, 10), "x1", ("a1",)),          # cross plain
        cand(mkts(2026, 9, 17, 11), "a2", ("b2",)),          # cross plain
    ]


def test_conservation_mixed_intra_true():
    _assert_conservation(elig_of(_mixed_ledger()), make_roster(True))


def test_conservation_mixed_intra_false_keeps_selfie_points():
    e = elig_of(_mixed_ledger())
    ros_no = make_roster(False)
    _assert_conservation(e, ros_no)
    # selfie points still reach groups.points even though intra raw made drops
    groups = build_groups_table(e, ros_no, frozenset())
    sibA = next(g for g in groups if g.group == "sibA")
    assert sibA.points == 7            # unchanged from the intra-true total
    assert sibA.made == sibA.cross_group


# --------------------------------------------------------------------------- #
# Semester filter
# --------------------------------------------------------------------------- #

def test_semester_filter_keeps_only_requested_semester():
    ledger = [
        cand(mkts(2026, 9, 14, 10), "a1", ("b1",)),   # F26
        cand(mkts(2027, 3, 10, 10), "a1", ("b1",)),   # S27
    ]
    f26 = elig_of(ledger, rules=DR_PLAIN, semester=SEM_F26)
    assert [s.date for s in f26.snipes] == ["2026-09-14"]
    s27 = elig_of(ledger, rules=DR_PLAIN, semester=SEM_S27)
    assert [s.date for s in s27.snipes] == ["2027-03-10"]


# --------------------------------------------------------------------------- #
# Opt-out exclusion (both as sniper and as target)
# --------------------------------------------------------------------------- #

def test_optout_excludes_person_everywhere():
    ledger = [
        cand(mkts(2026, 9, 14, 10), "a1", ("b1",)),   # target b1 opted out -> dropped
        cand(mkts(2026, 9, 14, 11), "b1", ("a2",)),   # sender b1 opted out -> dropped
        cand(mkts(2026, 9, 14, 12), "a1", ("a2",)),   # clean, survives
    ]
    e = elig_of(ledger, rules=DR_PLAIN, opted=frozenset({"b1"}))
    for s in e.snipes:
        assert s.sniper != "b1" and s.target != "b1"
    people = {p.person for p in build_people_table(e, ROS)}
    assert "b1" not in people


# --------------------------------------------------------------------------- #
# REPOST exclusion (single boundary, no table re-checks)
# --------------------------------------------------------------------------- #

def test_repost_excluded_by_boundary():
    first = selfie(mkts(2026, 9, 14, 10), "a1", ("a2",))
    first = Candidate(**{**first.__dict__, "rendition_hash": {"f1": "HASH"}})
    # same rendition bytes, a sib-tagged re-upload beyond the cooldown window
    repost = cand(mkts(2026, 9, 14, 11), "a1", ("a2",),
                  rendition_hash={"f2": "HASH"})
    e = elig_of([first, repost])
    assert len(e.snipes) == 1                         # only the original upload
    assert e.snipes[0].ts == first.ts
    pairs = build_pairs_table(e)
    assert next(p for p in pairs if p.sniper == "a1").count == 1


# --------------------------------------------------------------------------- #
# 3. Ranking helpers and top_n cutoff
# --------------------------------------------------------------------------- #

def test_top_snipers_rank_by_points_then_made():
    # a1: 1 selfie photo point (1 made); x1: 2 plain points (2 made)
    ledger = [
        selfie(mkts(2026, 9, 14, 10), "a1", ("a2",)),
        cand(mkts(2026, 9, 15, 10), "x1", ("b1",)),
        cand(mkts(2026, 9, 16, 10), "x1", ("b2",)),
    ]
    people = build_people_table(elig_of(ledger), ROS)
    ranked = rank_top_snipers(people)
    top_two = [r.person for r in ranked[:2]]
    assert top_two[0] == "x1"      # 2 points > 1 point
    assert "a1" in top_two


def test_rank_pairs_and_most_sniped_are_pure_orders():
    ledger = [
        cand(mkts(2026, 9, 14, 8), "p1", ("t",)),
        cand(mkts(2026, 9, 14, 9), "p2", ("t",)),
        cand(mkts(2026, 9, 14, 10), "p1", ("u",)),
    ]
    ros = make_roster(True, extra={u: None for u in ("t", "u", "p1", "p2")})
    e = elig_of(ledger, rules=DR_PLAIN, roster=ros)
    pairs = build_pairs_table(e)
    assert rank_pairs(pairs) == tuple(pairs)          # builder already canonical
    ms = build_most_sniped_table(e, ros)
    assert rank_most_sniped(ms) == tuple(ms)


def test_top_n_cutoff_collapses_boundary_tie_only():
    ledger = []
    hour = 8
    # counts 5,4,3,3,3,2 by using that many distinct snipers per target
    plan = {"t5": 5, "t4": 4, "t3a": 3, "t3b": 3, "t3c": 3, "t2": 2}
    snipers = [f"s{i}" for i in range(6)]
    extra = {u: None for u in list(plan) + snipers}
    ros = make_roster(True, extra=extra)
    for target, n in plan.items():
        for i in range(n):
            hour += 1
            ledger.append(cand(mkts(2026, 9, 14, hour % 24, (hour // 24) * 5), snipers[i],
                               (target,)))
    e = elig_of(ledger, rules=DR_PLAIN, roster=ros)
    ranked = build_pairs_table(e)   # any ranked rows; use count as the leading metric
    # rank targets instead: build most_sniped which ranks by times_sniped
    ms = build_most_sniped_table(e, ros)
    cut = top_n_cutoff(ms, 3, lambda r: r.times_sniped)
    assert cut.cutoff_value == 3
    assert len(cut.head) == 3
    # boundary tie = the remaining rows at exactly 3 (two of the three "3" targets
    # are beyond the head), rows at 2 are dropped
    assert all(r.times_sniped == 3 for r in cut.boundary_tie)
    assert len(cut.boundary_tie) == 2
    assert "t2" not in {r.person for r in cut.boundary_tie}


def test_top_n_cutoff_no_overflow_when_within_budget():
    ledger = [
        cand(mkts(2026, 9, 14, 8), "p1", ("t",)),
        cand(mkts(2026, 9, 14, 9), "p2", ("u",)),
    ]
    ros = make_roster(True, extra={u: None for u in ("t", "u", "p1", "p2")})
    ms = build_most_sniped_table(elig_of(ledger, rules=DR_PLAIN, roster=ros), ros)
    cut = top_n_cutoff(ms, 5, lambda r: r.times_sniped)
    assert cut.boundary_tie == ()
    assert cut.cutoff_value is None
    assert len(cut.head) == len(ms)


# --------------------------------------------------------------------------- #
# Purity: no clock read in the aggregation layer
# --------------------------------------------------------------------------- #

def test_layer_reads_no_clock(monkeypatch):
    import snipebot.aggregate as agg

    real_dt = agg.datetime

    class NoClock(real_dt):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("aggregate read the wall clock")

        @classmethod
        def utcnow(cls):
            raise AssertionError("aggregate read the wall clock")

        @classmethod
        def today(cls):
            raise AssertionError("aggregate read the wall clock")

    monkeypatch.setattr(agg, "datetime", NoClock)
    e = elig_of(_mixed_ledger())
    build_snipes_table(e, ROS)
    build_daily_table(e)
    build_people_table(e, ROS)
    build_groups_table(e, ROS, frozenset())
    build_most_sniped_table(e, ROS)
    build_pairs_table(e)
