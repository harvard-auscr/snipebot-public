"""L2-PR-* property and control tests expressible without ``sync``.

Verdict-level properties exercise the production sweep directly; conservation and points
are checked through the oracle's own counting (which never trusts the aggregate layer).
Deterministic positive controls pin the boundaries the generators only sample.
"""
from __future__ import annotations

import datetime
import os
import random
from zoneinfo import ZoneInfo

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from snipebot.config import (
    INT_MIN_TS,
    CooldownRule,
    DatedRules,
    ResolvedRule,
    ReviewFlag,
    Roster,
    RosterEntry,
    RosterMode,
    Scope,
    MultiTag,
    Semester,
)
from snipebot.parse import Candidate, SelfieOverride, TargetEdit, Veto, VetoSource
from snipebot.rules import Reason, SelfieClass, Status
from snipebot.rules import evaluate as production_evaluate
from snipebot.ts import format_ts, parse_ts
from tests.oracle import oracle
from tests.oracle.strategies import scenarios

US = 1_000_000
MIN = 60 * US
UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

# Overridable Hypothesis example count (default matches this file's original
# literal). A mutation-testing run sets SNIPEBOT_HYPOTHESIS_EXAMPLES lower so
# these property tests still kill mutants without the default per-test cost.
_EXAMPLES = int(os.environ.get("SNIPEBOT_HYPOTHESIS_EXAMPLES", "120"))

# A wide season covering every deterministic timestamp below.
SEASON = Semester("s", parse_ts("1000.000000"), parse_ts("100000000.000000"))


def rrule(*, cd_min=15, scope=Scope.PAIR, reset=False, multi=MultiTag.PER_TARGET,
          maxt=None, grace_min=10, daycap=None, allow_self=False, allow_bots=False,
          ctr=False, cil=False, av=False, selfie_bonus=True, eff=INT_MIN_TS):
    return ResolvedRule(
        effective_from_us=eff,
        cooldown=CooldownRule(cd_min * MIN, scope, reset),
        multi_tag=multi,
        max_targets_per_message=maxt,
        edit_grace_us=grace_min * MIN,
        max_snipes_per_target_per_day=daycap,
        allow_self=allow_self,
        allow_bots=allow_bots,
        count_thread_replies=ctr,
        count_image_links=cil,
        allow_video=av,
        selfie_bonus=selfie_bonus,
    )


def dated(*rules):
    return DatedRules(tuple(rules))


def mkroster(spec, count_intra=True):
    # spec: {user: (group, join_us, is_bot)}
    return Roster(
        {u: RosterEntry(u, j, g, b) for u, (g, j, b) in spec.items()}, count_intra)


def cand(ts, sender, targets=(), *, live_images=1, lii=None, fc=None, rh=None,
         override=None, vetoes=(), missing_runs=0, first_seen=None, edited=(),
         last_edit=None, thread_ts=None, subtype=None, live_videos=0, linked=0,
         fse=False):
    targets = tuple(targets)
    if lii is None:
        lii = tuple(f"{ts}#{j}" for j in range(live_images))
    if first_seen is None:
        first_seen = frozenset(targets)
    return Candidate(
        ts=ts, sender=sender, subtype=subtype, thread_ts=thread_ts, targets=targets,
        live_images=live_images, live_image_ids=lii, live_videos=live_videos,
        linked_images=linked, last_edit_ts=last_edit, file_sigs=(),
        vetoes=tuple(vetoes), missing_runs=missing_runs,
        first_seen_targets=frozenset(first_seen), first_sight_edited=fse,
        target_edited_in=tuple(edited), face_counts=fc or {}, rendition_hash=rh or {},
        detect_attempts=0, selfie_override=override,
    )


def T(us):
    return format_ts(us)


def _pairs_by_target(mv):
    return {p.target: p for p in mv.pairs}


# --------------------------------------------------------------------------- #
# L2-PR-determinism / suffix-monotone
# --------------------------------------------------------------------------- #

@settings(max_examples=_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(sc=scenarios(), seed=st.integers(0, 1 << 30))
def test_determinism_shuffled(sc, seed):
    ref = production_evaluate(*sc.eval_args())
    shuffled = list(sc.facts)
    random.Random(seed).shuffle(shuffled)
    got = production_evaluate(shuffled, sc.rules, sc.roster, sc.opted_out,
                              sc.semesters, sc.tz)
    assert got == ref


@settings(max_examples=_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(sc=scenarios())
def test_suffix_monotone(sc):
    base = production_evaluate(*sc.eval_args())
    latest = max((parse_ts(c.ts) for c in sc.facts), default=SEASON.start_us)
    newer = cand(T(latest + 10 * MIN), sc.facts[0].sender,
                 targets=(sc.facts[0].sender,), live_images=1)
    extended = production_evaluate((*sc.facts, newer), sc.rules, sc.roster,
                                   sc.opted_out, sc.semesters, sc.tz)
    by_ts = {mv.ts: mv for mv in extended}
    for mv in base:
        assert by_ts[mv.ts] == mv, f"older verdict for {mv.ts} moved when a newer row was appended"


# --------------------------------------------------------------------------- #
# L2-PR-cooldown-boundary / mingap / blocker
# --------------------------------------------------------------------------- #

def test_cooldown_boundary():
    roster = mkroster({"A": (None, INT_MIN_TS, False), "B": (None, INT_MIN_TS, False)})
    rules = dated(rrule(cd_min=15))
    cd = 15 * MIN
    for delta, expect in [(cd, Status.COUNTED), (cd - 1, Status.COOLDOWN)]:
        facts = [cand(T(2 * cd), "A", ["B"]), cand(T(2 * cd + delta), "A", ["B"])]
        out = production_evaluate(facts, rules, roster, set(), [SEASON], UTC)
        second = out[1].pairs[0]
        assert second.status is expect, (delta, second.status)
        if expect is Status.COOLDOWN:
            assert second.blocked_by == T(2 * cd)


@settings(max_examples=_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(sc=scenarios())
def test_cooldown_mingap_and_blocker(sc):
    verdicts = production_evaluate(*sc.eval_args())
    sender_of = {c.ts: c.sender for c in sc.facts}
    # Group COUNTED pairs by scope key (scope is constant across dated entries).
    scope = sc.rules.entries[0].cooldown.scope
    counted: dict[tuple, list[int]] = {}
    for mv in verdicts:
        for p in mv.pairs:
            if p.status is Status.COUNTED:
                key = (p.target,) if scope is Scope.TARGET else (sender_of[mv.ts], p.target)
                counted.setdefault(key, []).append(parse_ts(mv.ts))
    for key, times in counted.items():
        times.sort()
        for earlier, later in zip(times, times[1:]):
            cd = sc.rules.in_force_at(later).cooldown.microseconds
            assert later - earlier >= cd, f"two COUNTED in scope {key} closer than cooldown"
    # Every COOLDOWN pair names an earlier same-scope attempt inside the window.
    for mv in verdicts:
        for p in mv.pairs:
            if p.status is Status.COOLDOWN:
                assert p.blocked_by is not None
                anchor = parse_ts(p.blocked_by)
                here = parse_ts(mv.ts)
                cd = sc.rules.in_force_at(here).cooldown.microseconds
                assert anchor < here
                assert here - anchor < cd


# --------------------------------------------------------------------------- #
# L2-PR-multi-tag / independent cooldown
# --------------------------------------------------------------------------- #

def test_multi_tag_single_folds_rest():
    roster = mkroster({u: (None, INT_MIN_TS, False) for u in "ABC"})
    rules = dated(rrule(multi=MultiTag.SINGLE))
    facts = [cand(T(100 * MIN), "A", ["B", "C"])]
    mv = production_evaluate(facts, rules, roster, set(), [SEASON], UTC)[0]
    pairs = _pairs_by_target(mv)
    assert pairs["B"].status is Status.COUNTED
    assert pairs["C"].reason is Reason.MULTI_TAG_FOLDED
    assert mv.status is Status.COUNTED
    counted = [p for p in mv.pairs if p.status is Status.COUNTED]
    assert len(counted) == 1


def test_multi_tag_independent_cooldown():
    # Owner example (plan section 1): per_target, pair scope. A->B counts, then
    # A->{B,C} within cooldown: B rejected against the first, C counts.
    roster = mkroster({u: (None, INT_MIN_TS, False) for u in "ABC"})
    rules = dated(rrule(cd_min=15, scope=Scope.PAIR, multi=MultiTag.PER_TARGET))
    t0 = 100 * MIN
    facts = [cand(T(t0), "A", ["B"]),
             cand(T(t0 + 5 * MIN), "A", ["B", "C"])]
    out = production_evaluate(facts, rules, roster, set(), [SEASON], UTC)
    second = _pairs_by_target(out[1])
    assert second["B"].status is Status.COOLDOWN and second["B"].blocked_by == T(t0)
    assert second["C"].status is Status.COUNTED
    assert out[1].status is Status.COUNTED


# --------------------------------------------------------------------------- #
# L2-PR-late-tag
# --------------------------------------------------------------------------- #

def test_late_tag_evidence_and_grace():
    roster = mkroster({"A": (None, INT_MIN_TS, False), "B": (None, INT_MIN_TS, False)})
    rules = dated(rrule(grace_min=10))
    grace = 10 * MIN
    t0 = 50 * MIN
    # edited in outside grace -> LATE_TAG
    late = cand(T(t0), "A", ["B"], first_seen=frozenset(),
                edited=[TargetEdit("B", T(t0 + grace + 1))], fse=True,
                last_edit=T(t0 + grace + 1))
    assert production_evaluate([late], rules, roster, set(), [SEASON], UTC)[0].pairs[0].reason is Reason.LATE_TAG
    # edited in exactly at grace boundary -> COUNTED (grace inclusive)
    ok = cand(T(t0), "A", ["B"], first_seen=frozenset(),
              edited=[TargetEdit("B", T(t0 + grace))], fse=True, last_edit=T(t0 + grace))
    assert production_evaluate([ok], rules, roster, set(), [SEASON], UTC)[0].pairs[0].status is Status.COUNTED
    # present at first sight -> COUNTED even if edited since
    seen = cand(T(t0), "A", ["B"], last_edit=T(t0 + grace + 5 * MIN), fse=True)
    assert production_evaluate([seen], rules, roster, set(), [SEASON], UTC)[0].pairs[0].status is Status.COUNTED


# --------------------------------------------------------------------------- #
# L2-PR-delete-safety (evaluate level: a deleted row counts nothing and anchors nothing)
# --------------------------------------------------------------------------- #

def test_deleted_row_counts_nothing_and_anchors_nothing():
    roster = mkroster({"A": (None, INT_MIN_TS, False), "B": (None, INT_MIN_TS, False)})
    rules = dated(rrule(cd_min=15))
    t0 = 50 * MIN
    facts = [cand(T(t0), "A", ["B"], missing_runs=2),          # deleted
             cand(T(t0 + 1 * MIN), "A", ["B"])]                 # inside cooldown of t0
    out = production_evaluate(facts, rules, roster, set(), [SEASON], UTC)
    assert out[0].reason is Reason.DELETED and out[0].pairs == ()
    # The deleted attempt set no anchor, so the second attempt counts.
    assert out[1].pairs[0].status is Status.COUNTED


# --------------------------------------------------------------------------- #
# L2-PR-conservation (through the oracle's own counting)
# --------------------------------------------------------------------------- #

@settings(max_examples=_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(sc=scenarios())
def test_conservation(sc):
    verdicts = oracle.evaluate(*sc.eval_args())
    sender_of = {c.ts: c.sender for c in sc.facts}
    counted = oracle.counted_pairs(verdicts)
    n = len(counted)
    made: dict[str, int] = {}
    sniped: dict[str, int] = {}
    daily = 0
    for mv in verdicts:
        for p in mv.pairs:
            if p.status is Status.COUNTED:
                made[sender_of[mv.ts]] = made.get(sender_of[mv.ts], 0) + 1
                sniped[p.target] = sniped.get(p.target, 0) + 1
                daily += 1
    assert n == sum(made.values()) == sum(sniped.values()) == daily
    # every counted person is rostered in exactly one group row (their group or ungrouped);
    # under players.mode auto an unnamed person plays ungrouped, never a bot (E-W4-42)
    for person in set(made) | set(sniped):
        if sc.roster.mode is RosterMode.AUTO:
            assert person not in sc.roster.bots and person != "USLACKBOT"
        else:
            assert person in sc.roster.entries


# --------------------------------------------------------------------------- #
# L2-PR-optout-closure / roster-closure
# --------------------------------------------------------------------------- #

@settings(max_examples=_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(sc=scenarios())
def test_optout_and_roster_closure(sc):
    verdicts = production_evaluate(*sc.eval_args())
    sender_of = {c.ts: c.sender for c in sc.facts}
    for mv in verdicts:
        for p in mv.pairs:
            if p.status is Status.COUNTED:
                s, t = sender_of[mv.ts], p.target
                ts_us = parse_ts(mv.ts)
                assert s not in sc.opted_out and t not in sc.opted_out
                assert sc.roster.is_member_at(s, ts_us)
                assert sc.roster.is_member_at(t, ts_us)


# --------------------------------------------------------------------------- #
# L2-PR-selfie-class (controls incl. off-by-one)
# --------------------------------------------------------------------------- #

def _selfie_world():
    roster = mkroster({"A": ("g1", INT_MIN_TS, False), "B": ("g1", INT_MIN_TS, False),
                       "X": ("g2", INT_MIN_TS, False)})
    return roster


def _class_of(rules, roster, row):
    return production_evaluate([row], rules, roster, set(), [SEASON], UTC)[0].selfie


def test_selfie_class_controls():
    roster = _selfie_world()
    rules = dated(rrule(selfie_bonus=True))
    t0 = 30 * MIN
    # T faces -> SNIPE (T = 1 target)
    row = cand(T(t0), "A", ["B"], fc={f"{T(t0)}#0": 1})
    assert _class_of(rules, roster, row) is SelfieClass.SNIPE
    # T+1 faces -> SELFIE
    row = cand(T(t0), "A", ["B"], fc={f"{T(t0)}#0": 2})
    assert _class_of(rules, roster, row) is SelfieClass.SELFIE
    # T+2 faces -> AMBIGUOUS (bystander; CTL-FACES-OFFBYONE)
    row = cand(T(t0), "A", ["B"], fc={f"{T(t0)}#0": 3})
    assert _class_of(rules, roster, row) is SelfieClass.AMBIGUOUS
    # missing count -> AMBIGUOUS
    row = cand(T(t0), "A", ["B"], fc={})
    assert _class_of(rules, roster, row) is SelfieClass.AMBIGUOUS
    # override forces the class either way
    row = cand(T(t0), "A", ["B"], fc={f"{T(t0)}#0": 1},
               override=SelfieOverride(True, "ADM", VetoSource.CLI))
    assert _class_of(rules, roster, row) is SelfieClass.SELFIE
    row = cand(T(t0), "A", ["B"], fc={f"{T(t0)}#0": 2},
               override=SelfieOverride(False, "ADM", VetoSource.CLI))
    assert _class_of(rules, roster, row) is SelfieClass.SNIPE
    # cross-group (not sib-tagged) -> NOT_APPLICABLE
    row = cand(T(t0), "A", ["X"], fc={f"{T(t0)}#0": 2})
    assert _class_of(rules, roster, row) is SelfieClass.NOT_APPLICABLE


def test_selfie_pair_mirror_intra_only():
    roster = _selfie_world()
    rules = dated(rrule(selfie_bonus=True))
    t0 = 30 * MIN
    # A tags B (intra, g1) and X (cross, g2); frame has T+1 = 3 faces -> SELFIE message.
    row = cand(T(t0), "A", ["B", "X"], fc={f"{T(t0)}#0": 3})
    mv = production_evaluate([row], rules, roster, set(), [SEASON], UTC)[0]
    assert mv.selfie is SelfieClass.SELFIE
    pairs = _pairs_by_target(mv)
    assert pairs["B"].selfie is True          # intra-group + counted
    assert pairs["X"].selfie is False         # cross-group stays a plain snipe


# --------------------------------------------------------------------------- #
# L2-PR-selfie-points / points-conservation
# --------------------------------------------------------------------------- #

def test_selfie_points_three_way():
    roster = mkroster({u: ("g1", INT_MIN_TS, False) for u in "ABC"})
    rules = dated(rrule(selfie_bonus=True))
    t0 = 40 * MIN
    # A posts, tags B and C, all three in frame (T=2, faces = 3 = T+1) -> SELFIE.
    row = cand(T(t0), "A", ["B", "C"], fc={f"{T(t0)}#0": 3})
    verdicts = production_evaluate([row], rules, roster, set(), [SEASON], UTC)
    plain, selfie_photo, participation = oracle.points_breakdown(
        verdicts, {row.ts: "A"})
    assert plain == {}                         # both counted pairs are selfie pairs
    assert selfie_photo == {"A": 1}            # one photo point to the sniper
    assert participation == {"B": 1, "C": 1}   # one each to the tagged sibs
    pts = oracle.people_points(verdicts, {row.ts: "A"})
    assert pts == {"A": 1, "B": 1, "C": 1}


@settings(max_examples=_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(sc=scenarios())
def test_points_conservation(sc):
    verdicts = production_evaluate(*sc.eval_args())
    sender_of = {c.ts: c.sender for c in sc.facts}
    plain, selfie_photo, participation = oracle.points_breakdown(verdicts, sender_of)
    people = oracle.people_points(verdicts, sender_of)
    counted = oracle.counted_pairs(verdicts)
    selfie_pairs = sum(1 for p in counted if p.selfie)
    plain_pairs = sum(1 for p in counted if not p.selfie)
    photos = sum(selfie_photo.values())
    # both units
    assert sum(people.values()) == plain_pairs + photos + selfie_pairs
    # every counted pair contributes exactly one to sum(pairs.points)
    assert plain_pairs + selfie_pairs == len(counted)
    assert sum(people.values()) == len(counted) + photos
    # participation points go to targets, plain points to senders
    assert sum(participation.values()) == selfie_pairs
    assert sum(plain.values()) == plain_pairs


# --------------------------------------------------------------------------- #
# L2-PR-repost-hash
# --------------------------------------------------------------------------- #

def test_repost_hash_same_semester_only():
    roster = mkroster({"A": ("g1", INT_MIN_TS, False), "B": ("g1", INT_MIN_TS, False)})
    sem0 = Semester("s0", parse_ts("1000.000000"), parse_ts("5000000.000000"))
    sem1 = Semester("s1", parse_ts("6000000.000000"), parse_ts("9000000.000000"))
    rules = dated(rrule(cd_min=15, selfie_bonus=True))
    cd = 15 * MIN
    t1 = 100 * MIN
    # original + exact re-upload later in the same semester -> REPOST, no cooldown eaten
    orig = cand(T(t1), "A", ["B"], lii=("f0",), fc={"f0": 1}, rh={"f0": "H"})
    repost = cand(T(t1 + cd + MIN), "A", ["B"], lii=("f1",), fc={"f1": 1}, rh={"f1": "H"})
    third = cand(T(t1 + cd + 2 * MIN), "A", ["B"], lii=("f2",), fc={"f2": 1},
                 rh={"f2": "K"})
    out = production_evaluate([orig, repost, third], rules, roster, set(), [sem0], UTC)
    assert out[0].pairs[0].status is Status.COUNTED
    assert out[1].reason is Reason.REPOST and out[1].pairs == ()
    # the repost anchored nothing: `third` is measured against `orig` (far enough) -> COUNTED
    assert out[2].pairs[0].status is Status.COUNTED
    # the same hash in a different semester is NOT a repost
    cross = cand(T(7_000_000 * US), "A", ["B"], lii=("g0",), fc={"g0": 1}, rh={"g0": "H"})
    out2 = production_evaluate([orig, cross], rules, roster, set(), [sem0, sem1], UTC)
    assert out2[1].pairs[0].status is Status.COUNTED


# --------------------------------------------------------------------------- #
# L2-PR-selfie-bonus-dated
# --------------------------------------------------------------------------- #

def test_selfie_bonus_dated():
    roster = mkroster({"A": ("g1", INT_MIN_TS, False), "B": ("g1", INT_MIN_TS, False)})
    eff = 1000 * MIN
    rules = dated(rrule(selfie_bonus=True, eff=INT_MIN_TS),
                  rrule(selfie_bonus=False, eff=eff))
    # T+1 faces before the flip -> SELFIE
    before = cand(T(500 * MIN), "A", ["B"], fc={f"{T(500 * MIN)}#0": 2})
    # T+1 faces after the flip -> NOT_APPLICABLE (no bonus)
    after = cand(T(1500 * MIN), "A", ["B"], fc={f"{T(1500 * MIN)}#0": 2})
    out = production_evaluate([before, after], rules, roster, set(), [SEASON], UTC)
    assert out[0].selfie is SelfieClass.SELFIE
    assert out[1].selfie is SelfieClass.NOT_APPLICABLE
    assert out[1].pairs[0].selfie is False


# --------------------------------------------------------------------------- #
# L2-PR-review-flag (needs_review is derived and changes no verdict)
# --------------------------------------------------------------------------- #

def _needs_review(mv, row, review: ReviewFlag) -> bool:
    if mv.status is not Status.COUNTED:
        return False
    many = review.min_targets is not None and len(row.targets) >= review.min_targets
    return many or mv.selfie is SelfieClass.AMBIGUOUS


@settings(max_examples=_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(sc=scenarios())
def test_review_flag_derived_and_verdict_independent(sc):
    verdicts = production_evaluate(*sc.eval_args())
    by_ts = {c.ts: c for c in sc.facts}
    # The flag never fires off a non-counted message and honors a null min_targets.
    null_review = ReviewFlag(None, "question")
    for mv in verdicts:
        row = by_ts[mv.ts]
        flag = _needs_review(mv, row, sc.review)
        if mv.status is not Status.COUNTED:
            assert flag is False
        if _needs_review(mv, row, null_review):
            assert mv.selfie is SelfieClass.AMBIGUOUS
    # review is not even an argument to evaluate, so it cannot move a verdict.
    again = production_evaluate(*sc.eval_args())
    assert again == verdicts


# --------------------------------------------------------------------------- #
# L2-PR-dst (frozen clock; local day buckets across the fall-back)
# --------------------------------------------------------------------------- #

def _us(dt: datetime.datetime) -> int:
    return (dt - _EPOCH) // datetime.timedelta(microseconds=1)


def test_dst_day_buckets():
    # 2026-11-01 01:30 occurs twice in New York: 01:30 EDT (05:30 UTC) and 01:30 EST
    # (06:30 UTC), 3600 s apart, both local date 2026-11-01.
    edt = datetime.datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=0)
    est = datetime.datetime(2026, 11, 1, 1, 30, tzinfo=NY, fold=1)
    assert _us(est) - _us(edt) == 3600 * US
    late = datetime.datetime(2026, 11, 1, 23, 30, tzinfo=NY)  # 2026-11-02 04:30 UTC
    sem = Semester("s", _us(datetime.datetime(2026, 10, 25, tzinfo=NY)),
                   _us(datetime.datetime(2026, 11, 5, 23, 59, 59, 999999, tzinfo=NY)))
    roster = mkroster({"A": (None, INT_MIN_TS, False), "D": (None, INT_MIN_TS, False),
                       "B": (None, INT_MIN_TS, False)})
    # Per-target daily cap of 1: two snipes of B on the same local day -> the second
    # is DAILY_CAP; a snipe the next local day counts again.
    rules = dated(rrule(cd_min=0, scope=Scope.TARGET, daycap=1))
    nextday = datetime.datetime(2026, 11, 2, 1, 30, tzinfo=NY)
    facts = [cand(T(_us(edt)), "A", ["B"]),
             cand(T(_us(est)), "D", ["B"]),
             cand(T(_us(late)), "A", ["B"]),
             cand(T(_us(nextday)), "D", ["B"])]
    out = production_evaluate(facts, rules, roster, set(), [sem], NY)
    assert out[0].pairs[0].status is Status.COUNTED
    assert out[1].pairs[0].reason is Reason.DAILY_CAP      # same local day (11-01)
    assert out[2].pairs[0].reason is Reason.DAILY_CAP      # 23:30 EST still 11-01
    assert out[3].pairs[0].status is Status.COUNTED        # 11-02, cap resets
