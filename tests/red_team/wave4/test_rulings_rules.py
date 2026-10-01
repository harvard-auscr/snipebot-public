"""Wave-4 rulings on the evaluate side (00-data sections 2 and 4).

E-W4-29: a self-tag is never a selfie target; neither the sib-tagged test nor the `T` of
the selfie classification counts a target equal to the sender.
E-W4-30: an opted-out target never makes a message sib-tagged.

Every case runs against `snipebot.rules.evaluate` and the independent oracle.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from snipebot import rules
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
from snipebot.rules import Reason, SelfieClass, Status
from snipebot.ts import US_PER_MINUTE

from tests.oracle import oracle

TZ = ZoneInfo("UTC")
BASE = 1_700_000_000
SEM = Semester(name="F26", start_us=0, end_us=4_000_000_000 * 1_000_000)

SNIPER = "U0AAA001"
SIB = "U0AAA002"
SIB2 = "U0AAA003"
OTHER = "U0AAA004"   # rostered, different group

IMPLS = pytest.mark.parametrize(
    "evaluate", [rules.evaluate, oracle.evaluate], ids=["rules", "oracle"]
)


def _ts(offset_s: int) -> str:
    return f"{BASE + offset_s}.000000"


def _rule(*, allow_self: bool = False) -> DatedRules:
    return DatedRules(entries=(ResolvedRule(
        effective_from_us=0,
        cooldown=CooldownRule(
            microseconds=15 * US_PER_MINUTE, scope=Scope.PAIR, rejected_attempts_reset=False,
        ),
        multi_tag=MultiTag.PER_TARGET,
        max_targets_per_message=None,
        edit_grace_us=10 * US_PER_MINUTE,
        max_snipes_per_target_per_day=None,
        allow_self=allow_self,
        allow_bots=False,
        count_thread_replies=False,
        count_image_links=False,
        allow_video=False,
        selfie_bonus=True,
    ),))


def _roster() -> Roster:
    groups = {SNIPER: "g1", SIB: "g1", SIB2: "g1", OTHER: "g2"}
    return Roster(
        entries={u: RosterEntry(user=u, join_us=0, group=g, is_bot=False)
                 for u, g in groups.items()},
        count_intra_group=True,
    )


def _cand(ts: str, targets: tuple[str, ...], counts: list[int],
          hashes: list[str] | None = None) -> Candidate:
    ids = tuple(f"F0FILE{i:03d}" for i in range(len(counts)))
    return Candidate(
        ts=ts, sender=SNIPER, subtype=None, thread_ts=None, targets=targets,
        live_images=max(1, len(ids)), live_image_ids=ids, live_videos=0, linked_images=0,
        last_edit_ts=None, file_sigs=(), vetoes=(), missing_runs=0,
        first_seen_targets=frozenset(targets), first_sight_edited=False,
        target_edited_in=(), face_counts=dict(zip(ids, counts)),
        rendition_hash=dict(zip(ids, hashes or [])),
    )


def _run(evaluate, cands, *, allow_self=False, opted=frozenset()):
    return evaluate(cands, _rule(allow_self=allow_self), _roster(), opted, (SEM,), TZ)


@IMPLS
def test_self_tag_alone_is_not_sib_tagged(evaluate):
    """E-W4-29: a sender tagging only themself (allow_self) is not sib-tagged, so the
    counted message is never selfie-classified."""
    (v,) = _run(evaluate, [_cand(_ts(0), (SNIPER,), [2])], allow_self=True)
    assert v.status is Status.COUNTED
    assert v.selfie is SelfieClass.NOT_APPLICABLE
    assert v.pairs[0].selfie is False


@IMPLS
def test_self_tag_alone_never_reposts(evaluate):
    """E-W4-29: two self-tag-only messages sharing rendition bytes are not sib-tagged,
    so the later one never hits the REPOST gate."""
    out = _run(evaluate, [
        _cand(_ts(0), (SNIPER,), [1], ["hA"]),
        _cand(_ts(3600), (SNIPER,), [1], ["hA"]),
    ], allow_self=True)
    assert [v.reason for v in out] == [Reason.COUNTED, Reason.COUNTED]


@IMPLS
@pytest.mark.parametrize("allow_self", [False, True])
def test_selfie_T_excludes_the_sender(evaluate, allow_self):
    """E-W4-29: tagging self plus one sib gives T = 1, so two faces is T + 1 (SELFIE)
    and one face is T (SNIPE)."""
    (v,) = _run(evaluate, [_cand(_ts(0), (SNIPER, SIB), [2])], allow_self=allow_self)
    assert v.selfie is SelfieClass.SELFIE
    by_target = {p.target: p for p in v.pairs}
    assert by_target[SIB].selfie is True
    if not allow_self:
        assert by_target[SNIPER].reason is Reason.SELF_SNIPE

    (v,) = _run(evaluate, [_cand(_ts(0), (SNIPER, SIB), [1])], allow_self=allow_self)
    assert v.selfie is SelfieClass.SNIPE


@IMPLS
def test_opted_out_sib_does_not_make_sib_tagged(evaluate):
    """E-W4-30: a counted message whose only sib target is opted out is not sib-tagged:
    it is never selfie-classified."""
    (v,) = _run(evaluate, [_cand(_ts(0), (SIB, OTHER), [3])], opted=frozenset({SIB}))
    assert v.status is Status.COUNTED
    assert v.selfie is SelfieClass.NOT_APPLICABLE
    assert {p.target: p.reason for p in v.pairs} == {
        SIB: Reason.TARGET_OPTED_OUT, OTHER: Reason.COUNTED,
    }


@IMPLS
def test_opted_out_sib_message_never_reposts(evaluate):
    """E-W4-30: a later message whose only sib target is opted out is not sib-tagged,
    so matching rendition bytes never make it a REPOST."""
    out = _run(evaluate, [
        _cand(_ts(0), (SIB2,), [1], ["hB"]),
        _cand(_ts(3600), (SIB, OTHER), [1], ["hB"]),
    ], opted=frozenset({SIB}))
    assert [v.reason for v in out] == [Reason.COUNTED, Reason.COUNTED]


@IMPLS
def test_opted_out_sib_beside_live_sib_still_sib_tagged(evaluate):
    """E-W4-30 boundary: another in-group target keeps the message sib-tagged; T still
    counts every non-sender target (two targets, three faces is SELFIE)."""
    (v,) = _run(evaluate, [_cand(_ts(0), (SIB, SIB2), [3])], opted=frozenset({SIB}))
    assert v.selfie is SelfieClass.SELFIE
    assert {p.target: p.selfie for p in v.pairs} == {SIB: False, SIB2: True}
