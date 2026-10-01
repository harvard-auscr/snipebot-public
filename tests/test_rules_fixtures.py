"""L1 rule-fixture tests (50 section 2.1): a real payload through `parse`, then
`evaluate` on that single row alone under the shipped default rules with both
endpoints rostered, asserting the message verdict the FX row states.

Where a section 2.1 row's twin flips one input (the `CT` rows, section 1.3), the
pair is asserted here too: for the roster gate the twins land in opposite
verdicts directly; for the veto pair the parse+evaluate verdict does not flip
under the default `by: [admins]` (the veto is assembled by `sync`, and the flip
is the audit reading — asserted in test_sync), so here both sides are COUNTED;
for the late-tag pair the edit-time evidence that makes a tag "late" is a `sync`
merge product, so the flip is shown at the pair level on the merged rows.

The three seam-level controls (50 section 1.3) target the `test_ctl_*` tests at
the foot of this module; `tests/test_controls_registry.py` drives them.
"""

from __future__ import annotations

import dataclasses
import json
import warnings
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

from snipebot import rules
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
from snipebot.parse import Candidate, Digest, TargetEdit, parse
from snipebot.rules import Reason, SelfieClass, Status
from snipebot.ts import US_PER_MINUTE, parse_ts

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
TZ = ZoneInfo("UTC")

# A semester wide enough to hold every fixture ts (all in September 2026).
SEM = Semester(name="F26", start_us=0, end_us=4_000_000_000 * 1_000_000)

# The legitimate roster: the sniper and the real-user targets, both endpoints of
# every FX candidate. U0AAA099 (the off-roster sender) is deliberately absent.
_LEGIT_USERS = ("U0AAA001", "U0AAA002", "U0AAA003", "U0AAA004", "U0AAA009", "U0AAA010")


# --------------------------------------------------------------------------- #
# Default rules / roster (the shipped defaults; 40-config section 2.3, config.py
# `_default_rule`). Built by hand, mirroring the tests/test_rules_unit.py helper.
# --------------------------------------------------------------------------- #

def _default_rule() -> ResolvedRule:
    return ResolvedRule(
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


def _rules() -> DatedRules:
    return DatedRules(entries=(_default_rule(),))


def _roster(members: dict[str, str | None]) -> Roster:
    """members: user -> group name (or None for an extra)."""
    return Roster(
        entries={
            u: RosterEntry(user=u, join_us=0, group=g, is_bot=False)
            for u, g in members.items()
        },
        count_intra_group=True,
    )


def _legit_roster() -> Roster:
    return _roster({u: None for u in _LEGIT_USERS})


def _evaluate(cand: Candidate, roster: Roster | None = None):
    return rules.evaluate(
        [cand], _rules(), roster or _legit_roster(), frozenset(), (SEM,), TZ
    )[0]


def _load(fixtures_dir: Path, *parts: str) -> dict:
    with fixtures_dir.joinpath(*parts).open(encoding="utf-8") as fh:
        data = json.load(fh)
    return data["messages"][0] if isinstance(data, dict) and "messages" in data else data


def _parse(msg: dict):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # ParseAnomaly is asserted in test_parse, not here
        return parse(msg, CHANNEL, BOT)


# --------------------------------------------------------------------------- #
# FX rows: one message, parse then evaluate, assert the stated verdict.
# --------------------------------------------------------------------------- #

# fixture stem -> (message status, dominant reason) the FX row states.
_FX_VERDICTS: dict[str, tuple[Status, Reason]] = {
    "photo-and-tag": (Status.COUNTED, Reason.COUNTED),
    "photo-only": (Status.UNTAGGED, Reason.UNTAGGED),
    "tag-only": (Status.NOT_COUNTED, Reason.NO_LIVE_IMAGE),
    "multi-tag": (Status.COUNTED, Reason.COUNTED),
    "multi-image": (Status.COUNTED, Reason.COUNTED),
    "ios-multi-photo-share": (Status.COUNTED, Reason.COUNTED),
    "thread-reply": (Status.NOT_COUNTED, Reason.NOT_TOP_LEVEL),
    "thread-reply-broadcast": (Status.NOT_COUNTED, Reason.NOT_TOP_LEVEL),
    "image-link": (Status.NOT_COUNTED, Reason.NO_LIVE_IMAGE),
    "mention-in-quote-or-code": (Status.COUNTED, Reason.COUNTED),
    "usergroup-mention": (Status.COUNTED, Reason.COUNTED),
    "at-channel": (Status.COUNTED, Reason.COUNTED),
    "heic": (Status.COUNTED, Reason.COUNTED),
    "gif": (Status.COUNTED, Reason.COUNTED),
    "video": (Status.NOT_COUNTED, Reason.NO_LIVE_IMAGE),
    "slack-connect-file": (Status.NOT_COUNTED, Reason.NO_LIVE_IMAGE),
}


@pytest.mark.parametrize("stem", sorted(_FX_VERDICTS), ids=sorted(_FX_VERDICTS))
def test_fx_verdict(fixtures_dir: Path, stem: str) -> None:
    msg = _load(fixtures_dir, "history", f"{stem}.json")
    cand = _parse(msg)
    assert isinstance(cand, Candidate), f"{stem} did not parse to a Candidate"
    verdict = _evaluate(cand)
    exp_status, exp_reason = _FX_VERDICTS[stem]
    assert verdict.status is exp_status, f"{stem}: status {verdict.status} != {exp_status}"
    assert verdict.reason is exp_reason, f"{stem}: reason {verdict.reason} != {exp_reason}"


def test_fx_multi_tag_targets_deduped_in_order(fixtures_dir: Path) -> None:
    # section 2.1: `targets` in mention order, deduped, len 3 -> all COUNTED.
    cand = _parse(_load(fixtures_dir, "history", "multi-tag.json"))
    assert isinstance(cand, Candidate)
    assert cand.targets == ("U0AAA002", "U0AAA003", "U0AAA004")
    verdict = _evaluate(cand)
    assert verdict.status is Status.COUNTED
    assert len(verdict.pairs) == 3
    assert all(p.status is Status.COUNTED for p in verdict.pairs)


def test_fx_multi_image_one_counted(fixtures_dir: Path) -> None:
    # section 2.1: live_images > 1, one target -> a single COUNTED pair.
    cand = _parse(_load(fixtures_dir, "history", "multi-image.json"))
    assert isinstance(cand, Candidate)
    assert cand.live_images > 1
    verdict = _evaluate(cand)
    assert verdict.status is Status.COUNTED
    assert len(verdict.pairs) == 1


def test_fx_digest_parses_to_digest(fixtures_dir: Path) -> None:
    # L1-FX-digest-roundtrip: a snipe_digest message is a Digest, not a candidate.
    result = _parse(_load(fixtures_dir, "history", "digest.json"))
    assert isinstance(result, Digest)


# --------------------------------------------------------------------------- #
# CT rows (paired one-input flips; 50 section 1.3, section 2.1).
# --------------------------------------------------------------------------- #

def test_L1_CT_off_roster(fixtures_dir: Path) -> None:
    # L1-CT-off-roster: the off-roster sender flips the roster gate against its
    # rostered twin. Both are photo-and-tag shapes; only the sender differs.
    twin = _parse(_load(fixtures_dir, "history", "photo-and-tag.json"))
    control = _parse(_load(fixtures_dir, "controls", "photo-and-tag__off-roster-sender.json"))
    assert isinstance(twin, Candidate) and isinstance(control, Candidate)

    twin_verdict = _evaluate(twin)
    control_verdict = _evaluate(control)   # sender U0AAA099 is absent from the roster

    assert twin_verdict.status is Status.COUNTED
    assert control_verdict.status is Status.NOT_COUNTED
    assert control_verdict.reason is Reason.SENDER_OFF_ROSTER


def test_L1_CT_veto_target(fixtures_dir: Path) -> None:
    # L1-CT-veto-target: under the default `by: [admins]` a target's reaction is
    # not a veto, so parse+evaluate leaves BOTH the control (no reaction) and its
    # twin (target reaction) COUNTED. The veto is assembled by `sync` from
    # reactions and never appears in a single parse, so the verdict does not flip
    # here; the flip is the audit flag, asserted in test_sync.
    control = _parse(_load(fixtures_dir, "controls", "veto-by-target__control.json"))
    twin = _parse(_load(fixtures_dir, "refetch", "veto-by-target", "after.json"))
    assert isinstance(control, Candidate) and isinstance(twin, Candidate)

    assert control.vetoes == ()
    assert twin.vetoes == ()   # parse assembles no veto from a reaction
    assert _evaluate(control).status is Status.COUNTED
    assert _evaluate(twin).status is Status.COUNTED


def test_L1_CT_late_tag(fixtures_dir: Path) -> None:
    # L1-CT-late-tag: `tag-edited-in__within-grace` -> COUNTED. The "late" twin
    # flips only on edit-time evidence outside the grace window, which `sync`
    # records as a TargetEdit; the flip is shown here at the pair level on the
    # merged rows (the merge itself is exercised in test_sync).
    after = _parse(_load(fixtures_dir, "controls", "tag-edited-in__within-grace", "after.json"))
    assert isinstance(after, Candidate)
    # As a single observation every target is first-seen, so the row is COUNTED.
    assert _evaluate(after).status is Status.COUNTED

    ts_us = parse_ts(after.ts)
    grace_us = _default_rule().edit_grace_us
    edited_in = "U0AAA003"
    assert edited_in in after.targets

    within = dataclasses.replace(
        after,
        first_seen_targets=frozenset(t for t in after.targets if t != edited_in),
        target_edited_in=(TargetEdit(user=edited_in, edit_ts="1758210030.000000"),),
    )
    late = dataclasses.replace(
        within,
        target_edited_in=(
            TargetEdit(user=edited_in, edit_ts=f"{(ts_us + grace_us) // 1_000_000}.010000"),
        ),
    )

    def _pair_reason(cand: Candidate, target: str) -> Reason:
        verdict = _evaluate(cand)
        return next(p.reason for p in verdict.pairs if p.target == target)

    assert _pair_reason(within, edited_in) is Reason.COUNTED
    assert _pair_reason(late, edited_in) is Reason.LATE_TAG


# --------------------------------------------------------------------------- #
# Control targets (50 section 1.3). These are the tests the three seam-level
# breaks turn red; tests/test_controls_registry.py drives them in a subprocess.
# Built by hand (never via `parse`) so each pins exactly the broken clause.
# --------------------------------------------------------------------------- #

def _cand(ts: str, sender: str = "U0AAA001", targets=("U0AAA002",), **over) -> Candidate:
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


def test_ctl_cooldown_boundary_counts() -> None:
    # An attempt exactly one cooldown window after its anchor is admitted
    # (`elapsed < window` is False at the edge). CTL-RULES-BOUNDARY widens the
    # window by one microsecond and this second snipe cools down instead.
    first = _cand("1758210000.000000")
    window_s = (15 * US_PER_MINUTE) // 1_000_000
    second = _cand(f"{1758210000 + window_s}.000000")
    out = rules.evaluate(
        [first, second], _rules(), _legit_roster(), frozenset(), (SEM,), TZ
    )
    assert out[0].status is Status.COUNTED
    assert out[1].status is Status.COUNTED


def _sibfam_selfie_candidate(face_count: int) -> Candidate:
    # Sibling sender + target, one live image carrying `face_count` faces.
    return _cand(
        "1758210000.000000",
        live_image_ids=("F0FILE001",),
        face_counts={"F0FILE001": face_count},
    )


def _sibfam_roster() -> Roster:
    return _roster({"U0AAA001": "sibA", "U0AAA002": "sibA"})


def test_ctl_selfie_class_t_plus_one() -> None:
    # One target, T+1 faces (the sniper's own face on top) -> SELFIE.
    # CTL-FACES-OFFBYONE shifts the selfie test to T+2, dropping this to AMBIGUOUS.
    cand = _sibfam_selfie_candidate(face_count=2)   # len(targets) == 1
    verdict = rules.evaluate(
        [cand], _rules(), _sibfam_roster(), frozenset(), (SEM,), TZ
    )[0]
    assert verdict.status is Status.COUNTED
    assert verdict.selfie is SelfieClass.SELFIE


def test_ctl_selfie_class_t_plus_two_ambiguous() -> None:
    # T+2 faces is not a clean selfie -> AMBIGUOUS. CTL-FACES-OFFBYONE mislabels
    # it SELFIE.
    cand = _sibfam_selfie_candidate(face_count=3)   # len(targets) + 2
    verdict = rules.evaluate(
        [cand], _rules(), _sibfam_roster(), frozenset(), (SEM,), TZ
    )[0]
    assert verdict.status is Status.COUNTED
    assert verdict.selfie is SelfieClass.AMBIGUOUS
