"""Unit tests for `evaluate` (00-data section 4, section 5).

Candidates are built by hand (never via `parse`) so each test pins one clause of
the precedence tables or the cooldown algorithm. Times are Slack ts strings; all
arithmetic in the module under test is integer microseconds.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from snipebot import rules
from snipebot.config import (
    CooldownRule,
    DatedRules,
    NoRuleInForceError,
    ResolvedRule,
    ReviewFlag,
    Roster,
    RosterEntry,
    Scope,
    MultiTag,
    Semester,
)
from snipebot.parse import Candidate, SelfieOverride, TargetEdit, Veto, VetoSource
from snipebot.rules import (
    MessageVerdict,
    Reason,
    SelfieClass,
    Status,
    evaluate,
    needs_review,
)
from snipebot.ts import US_PER_MINUTE

TZ = ZoneInfo("UTC")
BASE = 1_700_000_000            # 2023-11-14 22:13:20 UTC; ~6400 s to next UTC midnight
SEM = Semester(name="F26", start_us=0, end_us=4_000_000_000 * 1_000_000)


def ts(offset_s: int, micro: int = 0) -> str:
    return f"{BASE + offset_s}.{micro:06d}"


def cand(ts_str: str, sender: str = "U_S", targets=("U_T",), **over) -> Candidate:
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


def rule(
    *,
    cooldown_min: int = 15,
    scope: Scope = Scope.PAIR,
    reset: bool = False,
    multi_tag: MultiTag = MultiTag.PER_TARGET,
    max_targets=None,
    edit_grace_min: int = 10,
    daily_cap=None,
    allow_self: bool = False,
    allow_bots: bool = False,
    count_threads: bool = False,
    count_links: bool = False,
    allow_video: bool = False,
    selfie_bonus: bool = False,
    eff_from: int = 0,
) -> ResolvedRule:
    return ResolvedRule(
        effective_from_us=eff_from,
        cooldown=CooldownRule(
            microseconds=cooldown_min * US_PER_MINUTE,
            scope=scope,
            rejected_attempts_reset=reset,
        ),
        multi_tag=multi_tag,
        max_targets_per_message=max_targets,
        edit_grace_us=edit_grace_min * US_PER_MINUTE,
        max_snipes_per_target_per_day=daily_cap,
        allow_self=allow_self,
        allow_bots=allow_bots,
        count_thread_replies=count_threads,
        count_image_links=count_links,
        allow_video=allow_video,
        selfie_bonus=selfie_bonus,
    )


def dated(*rs: ResolvedRule) -> DatedRules:
    return DatedRules(entries=tuple(sorted(rs, key=lambda r: r.effective_from_us)))


def roster(members: dict, count_intra_group: bool = True) -> Roster:
    """members: user -> dict(group=..., is_bot=..., join=...)."""
    entries = {
        u: RosterEntry(
            user=u,
            join_us=m.get("join", 0),
            group=m.get("group"),
            is_bot=m.get("is_bot", False),
        )
        for u, m in members.items()
    }
    return Roster(entries=entries, count_intra_group=count_intra_group)


PLAIN = {"U_S": {}, "U_T": {}, "U_C": {}}


def run(cands, *, rules_=None, roster_=None, opted=frozenset(), sems=(SEM,)):
    return evaluate(
        cands,
        rules_ or dated(rule()),
        roster_ or roster(PLAIN),
        opted,
        sems,
        TZ,
    )


def only(cands, **kw) -> MessageVerdict:
    out = run(cands, **kw)
    assert len(out) == 1
    return out[0]


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #

def test_basic_counted():
    v = only([cand(ts(0))])
    assert v.status is Status.COUNTED
    assert v.reason is Reason.COUNTED
    assert v.selfie is SelfieClass.NOT_APPLICABLE
    assert len(v.pairs) == 1
    p = v.pairs[0]
    assert (p.target, p.status, p.reason, p.blocked_by, p.selfie) == (
        "U_T", Status.COUNTED, Reason.COUNTED, None, False,
    )


# --------------------------------------------------------------------------- #
# Cooldown boundary + blocked_by
# --------------------------------------------------------------------------- #

def test_cooldown_boundary_exact_counts():
    out = run([cand(ts(0)), cand(ts(900))])   # exactly 15 min apart
    assert [v.status for v in out] == [Status.COUNTED, Status.COUNTED]


def test_cooldown_one_micro_short_rejects():
    out = run([cand(ts(0)), cand(f"{BASE + 899}.999999")])
    assert out[1].status is Status.COOLDOWN
    p = out[1].pairs[0]
    assert p.status is Status.COOLDOWN
    assert p.reason is Reason.COOLDOWN
    assert p.blocked_by == ts(0)          # names the attempt that set the anchor
    assert p.selfie is False


# --------------------------------------------------------------------------- #
# rejected_attempts_reset on/off (distinguished by blocked_by)
# --------------------------------------------------------------------------- #

def test_rejected_attempts_reset_off_keeps_original_anchor():
    r = dated(rule(reset=False))
    out = run([cand(ts(0)), cand(ts(300)), cand(ts(600))], rules_=r)
    assert out[1].pairs[0].blocked_by == ts(0)
    assert out[2].pairs[0].status is Status.COOLDOWN
    assert out[2].pairs[0].blocked_by == ts(0)      # still measured from the counted row


def test_rejected_attempts_reset_on_moves_anchor():
    r = dated(rule(reset=True))
    out = run([cand(ts(0)), cand(ts(300)), cand(ts(600))], rules_=r)
    assert out[1].pairs[0].blocked_by == ts(0)
    assert out[2].pairs[0].status is Status.COOLDOWN
    assert out[2].pairs[0].blocked_by == ts(300)    # the rejected attempt restarted the clock


# --------------------------------------------------------------------------- #
# late_tag: grace-inclusive
# --------------------------------------------------------------------------- #

def test_late_tag_edit_at_grace_boundary_counts():
    edit = ts(600)                        # exactly ts + 10 min grace
    c = cand(ts(0), first_seen_targets=frozenset(),
             target_edited_in=(TargetEdit(user="U_T", edit_ts=edit),))
    assert only([c]).pairs[0].reason is Reason.COUNTED


def test_late_tag_one_micro_past_grace_rejects():
    edit = f"{BASE + 600}.000001"          # ts + grace + 1 us
    c = cand(ts(0), first_seen_targets=frozenset(),
             target_edited_in=(TargetEdit(user="U_T", edit_ts=edit),))
    p = only([c]).pairs[0]
    assert p.status is Status.NOT_COUNTED
    assert p.reason is Reason.LATE_TAG


def test_late_tag_absent_target_no_edit_evidence_counts():
    # appeared later but no recorded edit time -> not late (evidence rule)
    c = cand(ts(0), first_seen_targets=frozenset(), target_edited_in=())
    assert only([c]).pairs[0].reason is Reason.COUNTED


# --------------------------------------------------------------------------- #
# daily cap
# --------------------------------------------------------------------------- #

def test_daily_cap_reached():
    r = dated(rule(daily_cap=1))
    out = run([cand(ts(0)), cand(ts(1000))], rules_=r)   # both same UTC day, > cooldown apart
    assert out[0].pairs[0].reason is Reason.COUNTED
    assert out[1].pairs[0].reason is Reason.DAILY_CAP
    assert out[1].pairs[0].blocked_by is None


def test_daily_cap_resets_next_day():
    r = dated(rule(daily_cap=1))
    out = run([cand(ts(0)), cand(ts(7000))], rules_=r)   # +7000 s crosses UTC midnight
    assert out[0].pairs[0].reason is Reason.COUNTED
    assert out[1].pairs[0].reason is Reason.COUNTED


# --------------------------------------------------------------------------- #
# multi_tag
# --------------------------------------------------------------------------- #

def test_multi_tag_per_target_counts_each():
    v = only([cand(ts(0), targets=("U_T", "U_C"))])
    assert v.status is Status.COUNTED
    assert [p.reason for p in v.pairs] == [Reason.COUNTED, Reason.COUNTED]


def test_multi_tag_single_folds_the_rest():
    r = dated(rule(multi_tag=MultiTag.SINGLE))
    v = only([cand(ts(0), targets=("U_T", "U_C"))], rules_=r)
    assert v.status is Status.COUNTED
    assert v.pairs[0].reason is Reason.COUNTED
    assert v.pairs[1].reason is Reason.MULTI_TAG_FOLDED
    assert v.pairs[1].status is Status.NOT_COUNTED


def test_multi_tag_single_folds_in_mention_order():
    # the chosen target is the first still-eligible one in mention order
    r = dated(rule(multi_tag=MultiTag.SINGLE))
    v = only([cand(ts(0), targets=("U_C", "U_T"))], rules_=r)
    assert v.pairs[0].target == "U_C"
    assert v.pairs[0].reason is Reason.COUNTED
    assert v.pairs[1].reason is Reason.MULTI_TAG_FOLDED


def test_per_target_independent_cooldown_owner_example():
    # A->B counted at t; A->{B,C} at t+dt (< cooldown): B cooldown, C counts, msg counted
    out = run([cand(ts(0)), cand(ts(300), targets=("U_T", "U_C"))])
    v = out[1]
    assert v.status is Status.COUNTED
    b, c = v.pairs
    assert (b.target, b.status, b.blocked_by) == ("U_T", Status.COOLDOWN, ts(0))
    assert (c.target, c.status) == ("U_C", Status.COUNTED)


# --------------------------------------------------------------------------- #
# Roster gates, both sides
# --------------------------------------------------------------------------- #

def test_sender_off_roster():
    r = roster({"U_T": {}})               # sender absent
    v = only([cand(ts(0))], roster_=r)
    assert v.status is Status.NOT_COUNTED
    assert v.reason is Reason.SENDER_OFF_ROSTER
    assert v.pairs == ()


def test_target_off_roster_by_join_date():
    r = roster({"U_S": {}, "U_T": {"join": (BASE + 100) * 1_000_000}})
    v = only([cand(ts(0))], roster_=r)     # message ts precedes target's join
    assert v.pairs[0].reason is Reason.TARGET_OFF_ROSTER


def test_target_is_bot():
    r = roster({"U_S": {}, "U_T": {"is_bot": True}})
    assert only([cand(ts(0))], roster_=r).pairs[0].reason is Reason.TARGET_IS_BOT


def test_target_is_bot_allowed():
    r = roster({"U_S": {}, "U_T": {"is_bot": True}})
    assert only([cand(ts(0))], roster_=r, rules_=dated(rule(allow_bots=True))
                ).pairs[0].reason is Reason.COUNTED


# --------------------------------------------------------------------------- #
# Other per-message gates
# --------------------------------------------------------------------------- #

def test_out_of_season():
    late = Semester(name="S27", start_us=(BASE + 10_000) * 1_000_000, end_us=10 ** 20)
    v = only([cand(ts(0))], sems=(late,))
    assert v.reason is Reason.OUT_OF_SEASON


def test_not_top_level():
    c = cand(ts(0), thread_ts=ts(-50))
    assert only([c]).reason is Reason.NOT_TOP_LEVEL


def test_thread_reply_counts_when_configured():
    c = cand(ts(0), thread_ts=ts(-50))
    assert only([c], rules_=dated(rule(count_threads=True))).status is Status.COUNTED


def test_no_live_image():
    assert only([cand(ts(0), live_images=0)]).reason is Reason.NO_LIVE_IMAGE


def test_untagged():
    v = only([cand(ts(0), targets=(), first_seen_targets=frozenset())])
    assert v.status is Status.UNTAGGED
    assert v.reason is Reason.UNTAGGED
    assert v.pairs == ()


def test_too_many_targets():
    r = dated(rule(max_targets=1))
    v = only([cand(ts(0), targets=("U_T", "U_C"))], rules_=r)
    assert v.reason is Reason.TOO_MANY_TARGETS


# --------------------------------------------------------------------------- #
# Opt-out (must never be signalled, but must gate)
# --------------------------------------------------------------------------- #

def test_sender_opted_out():
    v = only([cand(ts(0))], opted=frozenset({"U_S"}))
    assert v.reason is Reason.SENDER_OPTED_OUT
    assert v.pairs == ()


def test_target_opted_out():
    v = only([cand(ts(0))], opted=frozenset({"U_T"}))
    assert v.pairs[0].reason is Reason.TARGET_OPTED_OUT


# --------------------------------------------------------------------------- #
# Veto (evaluate sees only the already-permitted vetoes; sync filters actors)
# --------------------------------------------------------------------------- #

def test_veto_present_voids_message():
    for src in (VetoSource.REACTION, VetoSource.CLI):
        c = cand(ts(0), vetoes=(Veto(by="U_A", source=src),))
        v = only([c])
        assert v.reason is Reason.VETOED
        assert v.pairs == ()


def test_no_veto_counts():
    assert only([cand(ts(0), vetoes=())]).status is Status.COUNTED


def test_veto_voids_every_pair():
    c = cand(ts(0), targets=("U_T", "U_C"), vetoes=(Veto(by="U_A", source=VetoSource.CLI),))
    assert only([c]).pairs == ()


# --------------------------------------------------------------------------- #
# Precedence when several gates trip
# --------------------------------------------------------------------------- #

def test_precedence_deleted_beats_lower_gates():
    c = cand(ts(0), missing_runs=2, vetoes=(Veto(by="U_A", source=VetoSource.CLI),),
             live_images=0)
    assert only([c]).reason is Reason.DELETED


def test_precedence_roster_beats_no_image():
    r = roster({"U_T": {}})               # sender off roster
    c = cand(ts(0), live_images=0)
    assert only([c], roster_=r).reason is Reason.SENDER_OFF_ROSTER


def test_message_reason_is_highest_precedence_target_reason():
    # one late tag (rank 4) + one opted-out target (rank 1): opted-out wins the label
    c = cand(ts(0), targets=("U_T", "U_C"), first_seen_targets=frozenset({"U_C"}),
             target_edited_in=(TargetEdit(user="U_T", edit_ts=f"{BASE + 600}.000001"),))
    v = only([c], opted=frozenset({"U_C"}))
    assert v.status is Status.NOT_COUNTED
    assert v.reason is Reason.TARGET_OPTED_OUT


# --------------------------------------------------------------------------- #
# Selfie classification
# --------------------------------------------------------------------------- #

SIBS = {"U_S": {"group": "g1"}, "U_T": {"group": "g1"}, "U_C": {"group": "g1"}}


def sib_cand(ts_str, targets=("U_T",), counts=None, **over):
    ids = tuple(f"f{i}" for i in range(len(counts or [])))
    fc = dict(zip(ids, counts or []))
    return cand(ts_str, targets=targets, live_image_ids=ids, face_counts=fc, **over)


def sib_rule():
    return dated(rule(selfie_bonus=True))


def test_selfie_class_T_is_snipe():
    v = only([sib_cand(ts(0), counts=[1])], rules_=sib_rule(), roster_=roster(SIBS))
    assert v.selfie is SelfieClass.SNIPE
    assert v.pairs[0].selfie is False


def test_selfie_class_T_plus_one_is_selfie():
    v = only([sib_cand(ts(0), counts=[2])], rules_=sib_rule(), roster_=roster(SIBS))
    assert v.selfie is SelfieClass.SELFIE
    assert v.pairs[0].selfie is True          # intra-group, counted, selfie message


def test_selfie_class_other_count_is_ambiguous():
    v = only([sib_cand(ts(0), counts=[5])], rules_=sib_rule(), roster_=roster(SIBS))
    assert v.selfie is SelfieClass.AMBIGUOUS
    assert v.pairs[0].selfie is False          # ambiguous scores as a plain snipe


def test_selfie_class_missing_count_is_ambiguous():
    c = cand(ts(0), live_image_ids=("f0",), face_counts={})
    v = only([c], rules_=sib_rule(), roster_=roster(SIBS))
    assert v.selfie is SelfieClass.AMBIGUOUS


def test_selfie_not_sib_tagged_is_not_applicable():
    # counted, but sender/target in different groups -> never classified
    r = roster({"U_S": {"group": "g1"}, "U_T": {"group": "g2"}})
    v = only([sib_cand(ts(0), counts=[2])], rules_=sib_rule(), roster_=r)
    assert v.selfie is SelfieClass.NOT_APPLICABLE


def test_selfie_bonus_false_is_not_applicable():
    v = only([sib_cand(ts(0), counts=[2])], rules_=dated(rule(selfie_bonus=False)),
             roster_=roster(SIBS))
    assert v.selfie is SelfieClass.NOT_APPLICABLE
    assert v.pairs[0].selfie is False


def test_selfie_cross_group_pair_stays_plain_on_selfie_message():
    # sender+U_T are sibs (g1); U_C is g2. Selfie message, but the cross-group pair is plain.
    r = roster({"U_S": {"group": "g1"}, "U_T": {"group": "g1"}, "U_C": {"group": "g2"}})
    # T = 2 targets, so a selfie is T+1 = 3 faces in frame
    v = only([sib_cand(ts(0), targets=("U_T", "U_C"), counts=[3])],
             rules_=sib_rule(), roster_=r)
    assert v.selfie is SelfieClass.SELFIE
    by_target = {p.target: p.selfie for p in v.pairs}
    assert by_target == {"U_T": True, "U_C": False}


# --------------------------------------------------------------------------- #
# Selfie override (REACTION / CLI, both values) beats the detector
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("src", [VetoSource.REACTION, VetoSource.CLI])
def test_override_true_forces_selfie(src):
    ov = SelfieOverride(value=True, by="U_A", source=src)
    v = only([sib_cand(ts(0), counts=[1], selfie_override=ov)],   # detector would say SNIPE
             rules_=sib_rule(), roster_=roster(SIBS))
    assert v.selfie is SelfieClass.SELFIE


@pytest.mark.parametrize("src", [VetoSource.REACTION, VetoSource.CLI])
def test_override_false_forces_snipe(src):
    ov = SelfieOverride(value=False, by="U_A", source=src)
    v = only([sib_cand(ts(0), counts=[2], selfie_override=ov)],   # detector would say SELFIE
             rules_=sib_rule(), roster_=roster(SIBS))
    assert v.selfie is SelfieClass.SNIPE
    assert v.pairs[0].selfie is False


# --------------------------------------------------------------------------- #
# REPOST
# --------------------------------------------------------------------------- #

def test_repost_same_semester_any_sender_not_counted():
    a = cand(ts(0), targets=("U_T",), live_image_ids=("f0",),
             face_counts={"f0": 2}, rendition_hash={"f0": "HHH"})
    b = cand(ts(300), sender="U_T", targets=("U_S",), live_image_ids=("g0",),
             face_counts={"g0": 2}, rendition_hash={"g0": "HHH"})   # same bytes, later ts
    out = run([a, b], rules_=sib_rule(), roster_=roster(SIBS))
    assert out[0].status is Status.COUNTED
    assert out[1].reason is Reason.REPOST
    assert out[1].status is Status.NOT_COUNTED
    assert out[1].pairs == ()


def test_repost_consumes_no_cooldown():
    a = cand(ts(0), rendition_hash={"f0": "HHH"}, live_image_ids=("f0",),
             face_counts={"f0": 2})
    b = cand(ts(300), rendition_hash={"g0": "HHH"}, live_image_ids=("g0",),
             face_counts={"g0": 2})                     # repost, sets no anchor
    c = cand(ts(900), rendition_hash={"h0": "OTHER"}, live_image_ids=("h0",),
             face_counts={"h0": 2})                     # 15 min after the counted row
    out = run([a, b, c], rules_=sib_rule(), roster_=roster(SIBS))
    assert out[1].reason is Reason.REPOST
    assert out[2].pairs[0].reason is Reason.COUNTED     # anchor was still the first row


def test_repost_hash_does_not_match_across_semesters():
    s1 = Semester(name="s1", start_us=0, end_us=(BASE + 5000) * 1_000_000)
    s2 = Semester(name="s2", start_us=(BASE + 6000) * 1_000_000, end_us=10 ** 20)
    a = cand(ts(0), rendition_hash={"f0": "HHH"}, live_image_ids=("f0",),
             face_counts={"f0": 2})
    b = cand(ts(7000), rendition_hash={"g0": "HHH"}, live_image_ids=("g0",),
             face_counts={"g0": 2})
    out = run([a, b], rules_=sib_rule(), roster_=roster(SIBS), sems=(s1, s2))
    assert out[1].reason is Reason.COUNTED


# --------------------------------------------------------------------------- #
# needs_review
# --------------------------------------------------------------------------- #

def test_needs_review_on_ambiguous_even_when_min_targets_null():
    c = sib_cand(ts(0), counts=[5])
    v = only([c], rules_=sib_rule(), roster_=roster(SIBS))
    assert v.selfie is SelfieClass.AMBIGUOUS
    assert needs_review(v, c, ReviewFlag(min_targets=None, emoji="question")) is True


def test_needs_review_on_many_targets():
    r = roster({"U_S": {}, "U_T": {}, "U_C": {}})
    c = cand(ts(0), targets=("U_T", "U_C"))
    v = only([c], roster_=r)
    assert v.status is Status.COUNTED
    assert needs_review(v, c, ReviewFlag(min_targets=2, emoji="question")) is True
    assert needs_review(v, c, ReviewFlag(min_targets=3, emoji="question")) is False


def test_needs_review_false_when_not_counted():
    c = cand(ts(0))
    v = only([c], opted=frozenset({"U_T"}))    # target opted out -> not counted
    assert v.status is Status.NOT_COUNTED
    assert needs_review(v, c, ReviewFlag(min_targets=1, emoji="question")) is False


# --------------------------------------------------------------------------- #
# NoRuleInForceError
# --------------------------------------------------------------------------- #

def test_no_rule_in_force_raises():
    future = dated(rule(eff_from=(BASE + 10_000) * 1_000_000))
    with pytest.raises(NoRuleInForceError):
        evaluate([cand(ts(0))], future, roster(PLAIN), frozenset(), (SEM,), TZ)


# --------------------------------------------------------------------------- #
# Determinism / purity
# --------------------------------------------------------------------------- #

def _timeline():
    return [
        cand(ts(0), targets=("U_T", "U_C")),
        cand(ts(300)),
        cand(ts(1000), sender="U_T", targets=("U_S",)),
        cand(ts(2000), targets=("U_C",)),
        cand(ts(5000)),
    ]


def test_determinism_same_input_twice():
    a = run(_timeline())
    b = run(_timeline())
    assert a == b


def test_input_order_independence():
    fwd = run(_timeline())
    rev = run(list(reversed(_timeline())))
    assert fwd == rev
    assert [v.ts for v in fwd] == sorted(v.ts for v in fwd)


def test_suffix_monotonicity():
    head = _timeline()
    with_extra = head + [cand(ts(9000), targets=("U_T",))]
    a = run(head)
    b = run(with_extra)
    assert a == b[: len(a)]                    # older verdicts unchanged by a later message


def test_evaluate_never_reads_the_clock(monkeypatch):
    import datetime as _dt

    class _NoClock:
        @staticmethod
        def fromtimestamp(*a, **k):
            return _dt.datetime.fromtimestamp(*a, **k)

        @staticmethod
        def now(*a, **k):
            raise AssertionError("evaluate must not read the wall clock")

        today = now
        utcnow = now

    monkeypatch.setattr(rules, "datetime", _NoClock)
    # exercises the daily-cap path (the only place a date is derived)
    out = run([cand(ts(0)), cand(ts(1000))], rules_=dated(rule(daily_cap=1)))
    assert out[1].pairs[0].reason is Reason.DAILY_CAP
