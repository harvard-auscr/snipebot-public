"""Backstop kills for mutation survivors the oracle/property strategies do not
reach (50-test-matrix.md section 6.3: "killed by a backstop"). Each test here
is named after the surviving mutant id it exists to kill; the docstring of
each records that id and is cross-referenced from tests/mutation/survivors.md.

This file is part of the mutmut runner (tests/mutation/mutmut_config.py /
pyproject.toml [tool.mutmut]), so it must stay fast and needs no fixtures
beyond the plain constructors already used by test_rules_fixtures.py and
test_aggregate.py.
"""

from __future__ import annotations

import dataclasses
import hashlib
import warnings
from fractions import Fraction

import pytest
from zoneinfo import ZoneInfo

from snipebot.aggregate import (
    UNGROUPED,
    EligibleSnipe,
    Eligibility,
    GroupRow,
    build_groups_table,
    build_people_table,
    build_most_sniped_table,
    rank_pairs,
    rank_groups,
    top_n_cutoff,
)
from snipebot.aggregate import _NO_SNIPE_US, _per_member, _top_sniper_of
from snipebot.config import (
    CooldownRule,
    DatedRules,
    ReviewFlag,
    ResolvedRule,
    Roster,
    RosterEntry,
    Scope,
    MultiTag,
    Semester,
)
from snipebot.parse import (
    Candidate,
    Digest,
    DigestMetadata,
    ParseAnomaly,
    SelfieOverride,
    TargetEdit,
    Veto,
    VetoSource,
    file_sig,
    parse,
)
from snipebot.parse import _RENDITION_KEYS, _block_mentions, _is_present, _strip_code
from snipebot.rules import (
    MessageVerdict,
    PairVerdict,
    Reason,
    SelfieClass,
    Status,
    evaluate,
    needs_review,
)
from snipebot.rules import _TARGET_REASON_RANK, _late_tag, _local_date_str
from snipebot.ts import US_PER_MINUTE, TsFormatError, format_ts, parse_ts

_TZ = ZoneInfo("UTC")
_SEM = Semester(name="F26", start_us=0, end_us=4_000_000_000 * 1_000_000)


def _rule(**over) -> ResolvedRule:
    base = dict(
        effective_from_us=0,
        cooldown=CooldownRule(
            microseconds=15 * US_PER_MINUTE, scope=Scope.PAIR, rejected_attempts_reset=False
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
        selfie_bonus=True,
    )
    base.update(over)
    return ResolvedRule(**base)


def _roster(members: dict[str, str | None]) -> Roster:
    return Roster(
        entries={
            u: RosterEntry(user=u, join_us=0, group=g, is_bot=False)
            for u, g in members.items()
        },
        count_intra_group=True,
    )


def _cand(ts: str, sender: str = "U_S", targets=("U_T",), **over) -> Candidate:
    base = dict(
        ts=ts,
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


def _only(cand: Candidate, roster: Roster, rule: ResolvedRule | None = None, opted_out=frozenset()):
    rules_ = DatedRules(entries=(rule or _rule(),))
    return evaluate([cand], rules_, roster, opted_out, (_SEM,), _TZ)[0]


def test_status_values_are_stable() -> None:
    """L4-MU-backstop-status-values.

    `Status` members are compared by identity everywhere in rules.py/
    aggregate.py (``is Status.COUNTED``), so a mutation of a member's
    underlying string literal is invisible to every identity-based
    assertion in the oracle/property/fixture suites; ledger.py persists
    ``status.value`` (and reads it back), so the literal is load-bearing
    for the stored ledger, not just cosmetic.
    """
    assert {s.name: s.value for s in Status} == {
        "COUNTED": "counted",
        "COOLDOWN": "cooldown",
        "UNTAGGED": "untagged",
        "NOT_COUNTED": "not_counted",
    }


def test_reason_values_are_stable() -> None:
    """L4-MU-backstop-reason-values. Same rationale as the Status literals:
    ledger.py persists ``reason.value``, and every call site compares by
    enum identity, so nothing in the scoped runner's fast tests would
    notice a mutated literal."""
    assert {r.name: r.value for r in Reason} == {
        "COUNTED": "counted",
        "COOLDOWN": "cooldown",
        "UNTAGGED": "untagged",
        "DELETED": "deleted",
        "NOT_TOP_LEVEL": "not_top_level",
        "OUT_OF_SEASON": "out_of_season",
        "SENDER_OFF_ROSTER": "sender_off_roster",
        "SENDER_OPTED_OUT": "sender_opted_out",
        "NO_LIVE_IMAGE": "no_live_image",
        "TOO_MANY_TARGETS": "too_many_targets",
        "VETOED": "vetoed",
        "REPOST": "repost",
        "SELF_SNIPE": "self_snipe",
        "TARGET_OPTED_OUT": "target_opted_out",
        "TARGET_OFF_ROSTER": "target_off_roster",
        "TARGET_IS_BOT": "target_is_bot",
        "LATE_TAG": "late_tag",
        "DAILY_CAP": "daily_cap",
        "MULTI_TAG_FOLDED": "multi_tag_folded",
    }


def test_selfie_class_values_are_stable() -> None:
    """L4-MU-backstop-selfie-class-values. Same identity-vs-literal gap as
    the two enums above, scoped to SelfieClass."""
    assert {c.name: c.value for c in SelfieClass} == {
        "SNIPE": "snipe",
        "SELFIE": "selfie",
        "AMBIGUOUS": "ambiguous",
        "NOT_APPLICABLE": "not_applicable",
    }


# --------------------------------------------------------------------------- #
# needs_review: no test in this runner's scope calls the real function.
# tests/oracle/test_properties.py's L2-PR-review-flag test checks the property
# against its OWN inline reimplementation (`_needs_review`), never against
# `snipebot.rules.needs_review`; that function is otherwise exercised only by
# tests/test_rules_unit.py, which is outside the pure-core+oracle+backstop
# runner (section 6.1). Any mutation of needs_review's body is invisible to
# every test this runner actually calls, so it is backstopped here.
# --------------------------------------------------------------------------- #

def test_needs_review_backstop_false_when_not_counted() -> None:
    """L4-MU-backstop-needs-review-not-counted."""
    c = _cand("1758210000.000000")
    v = _only(c, _roster({"U_S": None}), opted_out=frozenset({"U_T"}))
    assert v.status is Status.NOT_COUNTED
    assert needs_review(v, c, ReviewFlag(min_targets=1, emoji="question")) is False


def test_needs_review_backstop_many_targets_boundary() -> None:
    """L4-MU-backstop-needs-review-many-targets. Also pins the `>=` at
    `len(row.targets) >= review.min_targets`: a `>` mutation would flip the
    exact-equality case from True to False."""
    r = _roster({"U_S": None, "U_T": None, "U_C": None})
    c = _cand("1758210000.000000", targets=("U_T", "U_C"))
    v = _only(c, r)
    assert v.status is Status.COUNTED
    assert needs_review(v, c, ReviewFlag(min_targets=2, emoji="question")) is True
    assert needs_review(v, c, ReviewFlag(min_targets=3, emoji="question")) is False


def test_needs_review_backstop_ambiguous_selfie() -> None:
    """L4-MU-backstop-needs-review-ambiguous: fires even with min_targets None."""
    c = _cand(
        "1758210000.000000",
        live_image_ids=("F0FILE001",),
        face_counts={"F0FILE001": 3},   # len(targets)=1, T+2 faces -> AMBIGUOUS
    )
    v = _only(c, _roster({"U_S": "sibA", "U_T": "sibA"}))
    assert v.status is Status.COUNTED
    assert v.selfie is SelfieClass.AMBIGUOUS
    assert needs_review(v, c, ReviewFlag(min_targets=None, emoji="question")) is True


# --------------------------------------------------------------------------- #
# PairVerdict / MessageVerdict frozen-ness: no test in this runner's scope, or
# in the full suite, ever attempts to mutate one in place, so `frozen=True` ->
# `frozen=False` changes nothing any assertion observes even though it is a
# real contract loss (callers rely on these verdicts being safe to share and
# hash). Backstopped directly against the dataclass contract.
# --------------------------------------------------------------------------- #

def test_pair_verdict_backstop_is_frozen() -> None:
    """L4-MU-backstop-pair-verdict-frozen."""
    pv = PairVerdict(
        ts="1758210000.000000", target="U_T", status=Status.COUNTED,
        reason=Reason.COUNTED, blocked_by=None, selfie=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        pv.status = Status.COOLDOWN  # type: ignore[misc]


def test_message_verdict_backstop_is_frozen() -> None:
    """L4-MU-backstop-message-verdict-frozen."""
    mv = MessageVerdict(
        ts="1758210000.000000", status=Status.COUNTED, reason=Reason.COUNTED,
        selfie=SelfieClass.NOT_APPLICABLE, pairs=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        mv.status = Status.COOLDOWN  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# _TARGET_REASON_RANK: the oracle/property strategies exercise the *relative*
# ordering of most pairs of reasons, but not densely enough to catch every
# "bump one entry to the next entry's value" mutant (a collision creates a tie
# that `min()` then breaks by iteration order instead of by precedence, which
# only shows up for the specific target order the tie needs). Pinning the
# table's exact values is the direct invariant: nine reasons, nine distinct
# ranks, no collision possible.
# --------------------------------------------------------------------------- #

def test_target_reason_rank_backstop_values_are_distinct() -> None:
    """L4-MU-backstop-target-reason-rank."""
    assert _TARGET_REASON_RANK == {
        Reason.SELF_SNIPE: 0,
        Reason.TARGET_OPTED_OUT: 1,
        Reason.TARGET_OFF_ROSTER: 2,
        Reason.TARGET_IS_BOT: 3,
        Reason.LATE_TAG: 4,
        Reason.COOLDOWN: 5,
        Reason.DAILY_CAP: 6,
        Reason.MULTI_TAG_FOLDED: 7,
        Reason.COUNTED: 8,
    }


# --------------------------------------------------------------------------- #
# Second mutmut pass over rules.py (survivors.md "Run 2"): 15 SURVIVED, 17
# TIMEOUT, 6 SUSPICIOUS and 3 UNTESTED mutants, all closed with direct,
# deterministic backstops below instead of relying on the slow Hypothesis
# property suite to eventually reach them under -x.
# --------------------------------------------------------------------------- #

def test_local_date_str_backstop_format() -> None:
    """L4-MU-backstop-local-date-str-format. Mutant 79 wraps the strftime
    format string ("XX%Y-%m-%dXX"); nothing compares the literal format
    elsewhere, so pin the exact "YYYY-MM-DD" shape directly."""
    assert _local_date_str(1767225600_000000, _TZ) == "2026-01-01"


def test_late_tag_backstop_present_at_first_sight() -> None:
    """L4-MU-backstop-late-tag-first-sight. Mutant 81 flips the early
    `return False` to `return True` for a target present at first sight."""
    row = _cand("1758210000.000000", targets=("U_T",))
    rule = _rule()
    assert _late_tag(row, "U_T", rule, 1758210000_000000) is False


def test_late_tag_backstop_no_edit_record() -> None:
    """L4-MU-backstop-late-tag-no-record. Mutants 83 (`edit_ts` initial value
    `None`->`""`, which then crashes `parse_ts("")`) and 88 (final
    `return False`->`return True`): a target absent from first_seen_targets
    with no corresponding TargetEdit record must resolve to "not late",
    not raise and not flip to True."""
    row = _cand(
        "1758210000.000000", targets=("U_T",),
        first_seen_targets=frozenset(), target_edited_in=(),
    )
    rule = _rule()
    assert _late_tag(row, "U_T", rule, 1758210000_000000) is False


def test_late_tag_backstop_first_match_wins() -> None:
    """L4-MU-backstop-late-tag-first-match. Mutant 86 (`break`->`continue`):
    when more than one TargetEdit entry names the same target, the first
    (chronologically earliest recorded) edit_ts must govern, not the last."""
    late_edit_ts = "1758210000.500000"   # past grace relative to ts_us below
    row = _cand(
        "1758210000.000000", targets=("U_T",),
        first_seen_targets=frozenset(),
        target_edited_in=(
            TargetEdit(user="U_T", edit_ts=late_edit_ts),
            TargetEdit(user="U_T", edit_ts=None),
        ),
    )
    rule = _rule(edit_grace_us=0)
    assert _late_tag(row, "U_T", rule, 1758210000_000000) is True


def test_cooldown_key_backstop_scope_pair_vs_target() -> None:
    """L4-MU-backstop-cooldown-scope. Mutant 117 flips `is`->`is not` on
    `cd.scope is Scope.PAIR`, swapping which scope uses (sender,target) vs
    (target,) as the cooldown key."""
    roster = _roster({"U_A": None, "U_B": None, "U_T": None})
    ts0 = "1767225600.000000"
    ts1 = "1767225660.000000"   # 60s later, well inside any cooldown window

    pair_rule = _rule(cooldown=CooldownRule(
        microseconds=15 * US_PER_MINUTE, scope=Scope.PAIR, rejected_attempts_reset=False))
    out = evaluate(
        [_cand(ts0, sender="U_A", targets=("U_T",)), _cand(ts1, sender="U_B", targets=("U_T",))],
        DatedRules(entries=(pair_rule,)), roster, frozenset(), (_SEM,), _TZ,
    )
    assert out[0].pairs[0].status is Status.COUNTED
    assert out[1].pairs[0].status is Status.COUNTED   # different sender, PAIR scope -> no clash

    target_rule = _rule(cooldown=CooldownRule(
        microseconds=15 * US_PER_MINUTE, scope=Scope.TARGET, rejected_attempts_reset=False))
    out2 = evaluate(
        [_cand(ts0, sender="U_A", targets=("U_T",)), _cand(ts1, sender="U_B", targets=("U_T",))],
        DatedRules(entries=(target_rule,)), roster, frozenset(), (_SEM,), _TZ,
    )
    assert out2[0].pairs[0].status is Status.COUNTED
    assert out2[1].pairs[0].status is Status.COOLDOWN   # TARGET scope -> any sender counts


def test_cooldown_backstop_rejected_attempts_reset_extends_and_reports_blocker() -> None:
    """L4-MU-backstop-cooldown-reset. Mutants 125 (`anchor[key]=None` on
    reset, silently clearing the cooldown instead of restarting it) and 126
    (`anchor_ts[key]=None` on reset, breaking `blocked_by` reporting): with
    rejected_attempts_reset=True a rejection must restart the clock and the
    next attempt's blocked_by must name the actual blocking attempt."""
    roster = _roster({"U_S": None, "U_T": None})
    rule = _rule(cooldown=CooldownRule(
        microseconds=15 * US_PER_MINUTE, scope=Scope.PAIR, rejected_attempts_reset=True))
    ts0 = "1767225600.000000"   # admitted
    ts1 = "1767225900.000000"   # +5min: rejected, resets anchor to ts1
    ts2 = "1767226200.000000"   # +5min more: still within 15min of ts1 -> rejected
    out = evaluate(
        [_cand(ts0, targets=("U_T",)), _cand(ts1, targets=("U_T",)), _cand(ts2, targets=("U_T",))],
        DatedRules(entries=(rule,)), roster, frozenset(), (_SEM,), _TZ,
    )
    assert out[0].pairs[0].status is Status.COUNTED
    assert out[1].pairs[0].status is Status.COOLDOWN
    assert out[2].pairs[0].status is Status.COOLDOWN
    assert out[2].pairs[0].blocked_by == ts1


def test_day_cap_backstop_same_day_boundary() -> None:
    """L4-MU-backstop-day-cap-same-day. Mutants 128 (cap forced None), 135
    (day_counted missing-default 0->1), 136 (`+1`->`-1`), 137 (`+1`->`+2`)
    and 138 (day_counted forced None): with cap=2 and three same-day snipes
    on one target, exactly the first two may count."""
    roster = _roster({"U_S": None, "U_T": None})
    rule = _rule(
        cooldown=CooldownRule(microseconds=0, scope=Scope.PAIR, rejected_attempts_reset=False),
        max_snipes_per_target_per_day=2,
    )
    ts_a, ts_b, ts_c = "1767225600.000000", "1767225660.000000", "1767225720.000000"
    out = evaluate(
        [_cand(ts_a, targets=("U_T",)), _cand(ts_b, targets=("U_T",)), _cand(ts_c, targets=("U_T",))],
        DatedRules(entries=(rule,)), roster, frozenset(), (_SEM,), _TZ,
    )
    assert [o.pairs[0].status for o in out] == [Status.COUNTED, Status.COUNTED, Status.NOT_COUNTED]
    assert out[2].pairs[0].reason is Reason.DAILY_CAP


def test_day_cap_backstop_resets_next_day() -> None:
    """L4-MU-backstop-day-cap-reset. Mutant 127 (`day = None` instead of
    `_local_date_str(...)`): with cap=1, a same-target snipe on a LATER
    calendar day must still count, not compound into a lifetime cap."""
    roster = _roster({"U_S": None, "U_T": None})
    rule = _rule(
        cooldown=CooldownRule(microseconds=0, scope=Scope.PAIR, rejected_attempts_reset=False),
        max_snipes_per_target_per_day=1,
    )
    ts_day1 = "1767225600.000000"   # 2026-01-01 00:00:00 UTC
    ts_day2 = "1767312030.000000"   # 2026-01-02 00:00:30 UTC
    out = evaluate(
        [_cand(ts_day1, targets=("U_T",)), _cand(ts_day2, targets=("U_T",))],
        DatedRules(entries=(rule,)), roster, frozenset(), (_SEM,), _TZ,
    )
    assert [o.pairs[0].status for o in out] == [Status.COUNTED, Status.COUNTED]


def test_live_count_backstop_video_and_linked_terms_add() -> None:
    """L4-MU-backstop-live-count-signs. Mutants 149 (video term negated) and
    151 (linked-image term negated): both optional terms must ADD to
    live_images, or a message with real live content wrongly gates as
    NO_LIVE_IMAGE whenever the terms happen to cancel out."""
    rule = _rule(allow_video=True, count_image_links=True)
    row = _cand("1758210000.000000", targets=("U_T",), live_images=0, live_videos=2, linked_images=2)
    v = _only(row, _roster({"U_S": None, "U_T": None}), rule)
    assert v.status is Status.COUNTED


def test_selfie_backstop_sib_tag_requires_group_match() -> None:
    """L4-MU-backstop-sib-tag-match. Mutants 93, 146 and 147 each loosen a
    different `and` in the sib_tagged/selfie-gate chain to `or`: a sender
    with a sibling group sniping a target in a DIFFERENT group must not be
    treated as sib-tagged."""
    row = _cand("1758210000.000000", targets=("U_T",))
    v = _only(row, _roster({"U_S": "sibA", "U_T": "sibB"}))
    assert v.status is Status.COUNTED
    assert v.selfie is SelfieClass.NOT_APPLICABLE
    assert v.pairs[0].selfie is False


def test_selfie_backstop_snipe_requires_face_match() -> None:
    """L4-MU-backstop-selfie-snipe-match. Mutant 98 (`is not None`->`is None`
    in the SNIPE branch) makes an exact face-count match impossible, so it
    falls through to AMBIGUOUS instead of SNIPE."""
    row = _cand(
        "1758210000.000000", targets=("U_T",),
        live_image_ids=("F0",), face_counts={"F0": 1},   # 1 target, 1 face -> SNIPE
    )
    v = _only(row, _roster({"U_S": "sibA", "U_T": "sibA"}))
    assert v.status is Status.COUNTED
    assert v.selfie is SelfieClass.SNIPE


def test_target_gate_backstop_bot_reason() -> None:
    """L4-MU-backstop-target-bot. Mutant 195 flips `not rule.allow_bots` to
    `rule.allow_bots`; mutant 197 nulls the assigned reason. Either way a bot
    target under allow_bots=False must be gated TARGET_IS_BOT."""
    roster = Roster(
        entries={
            "U_S": RosterEntry(user="U_S", join_us=0, group=None, is_bot=False),
            "U_BOT": RosterEntry(user="U_BOT", join_us=0, group=None, is_bot=True),
        },
        count_intra_group=True,
    )
    row = _cand("1758210000.000000", targets=("U_BOT",))
    v = _only(row, roster, _rule(allow_bots=False))
    assert v.pairs[0].reason is Reason.TARGET_IS_BOT


def test_remember_hashes_backstop_out_of_season_with_hash() -> None:
    """L4-MU-backstop-remember-hashes. Mutant 108 (`or`->`and` in
    `_remember_hashes`'s early return): a message with a rendition hash but
    no in-force semester must not crash reading `sem.name` off a None
    semester."""
    roster = _roster({"U_S": None, "U_T": None})
    out_of_season_sem = Semester(name="other", start_us=10 ** 18, end_us=10 ** 18 + 1)
    row = _cand("1758210000.000000", targets=("U_T",), rendition_hash={"F0": "deadbeef"})
    out = evaluate([row], DatedRules(entries=(_rule(),)), roster, frozenset(), (out_of_season_sem,), _TZ)
    assert out[0].status is Status.NOT_COUNTED
    assert out[0].reason is Reason.OUT_OF_SEASON


def test_out_of_season_backstop_gate_applies() -> None:
    """L4-MU-backstop-out-of-season. Mutant 162 nulls the OUT_OF_SEASON gate
    assignment, letting an out-of-season message fall through ungated."""
    roster = _roster({"U_S": None, "U_T": None})
    out_of_season_sem = Semester(name="other", start_us=10 ** 18, end_us=10 ** 18 + 1)
    row = _cand("1758210000.000000", targets=("U_T",))
    out = evaluate([row], DatedRules(entries=(_rule(),)), roster, frozenset(), (out_of_season_sem,), _TZ)
    assert out[0].status is Status.NOT_COUNTED
    assert out[0].reason is Reason.OUT_OF_SEASON


def test_deleted_backstop_gate_applies() -> None:
    """L4-MU-backstop-deleted. Mutant 156 nulls the DELETED gate assignment,
    letting a deleted message fall through and get processed normally."""
    row = _cand("1758210000.000000", targets=("U_T",), missing_runs=2)
    v = _only(row, _roster({"U_S": None, "U_T": None}))
    assert v.status is Status.NOT_COUNTED
    assert v.reason is Reason.DELETED


def test_max_targets_backstop_boundary_not_gated() -> None:
    """L4-MU-backstop-max-targets-boundary. Mutant 171 (`>`->`>=`): exactly
    max_targets_per_message targets must NOT trigger TOO_MANY_TARGETS."""
    row = _cand("1758210000.000000", targets=("U_T1", "U_T2"))
    v = _only(row, _roster({"U_S": None, "U_T1": None, "U_T2": None}), _rule(max_targets_per_message=2))
    assert v.status is Status.COUNTED
    assert v.reason is Reason.COUNTED


def test_max_targets_backstop_over_limit_gated() -> None:
    """L4-MU-backstop-max-targets-gate. Mutant 173 nulls the
    TOO_MANY_TARGETS gate assignment, letting an over-limit message fall
    through ungated."""
    row = _cand("1758210000.000000", targets=("U_T1", "U_T2", "U_T3"))
    v = _only(
        row, _roster({"U_S": None, "U_T1": None, "U_T2": None, "U_T3": None}),
        _rule(max_targets_per_message=1),
    )
    assert v.status is Status.NOT_COUNTED
    assert v.reason is Reason.TOO_MANY_TARGETS
    assert v.pairs == ()


def test_vetoes_backstop_single_veto_gates() -> None:
    """L4-MU-backstop-vetoes-boundary. Mutant 175 (`>0`->`>1`): exactly one
    veto must still gate VETOED."""
    row = _cand("1758210000.000000", targets=("U_T",), vetoes=(Veto(by="U_MOD", source=VetoSource.CLI),))
    v = _only(row, _roster({"U_S": None, "U_T": None}))
    assert v.status is Status.NOT_COUNTED
    assert v.reason is Reason.VETOED


def test_untagged_backstop_does_not_abort_remaining_facts() -> None:
    """L4-MU-backstop-untagged-continue. Mutant 184 (`continue`->`break`): an
    untagged message must not stop the whole sweep from processing every
    later message."""
    roster = _roster({"U_S": None, "U_T": None})
    untagged = _cand("1758210000.000000", targets=())
    later = _cand("1758210060.000000", targets=("U_T",))
    out = evaluate([untagged, later], DatedRules(entries=(_rule(),)), roster, frozenset(), (_SEM,), _TZ)
    assert len(out) == 2
    assert out[0].status is Status.UNTAGGED
    assert out[1].status is Status.COUNTED


def test_self_snipe_backstop_gated_when_disallowed() -> None:
    """L4-MU-backstop-self-snipe-flip. Mutant 188 flips `not rule.allow_self`
    to `rule.allow_self`: with allow_self=False a self-mention must still be
    gated SELF_SNIPE."""
    row = _cand("1758210000.000000", sender="U_S", targets=("U_S",))
    v = _only(row, _roster({"U_S": None}), _rule(allow_self=False))
    assert v.pairs[0].reason is Reason.SELF_SNIPE


def test_target_gate_backstop_off_roster_reason() -> None:
    """L4-MU-backstop-target-off-roster. Mutant 194 nulls the assigned
    reason, so an off-roster target's pair reason silently becomes None."""
    row = _cand("1758210000.000000", targets=("U_GHOST",))
    v = _only(row, _roster({"U_S": None}))
    assert v.pairs[0].reason is Reason.TARGET_OFF_ROSTER


def test_multi_tag_single_backstop_picks_first_eligible() -> None:
    """L4-MU-backstop-multi-tag-single. Mutant 202 (`eligible[0]`->
    `eligible[1]`): under multi_tag=SINGLE the FIRST mentioned eligible
    target must be the one swept; the rest fold."""
    row = _cand("1758210000.000000", targets=("U_T1", "U_T2"))
    v = _only(
        row, _roster({"U_S": None, "U_T1": None, "U_T2": None}),
        _rule(multi_tag=MultiTag.SINGLE),
    )
    assert v.pairs[0].target == "U_T1"
    assert v.pairs[0].status is Status.COUNTED
    assert v.pairs[0].reason is Reason.COUNTED
    assert v.pairs[1].target == "U_T2"
    assert v.pairs[1].reason is Reason.MULTI_TAG_FOLDED


def test_dominant_reason_backstop_ranked_not_mention_order() -> None:
    """L4-MU-backstop-dominant-reason. Mutants 217 (rank lambda -> None),
    218 (m_reason -> None) and 77 (_TARGET_REASON_RANK -> None): the
    message-level reason on an all-NOT_COUNTED message must be the
    highest-precedence target reason, not whichever is mentioned first."""
    row = _cand("1758210000.000000", sender="U_S", targets=("U_OPT", "U_S"))
    v = _only(
        row, _roster({"U_S": None, "U_OPT": None}), _rule(allow_self=False),
        opted_out=frozenset({"U_OPT"}),
    )
    assert v.status is Status.NOT_COUNTED
    assert v.reason is Reason.SELF_SNIPE


def test_selfie_flag_backstop_requires_pair_group_match() -> None:
    """L4-MU-backstop-selfie-pair-flag. Mutant 224 (first `and`->`or`): the
    per-pair selfie flag must require THIS pair's target to share the
    sender's group, not just that the message classified as SELFIE."""
    row = _cand(
        "1758210000.000000", sender="U_S", targets=("U_SIB", "U_OTHER"),
        live_image_ids=("F0",), face_counts={"F0": 3},   # 2 targets, +1 face -> SELFIE
    )
    roster = _roster({"U_S": "sibA", "U_SIB": "sibA", "U_OTHER": "sibB"})
    v = _only(row, roster)
    assert v.selfie is SelfieClass.SELFIE
    assert v.status is Status.COUNTED
    sib_pair = next(p for p in v.pairs if p.target == "U_SIB")
    other_pair = next(p for p in v.pairs if p.target == "U_OTHER")
    assert sib_pair.selfie is True
    assert other_pair.selfie is False


# --------------------------------------------------------------------------- #
# ts.py: two of its three Run-2 survivors are exception-message-text mutants
# ("XX...XX" wraps); nothing else in this runner's scope inspects the literal
# text, so pin it directly. (The third, `Ts = int` -> `Ts = None`, is an
# equivalent mutant — see tests/mutation/survivors.md.)
# --------------------------------------------------------------------------- #

def test_parse_ts_backstop_error_message() -> None:
    """L4-MU-backstop-parse-ts-message. Mutant 236 wraps the TsFormatError
    message in "XX...XX"."""
    with pytest.raises(TsFormatError, match=r"^not a Slack ts: 'garbage'$"):
        parse_ts("garbage")


def test_format_ts_backstop_error_message() -> None:
    """L4-MU-backstop-format-ts-message. Mutant 243 wraps the ValueError
    message in "XX...XX"."""
    with pytest.raises(ValueError, match=r"^ts microseconds must be non-negative: -1$"):
        format_ts(-1)


# --------------------------------------------------------------------------- #
# parse.py (survivors.md "Run: snipebot/parse.py"): 86 survivors, most of them
# enum/dict/tuple literal or dataclass-contract mutations invisible to
# equality-only assertions, closed the same way as rules.py's enum/frozen
# backstops above.
# --------------------------------------------------------------------------- #

def test_veto_source_backstop_values() -> None:
    """L4-MU-backstop-veto-source-values. Mutants 247, 248, 249, 250."""
    assert {m.name: m.value for m in VetoSource} == {"REACTION": "reaction", "CLI": "cli"}


def test_parse_dataclasses_backstop_are_frozen() -> None:
    """L4-MU-backstop-parse-frozen. Mutants 251 (Veto), 253 (SelfieOverride),
    255 (TargetEdit), 257 (Candidate), 285 (Digest), 287 (DigestMetadata)."""
    v = Veto(by="U1", source=VetoSource.CLI)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.by = "U2"  # type: ignore[misc]

    so = SelfieOverride(value=True, by="U1", source=VetoSource.CLI)
    with pytest.raises(dataclasses.FrozenInstanceError):
        so.value = False  # type: ignore[misc]

    te = TargetEdit(user="U1", edit_ts=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        te.user = "U2"  # type: ignore[misc]

    c = _cand("1758210000.000000")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.sender = "U2"  # type: ignore[misc]

    d = Digest(ts="1.000000", channel="C1", report="daily", period_key="k",
               semester="F26", numbers_hash="h", revision=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.channel = "C2"  # type: ignore[misc]

    dm = DigestMetadata(event_type="snipe_digest", report="daily", period_key="k",
                         channel="C1", semester="F26", numbers_hash="h", revision=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        dm.channel = "C2"  # type: ignore[misc]


def test_candidate_backstop_field_defaults() -> None:
    """L4-MU-backstop-candidate-defaults. Mutants 259, 261, 262, 265, 268:
    the optional/faces-related fields' actual defaults, bypassing the test
    helper (which does not set any of these itself)."""
    c = _cand("1758210000.000000")
    assert c.face_counts == {}
    assert c.rendition_hash == {}
    assert c.detect_attempts == 0
    assert c.selfie_override is None
    assert c.has_file_object is False


def test_candidate_backstop_has_file_object_not_compared() -> None:
    """L4-MU-backstop-has-file-object-compare. Mutant 266 (compare=False->True):
    two Candidates differing ONLY in has_file_object must still be equal."""
    c1 = _cand("1758210000.000000", has_file_object=True)
    c2 = _cand("1758210000.000000", has_file_object=False)
    assert c1 == c2


def test_candidate_backstop_has_file_object_not_in_repr() -> None:
    """L4-MU-backstop-has-file-object-repr. Mutant 267 (repr=False->True)."""
    c = _cand("1758210000.000000", has_file_object=True)
    assert "has_file_object" not in repr(c)


def test_file_sig_backstop_exact_hash() -> None:
    """L4-MU-backstop-file-sig-exact. Mutants 276-282: width/height's "" vs
    str(...) branches (both directions) and the payload f-string, pinned
    against an independently computed sha256 so any tampering with the
    payload bytes is caught."""
    expected = hashlib.sha256(b"a.png\x1f100\x1f10\x1f20").hexdigest()
    assert file_sig("a.png", 100, 10, 20) == expected
    expected_no_w = hashlib.sha256(b"a.png\x1f100\x1f\x1f20").hexdigest()
    assert file_sig("a.png", 100, None, 20) == expected_no_w
    expected_no_h = hashlib.sha256(b"a.png\x1f100\x1f10\x1f").hexdigest()
    assert file_sig("a.png", 100, 10, None) == expected_no_h


def test_digest_metadata_backstop_to_wire_exact_shape() -> None:
    """L4-MU-backstop-to-wire-shape. Mutants 289-297: every dict key/value
    literal in `to_wire`'s wire-format dict."""
    dm = DigestMetadata(event_type="snipe_digest", report="daily",
                         period_key="daily:2026-09-18", channel="C_ORIG",
                         semester="F26", numbers_hash="deadbeef", revision=2)
    assert dm.to_wire("C_POST") == {
        "event_type": "snipe_digest",
        "event_payload": {
            "report": "daily",
            "period_key": "daily:2026-09-18",
            "channel": "C_POST",
            "semester": "F26",
            "numbers_hash": "deadbeef",
            "revision": 2,
        },
    }


def test_digest_metadata_backstop_from_wire_is_static() -> None:
    """L4-MU-backstop-from-wire-static. Mutant 298 (`@staticmethod` removed):
    calling `from_wire` off an INSTANCE (not just the class) must not
    implicitly prepend `self`, which would misalign the (channel, wire)
    arguments and raise a TypeError."""
    dm = DigestMetadata(event_type="snipe_digest", report="daily", period_key="k",
                         channel="C1", semester="F26", numbers_hash="h", revision=0)
    wire = dm.to_wire("C2")
    result = dm.from_wire("C2", wire)   # called off an instance, not the class
    assert result.channel == "C2"
    assert result.report == "daily"


def test_rendition_keys_backstop_values() -> None:
    """L4-MU-backstop-rendition-keys. Mutants 314, 315, 316."""
    assert _RENDITION_KEYS == ("thumb_1024", "thumb_960", "thumb_720", "thumb_480", "url_private_download")


def test_is_present_backstop_hidden_by_limit() -> None:
    """L4-MU-backstop-is-present-hidden. Mutants 325 (`mode` key wrong) and
    327 (`hidden_by_limit` value wrong)."""
    assert _is_present({"mode": "hidden_by_limit"}) is False
    assert _is_present({}) is True


def test_strip_code_backstop_removes_not_replaces() -> None:
    """L4-MU-backstop-strip-code. Mutants 340 (fenced sub replacement
    ""->"XX") and 342 (inline sub ""->"XX")."""
    assert _strip_code("a ```code<@U1>``` b `inline<@U2>` c") == "a  b  c"


def test_block_mentions_backstop_walk_and_gate() -> None:
    """L4-MU-backstop-block-mentions. Mutants 350-355 (the per-node gate:
    dict-key literals, `==`/`!=`, `in`/`not in`, `and`/`or`) and 357-359
    (`walk(value)`/`walk(item)`/`walk(blocks)` each replaced with
    `walk(None)`, silently pruning the recursive tree walk)."""
    blocks = [
        {"type": "user", "user_id": "U1"},
        {"type": "user"},                        # no user_id -> not extracted
        {"type": "channel", "user_id": "U2"},     # wrong type -> not extracted
        {"type": "rich_text_section", "elements": [
            {"type": "text", "text": "hi"},
            {"type": "user", "user_id": "U3"},
        ]},
    ]
    assert _block_mentions(blocks) == ["U1", "U3"]


def test_parse_backstop_digest_bot_warning() -> None:
    """L4-MU-backstop-digest-bot-warn. Mutants 368-372 (the warn condition's
    dict keys / `not in`-`in` / `!=`-`==` / `and`-`or`) and 373-375 (the
    warning's message text)."""
    base = {
        "ts": "1758210000.000000",
        "metadata": {"event_type": "snipe_digest", "event_payload": {
            "report": "daily", "period_key": "k", "channel": "C1",
            "semester": "F26", "numbers_hash": "h", "revision": 0,
        }},
    }

    with pytest.warns(ParseAnomaly, match=r"^digest not from bot ts=1758210000\.000000 user=REAL_USER$"):
        parse(dict(base, user="REAL_USER"), "C1", "BOT_ID")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parse(dict(base, bot_id="B1", user="REAL_USER"), "C1", "BOT_ID")
    assert not any(issubclass(w.category, ParseAnomaly) for w in caught)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parse(dict(base, user="BOT_ID"), "C1", "BOT_ID")
    assert not any(issubclass(w.category, ParseAnomaly) for w in caught)


def test_parse_backstop_digest_channel_mismatch_warning() -> None:
    """L4-MU-backstop-digest-channel-warn. Mutants 376, 378 (the warn
    condition's dict keys) and 380-383 (the warning's message text)."""
    mismatched = {
        "ts": "1758210000.000000", "user": "BOT_ID",
        "metadata": {"event_type": "snipe_digest", "event_payload": {
            "report": "daily", "period_key": "k", "channel": "C_WRONG",
            "semester": "F26", "numbers_hash": "h", "revision": 0,
        }},
    }
    with pytest.warns(
        ParseAnomaly,
        match=r"^digest channel mismatch ts=1758210000\.000000 found=C1 payload=C_WRONG$",
    ):
        parse(mismatched, "C1", "BOT_ID")

    matched = {
        "ts": "1758210000.000000", "user": "BOT_ID",
        "metadata": {"event_type": "snipe_digest", "event_payload": {
            "report": "daily", "period_key": "k", "channel": "C1",
            "semester": "F26", "numbers_hash": "h", "revision": 0,
        }},
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parse(matched, "C1", "BOT_ID")
    assert not any(issubclass(w.category, ParseAnomaly) for w in caught)


def test_parse_backstop_human_test_gate() -> None:
    """L4-MU-backstop-human-test. Mutants 386, 388, 390, 391 (dict key/value
    literals) and 393 (`or`->`and`/`or` precedence change) in the
    bot_id/subtype/user human-message filter."""
    base = {"ts": "1758210000.000000", "user": "U1", "text": "hi"}
    # A user-token post: bot_id next to a real user is still that user (10 §4 E-G2-1).
    assert parse(dict(base, bot_id="B1"), "C1", "BOT_ID") is not None
    assert parse(dict(base, subtype="bot_message"), "C1", "BOT_ID") is None
    assert parse(dict(base, user="BOT_ID"), "C1", "BOT_ID") is None
    assert parse({"ts": base["ts"], "text": "hi", "bot_id": "B1"}, "C1", "BOT_ID") is None
    assert parse(dict(base, user=""), "C1", "BOT_ID") is None
    assert parse(base, "C1", "BOT_ID") is not None


def test_parse_backstop_file_sigs_filter() -> None:
    """L4-MU-backstop-file-sigs-filter. Mutants 412, 413, 414, 416, 417, 418,
    419, 420, 421: the file_sigs list comprehension's dict keys, mimetype
    prefix, is_tombstoned check (`is not True` flipped three different
    ways) and the `and`->`or`, plus the whole expression replaced by None."""
    message = {
        "ts": "1758210000.000000", "user": "U1", "text": "hi",
        "files": [
            {"id": "F0A", "name": "a.png", "size": 100, "mimetype": "image/png",
             "original_w": 10, "original_h": 20, "is_tombstoned": False},
            {"id": "F0B", "name": "b.png", "size": 200, "mimetype": "image/png",
             "original_w": 5, "original_h": 5, "is_tombstoned": True},
            {"id": "F0C", "name": "c.txt", "size": 50, "mimetype": "text/plain"},
        ],
    }
    cand = parse(message, "C1", "BOT_ID")
    assert cand.file_sigs == (file_sig("a.png", 100, 10, 20),)


def test_parse_backstop_mention_disagreement_warning() -> None:
    """L4-MU-backstop-mention-disagree-warn. Mutants 430-432 (the warning's
    message text)."""
    message = {
        "ts": "1758210000.000000", "user": "U1", "text": "<@U1> hi",
        "blocks": [{"type": "rich_text_section", "elements": []}],
    }
    with pytest.warns(
        ParseAnomaly,
        match=r"^mention text/blocks disagree ts=1758210000\.000000 text=\['U1'\] blocks=\[\]$",
    ):
        parse(message, "C1", "BOT_ID")


def test_parse_backstop_missing_runs_default() -> None:
    """L4-MU-backstop-missing-runs-default. Mutant 439."""
    message = {"ts": "1758210000.000000", "user": "U1", "text": "hi"}
    cand = parse(message, "C1", "BOT_ID")
    assert cand.missing_runs == 0


# --------------------------------------------------------------------------- #
# aggregate.py (survivors.md "Run: snipebot/aggregate.py"): 53 real survivors
# (12 more ids were phantom cache rows with no real mutation, like rules.py's
# id 112 and parse.py's 263-264 -- see survivors.md).
# --------------------------------------------------------------------------- #

def test_ungrouped_backstop_value() -> None:
    """L4-MU-backstop-ungrouped-value. Mutants 442, 443."""
    assert UNGROUPED == "(ungrouped)"


def test_no_snipe_us_backstop_value() -> None:
    """L4-MU-backstop-no-snipe-us-value. Mutants 444 (`1<<62`->`2<<62`), 445
    (`->`1>>62`, i.e. 0) and 446 (`->`1<<63`)."""
    assert _NO_SNIPE_US == 1 << 62


def test_aggregate_dataclasses_backstop_are_frozen() -> None:
    """L4-MU-backstop-aggregate-frozen. Mutants 448 (EligibleSnipe), 450
    (RejectedAttempt), 452 (Eligibility), 487 (SnipeRow), 491 (DailyRow), 507
    (PersonRow), 526 (GroupRow), 598 (MostSnipedRow), 630 (PairRow), 659
    (TopNCut)."""
    from snipebot.aggregate import (
        DailyRow, MostSnipedRow, PairRow, PersonRow, RejectedAttempt, SnipeRow, TopNCut,
    )

    es = EligibleSnipe(ts="1.000000", ts_us=1_000_000, date="d", time="t",
                        sniper="A", target="B", sniper_group=None, target_group=None, selfie=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        es.sniper = "C"  # type: ignore[misc]

    ra = RejectedAttempt(ts="1.000000", ts_us=1_000_000, date="d", sniper="A", target="B", blocked_by="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ra.sniper = "C"  # type: ignore[misc]

    e = Eligibility(semester=_SEM, snipes=(), rejections=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.snipes = ()  # type: ignore[misc]

    sr = SnipeRow(ts_us=1, date="d", time="t", sniper="A", target="B", sniper_group="g", target_group="g")
    with pytest.raises(dataclasses.FrozenInstanceError):
        sr.sniper = "C"  # type: ignore[misc]

    dr = DailyRow(date="d", snipes=0, unique_snipers=0, unique_targets=0, cooldown_rejections=0, points=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        dr.snipes = 1  # type: ignore[misc]

    pr = PersonRow(person="A", group="g", snipes_made=0, times_sniped=0, unique_targets=0,
                    unique_snipers=0, best_day=None, points=0, first_snipe_us=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        pr.points = 1  # type: ignore[misc]

    gr = GroupRow(group="g", members=0, made=0, sniped=0, made_num=0, sniped_num=0,
                  intra_group=0, cross_group=0, points=0, points_num=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        gr.points = 1  # type: ignore[misc]

    msr = MostSnipedRow(rank=1, person="A", group="g", times_sniped=0, top_sniper_of_them="B", first_sniped_us=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        msr.rank = 2  # type: ignore[misc]

    pair = PairRow(sniper="A", target="B", count=0, points=0, first_us=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        pair.count = 1  # type: ignore[misc]

    cut = TopNCut(head=(), boundary_tie=(), cutoff_value=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cut.head = (1,)  # type: ignore[misc]


def test_per_member_backstop_zero_members_sentinel() -> None:
    """L4-MU-backstop-per-member-sentinel. Mutants 543 (`Fraction(-1)`->`(+1)`),
    544 (`->`(-2)`), 545 (`==`->`!=`, ZeroDivisionError) and 546 (`==0`->`==1`,
    also ZeroDivisionError)."""
    assert _per_member(999, 0) == Fraction(-1)
    assert _per_member(999, 0) < _per_member(0, 1)
    assert _per_member(5, 1) == Fraction(5, 1)


def test_people_table_backstop_times_sniped_tiebreak() -> None:
    """L4-MU-backstop-people-times-sniped. Mutant 524 (`-r.times_sniped`->
    `+r.times_sniped`): with tied snipes_made and first_snipe_us, more
    times_sniped must rank FIRST."""
    roster = Roster(entries={
        "P1": RosterEntry(user="P1", join_us=0, group=None, is_bot=False),
        "P2": RosterEntry(user="P2", join_us=0, group=None, is_bot=False),
    }, count_intra_group=True)
    common_ts = 1_000_000
    snipes = (
        EligibleSnipe(ts="1.000000", ts_us=common_ts, date="2026-01-01", time="00:00:01",
                      sniper="P1", target="X1", sniper_group=None, target_group=None, selfie=False),
        EligibleSnipe(ts="2.000000", ts_us=common_ts, date="2026-01-01", time="00:00:02",
                      sniper="P2", target="X2", sniper_group=None, target_group=None, selfie=False),
        EligibleSnipe(ts="3.000000", ts_us=2_000_000, date="2026-01-01", time="00:00:03",
                      sniper="Y1", target="P1", sniper_group=None, target_group=None, selfie=False),
        EligibleSnipe(ts="4.000000", ts_us=3_000_000, date="2026-01-01", time="00:00:04",
                      sniper="Y2", target="P1", sniper_group=None, target_group=None, selfie=False),
    )
    elig = Eligibility(semester=_SEM, snipes=snipes, rejections=())
    rows = build_people_table(elig, roster)
    order = [r.person for r in rows if r.person in ("P1", "P2")]
    assert order.index("P1") < order.index("P2")


def test_groups_table_backstop_cross_group_only() -> None:
    """L4-MU-backstop-cross-group. Mutant 574 (`!=`->`==` on sniper_group):
    in count_intra_group=False mode, `sniped` must count only CROSS-group
    receipts, not intra-group ones."""
    roster = Roster(entries={
        "M1": RosterEntry(user="M1", join_us=0, group="G1", is_bot=False),
        "M2": RosterEntry(user="M2", join_us=0, group="G1", is_bot=False),
    }, count_intra_group=False)

    def es(i, sender, sg):
        return EligibleSnipe(ts=f"{i}.000000", ts_us=i * 1_000_000, date="2026-01-01",
                              time="00:00:00", sniper=sender, target="M1",
                              sniper_group=sg, target_group="G1", selfie=False)

    snipes = (es(1, "X1", "OTHER"), es(2, "X2", "OTHER"), es(3, "M2", "G1"))
    elig = Eligibility(semester=_SEM, snipes=snipes, rejections=())
    rows = build_groups_table(elig, roster, frozenset())
    g1 = next(r for r in rows if r.group == "G1")
    assert g1.sniped == 2


def test_build_groups_table_backstop_touches_gate() -> None:
    """L4-MU-backstop-groups-touches. Mutants 583 (`>0`->`>=0`, trivially
    true), 584 (`>0`->`>1`, boundary), 585 (first `or`->`and`) and 588
    (`continue`->`break`, which can abort the whole group sweep depending on
    set-iteration order across the 5 decoy "not touching" groups below)."""
    roster = Roster(
        entries={
            **{f"M{i}": RosterEntry(user=f"M{i}", join_us=0, group=f"sibT{i}", is_bot=False) for i in range(5)},
            **{f"O{i}": RosterEntry(user=f"O{i}", join_us=0, group=f"sibN{i}", is_bot=False) for i in range(5)},
            "U1": RosterEntry(user="U1", join_us=0, group="sibW", is_bot=False),
        },
        count_intra_group=True,
    )
    opted_out = frozenset({f"O{i}" for i in range(5)} | {"U1"})
    snipe = EligibleSnipe(ts="1.000000", ts_us=1_000_000, date="2026-01-01", time="00:00:01",
                           sniper="U1", target="M0", sniper_group="sibW", target_group="sibT0", selfie=False)
    elig = Eligibility(semester=_SEM, snipes=(snipe,), rejections=())
    rows = build_groups_table(elig, roster, opted_out)
    groups = {r.group for r in rows}
    assert groups == {f"sibT{i}" for i in range(5)} | {"sibW"}


def test_build_groups_table_backstop_sort_key() -> None:
    """L4-MU-backstop-groups-sort-key. Mutants 590 (`-per_member(made)`->
    `+`), 591 (`-r.points`->`+`) and 592 (`-r.made`->`+`) in
    build_groups_table's OWN internal sort_key (distinct from rank_groups,
    tested separately below)."""
    roster = Roster(
        entries={
            "H": RosterEntry(user="H", join_us=0, group="HI", is_bot=False),
            "L": RosterEntry(user="L", join_us=0, group="LO", is_bot=False),
            "A1": RosterEntry(user="A1", join_us=0, group="PA", is_bot=False),
            "B1": RosterEntry(user="B1", join_us=0, group="PB", is_bot=False),
            "C1": RosterEntry(user="C1", join_us=0, group="PC", is_bot=False),
            "D1": RosterEntry(user="D1", join_us=0, group="PD", is_bot=False),
        },
        count_intra_group=True,
    )
    opted_out = frozenset({"A1", "B1", "C1", "D1"})

    def es(i, sender, target, sg, tg, selfie):
        return EligibleSnipe(ts=f"{i}.000000", ts_us=i * 1_000_000, date="2026-01-01",
                              time="00:00:00", sniper=sender, target=target,
                              sniper_group=sg, target_group=tg, selfie=selfie)

    snipes = (
        es(1, "H", "T", "HI", "OTHER", False),        # HI: made=1, points=1 (plain)
        es(2, "Z0", "L", "OTHER", "LO", True),         # LO: made=0, points=1 (participation)
        es(3, "Z1", "A1", "OTHER", "PA", True),
        es(4, "Z2", "A1", "OTHER", "PA", True),
        es(5, "Z3", "A1", "OTHER", "PA", True),        # PA: made=0, points=3 (x3 participation)
        es(6, "Z4", "B1", "OTHER", "PB", True),        # PB: made=0, points=1 (participation)
        es(7, "Z5", "C1", "OTHER", "PC", True),        # PC: made=0, points=1 (participation)
        es(8, "Z6", "D1", "OTHER", "PD", True),        # PD: participation, made=0 so far
        es(9, "GHOST", "T2", "PD", "OTHER", False),    # PD extra made (sniper not on roster): made=1, points unchanged
    )
    elig = Eligibility(semester=_SEM, snipes=snipes, rejections=())
    rows = build_groups_table(elig, roster, opted_out)
    order = [r.group for r in rows]
    assert order.index("HI") < order.index("LO")   # made_per_member desc (points_per_member tied at 1)
    assert order.index("PA") < order.index("PB")    # raw points desc (per_member tied at -1 for both)
    assert order.index("PD") < order.index("PC")    # raw made desc (per_member and points both tied)


def test_rank_groups_backstop_full_order() -> None:
    """L4-MU-backstop-rank-groups. Mutants 649 (UNGROUPED filter `!=`->`==`),
    650-653 (the four sort-key terms' signs flipped), 654 (key->None,
    leaving `real` unsorted), 655 (`real`->None, crashes), 656-657 (the
    ungrouped filter flipped / nulled) and 658 (`+`->`-` combining the two
    tuples, crashes -- tuples do not support subtraction)."""
    def gr(group, members, made_num, points_num, points, made):
        return GroupRow(group=group, members=members, made=made, sniped=0,
                         made_num=made_num, sniped_num=0, intra_group=0,
                         cross_group=0, points=points, points_num=points_num)

    x1 = gr("X1", 1, 0, 5, 0, 0)
    x2 = gr("X2", 1, 0, 3, 0, 0)
    y1 = gr("Y1", 1, 5, 0, 0, 0)
    y2 = gr("Y2", 1, 2, 0, 0, 0)
    z1 = gr("Z1", 0, 0, 0, 10, 0)
    z2 = gr("Z2", 0, 0, 0, 3, 0)
    w1 = gr("W1", 0, 0, 0, 0, 10)
    w2 = gr("W2", 0, 0, 0, 0, 4)
    ungrouped_row = gr(UNGROUPED, 999, 999, 999, 999, 999)

    # Deliberately scrambled input order, so a no-op sort (mutant 654) cannot
    # coincidentally match the expected order below.
    rows = (y1, z2, x1, w1, ungrouped_row, x2, y2, w2, z1)
    ranked = rank_groups(rows)
    assert [r.group for r in ranked] == ["X1", "X2", "Y1", "Y2", "Z1", "Z2", "W1", "W2", UNGROUPED]


def test_top_sniper_of_backstop_picks_highest_count() -> None:
    """L4-MU-backstop-top-sniper-of. Mutant 602 (`!=`->`==`, inverting the
    target filter), 605 (`+1`->`-1`, inverting the ranking), 608
    (`earliest[...]=None`, crashes comparing `None < None` on a count tie)
    and 609 (`-counts[u]`->`+counts[u]`, inverting the ranking). (Mutants
    604 and 606 -- a uniform +1 default shift and a uniform x2 scale on the
    same increment -- are equivalent: both preserve every relative count
    ordering and every tie, see survivors.md.)"""
    def s(i, sniper, target, ts_us):
        return EligibleSnipe(ts=f"{i}.000000", ts_us=ts_us, date="d", time="t",
                              sniper=sniper, target=target, sniper_group=None,
                              target_group=None, selfie=False)

    # X snipes T three times, Y snipes T once: X must win (highest count).
    clear = (s(1, "X", "T", 10), s(2, "X", "T", 20), s(3, "X", "T", 30), s(4, "Y", "T", 1))
    assert _top_sniper_of("T", clear) == "X"

    # A tie in count (1 each): earliest ts must break it.
    tie = (s(5, "A", "T2", 200), s(6, "B", "T2", 100))
    assert _top_sniper_of("T2", tie) == "B"


def test_most_sniped_table_backstop_final_tiebreak() -> None:
    """L4-MU-backstop-most-sniped-tiebreak. Mutant 615 (`kv[0]`->`kv[1]`):
    a three-way tie on both count and earliest ts must fall back to the
    TARGET's own ID (`kv[0]`), not the (unorderable) list of snipes
    (`kv[1]`, which would raise a TypeError when compared)."""
    def s(i, target, ts_us):
        return EligibleSnipe(ts=f"{i}.000000", ts_us=ts_us, date="d", time="t",
                              sniper=f"S{i}", target=target, sniper_group=None,
                              target_group=None, selfie=False)

    snipes = (s(1, "Z", 100), s(2, "A", 100))   # tied count=1, tied earliest ts=100
    elig = Eligibility(semester=_SEM, snipes=snipes, rejections=())
    roster = Roster(entries={}, count_intra_group=True)
    rows = build_most_sniped_table(elig, roster)
    assert [r.person for r in rows] == ["A", "Z"]


def test_rank_pairs_backstop_order() -> None:
    """L4-MU-backstop-rank-pairs. Mutant 647 (`-r.count`->`+r.count`)."""
    from snipebot.aggregate import PairRow
    rows = (
        PairRow(sniper="A", target="B", count=1, points=1, first_us=0),
        PairRow(sniper="C", target="D", count=5, points=5, first_us=0),
    )
    ranked = rank_pairs(rows)
    assert [r.sniper for r in ranked] == ["C", "A"]


def test_top_n_cutoff_backstop_boundary() -> None:
    """L4-MU-backstop-top-n-cutoff. Mutant 661 (`<=`->`<` at the no-overflow
    boundary: `cutoff_value` must be None when `len(rows)==top_n`) and 662
    (`rows[top_n-1]`->`rows[top_n+1]`, an off-by-two on the cutoff row)."""
    rows = [1, 2, 3]
    cut = top_n_cutoff(rows, top_n=3, metric=lambda r: r)
    assert cut.cutoff_value is None
    assert cut.head == (1, 2, 3)

    rows2 = [5, 4, 3, 2, 1]
    cut2 = top_n_cutoff(rows2, top_n=2, metric=lambda r: r)
    assert cut2.cutoff_value == 4
    assert cut2.head == (5, 4)
    assert cut2.boundary_tie == ()
