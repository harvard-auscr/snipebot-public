"""Verdict types and the `evaluate` contract (00-data §4, §5).

Module home of the verdict layer shared across the package: the `Status` and
`Reason` enums, `reason_status`, the sibfam `SelfieClass`, the `PairVerdict` and
`MessageVerdict` objects, the `needs_review` flag, and the `evaluate` signature;
`NoRuleInForceError` is re-exported from `snipebot.config`, which raises it. Other
modules import these by reference.

`evaluate`'s body (the cooldown / classification algorithm) is filled in by the
rules workstream; the declarations, `reason_status` and `needs_review` are
complete here because peers import them directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from snipebot.config import MultiTag, NoRuleInForceError, Scope  # noqa: F401  (re-exported)
from snipebot.ts import US_PER_SECOND, parse_ts

if TYPE_CHECKING:
    from collections.abc import AbstractSet, Sequence
    from zoneinfo import ZoneInfo

    from snipebot.config import DatedRules, ResolvedRule, Roster, Semester
    from snipebot.parse import Candidate
    # DatedRules, Roster, Semester, ReviewFlag are homed in the §6 config layer.


class Status(str, Enum):
    COUNTED = "counted"
    COOLDOWN = "cooldown"
    UNTAGGED = "untagged"
    NOT_COUNTED = "not_counted"


class Reason(str, Enum):
    # published outcomes
    COUNTED = "counted"                    # -> Status.COUNTED
    COOLDOWN = "cooldown"                   # -> Status.COOLDOWN
    UNTAGGED = "untagged"                   # -> Status.UNTAGGED
    # per-message not-counted
    DELETED = "deleted"
    NOT_TOP_LEVEL = "not_top_level"
    OUT_OF_SEASON = "out_of_season"
    SENDER_OFF_ROSTER = "sender_off_roster"
    SENDER_OPTED_OUT = "sender_opted_out"
    NO_LIVE_IMAGE = "no_live_image"
    TOO_MANY_TARGETS = "too_many_targets"
    VETOED = "vetoed"
    REPOST = "repost"                       # sib selfie re-upload: same rendition bytes,
                                            # earlier ts, same semester
    # per-target not-counted
    SELF_SNIPE = "self_snipe"
    TARGET_OPTED_OUT = "target_opted_out"
    TARGET_OFF_ROSTER = "target_off_roster"
    TARGET_IS_BOT = "target_is_bot"
    LATE_TAG = "late_tag"
    DAILY_CAP = "daily_cap"
    MULTI_TAG_FOLDED = "multi_tag_folded"   # only under multi_tag: single


_PUBLISHED_STATUS: dict[Reason, Status] = {
    Reason.COUNTED: Status.COUNTED,
    Reason.COOLDOWN: Status.COOLDOWN,
    Reason.UNTAGGED: Status.UNTAGGED,
}


def reason_status(reason: Reason) -> Status:
    """Map a reason to its message/pair status: COUNTED, COOLDOWN and UNTAGGED
    map to themselves; every other reason maps to NOT_COUNTED."""
    return _PUBLISHED_STATUS.get(reason, Status.NOT_COUNTED)


class SelfieClass(str, Enum):
    SNIPE = "snipe"                    # scores a plain snipe (T faces, or override False)
    SELFIE = "selfie"                  # awards selfie photo + participation points (T+1, or override True)
    AMBIGUOUS = "ambiguous"            # scores as SNIPE but sets the review flag; sticky until resolved
    NOT_APPLICABLE = "not_applicable"  # not counted, not sib-tagged, or selfie_bonus false


@dataclass(frozen=True)
class PairVerdict:
    ts: str                 # message ts (Slack ts)
    target: str             # target user ID
    status: Status
    reason: Reason
    blocked_by: str | None  # Slack ts of the attempt that set the cooldown anchor;
                            # only when status == COOLDOWN
    selfie: bool            # True iff the message class is SELFIE, this pair is
                            # intra-group and COUNTED


@dataclass(frozen=True)
class MessageVerdict:
    ts: str
    status: Status                    # message-level; drives the one feedback reaction
    reason: Reason                    # dominant reason
    selfie: SelfieClass               # sibfam selfie classification; NOT_APPLICABLE off the bonus path
    pairs: tuple[PairVerdict, ...]    # one per CURRENT real-user target; () when gated or untagged


def needs_review(mv: "MessageVerdict", row: "Candidate", review: "ReviewFlag") -> bool:
    if mv.status != Status.COUNTED:
        return False
    many_targets = review.min_targets is not None and len(row.targets) >= review.min_targets
    return many_targets or mv.selfie is SelfieClass.AMBIGUOUS


# NoRuleInForceError is defined once, in snipebot.config, because DatedRules.in_force_at
# raises it; it is re-exported here so callers can import it beside evaluate.


# Per-target reasons ranked by §4 precedence, used to pick the dominant
# message-level reason on a message with neither a COUNTED nor a COOLDOWN pair.
_TARGET_REASON_RANK: dict[Reason, int] = {
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


def _local_date_str(ts_us: int, tz: "ZoneInfo") -> str:
    """Message's local calendar date "YYYY-MM-DD" in `tz`. Integer µs in, no float."""
    return (
        datetime.fromtimestamp(ts_us // US_PER_SECOND, tz=timezone.utc)
        .astimezone(tz)
        .strftime("%Y-%m-%d")
    )


def _late_tag(row: "Candidate", target: str, rule: "ResolvedRule", ts_us: int) -> bool:
    """A target absent at first sight is late only on edit-time evidence outside grace."""
    if target in row.first_seen_targets:
        return False
    edit_ts: str | None = None
    for te in row.target_edited_in:
        if te.user == target:
            edit_ts = te.edit_ts
            break
    if edit_ts is None:
        return False
    return parse_ts(edit_ts) > ts_us + rule.edit_grace_us


def _classify_selfie(
    row: "Candidate",
    roster: "Roster",
    rule: "ResolvedRule",
    m_status: Status,
    sib_tagged: bool,
) -> SelfieClass:
    """Message-level sibfam selfie class (§4). Pure in the stored face facts, the
    durable override and the dated `selfie_bonus`; never reads the detector."""
    if not (m_status is Status.COUNTED and sib_tagged and rule.selfie_bonus):
        return SelfieClass.NOT_APPLICABLE
    override = row.selfie_override
    if override is not None:
        return SelfieClass.SELFIE if override.value else SelfieClass.SNIPE
    # a self-tag is never a selfie target: that pair is already SELF_SNIPE (E-W4-29)
    t = len([x for x in row.targets if x != row.sender])
    counts = [row.face_counts.get(fid) for fid in row.live_image_ids]
    if all(c is not None for c in counts) and all(c == t for c in counts):
        return SelfieClass.SNIPE
    if all(c is not None for c in counts) and all(c == t + 1 for c in counts):
        return SelfieClass.SELFIE
    return SelfieClass.AMBIGUOUS


def _remember_hashes(
    seen: dict[str, set[str]], sem: "Semester | None", row: "Candidate"
) -> None:
    """Record this row's rendition hashes under its semester, for the REPOST gate."""
    if sem is None or not row.rendition_hash:
        return
    seen.setdefault(sem.name, set()).update(row.rendition_hash.values())


def evaluate(
    facts: "Sequence[Candidate]",       # any order; unique ts
    rules: "DatedRules",                # resolved, dated (§6)
    roster: "Roster",                   # resolved, dated join instants (§6)
    opted_out: "AbstractSet[str]",      # opted-out user IDs (membership only)
    semesters: "Sequence[Semester]",    # resolved, local-time instants (§6)
    tz: "ZoneInfo",                     # config timezone, for day buckets
) -> "list[MessageVerdict]":
    rows = sorted(facts, key=lambda f: parse_ts(f.ts))
    verdicts: list[MessageVerdict] = []

    # cooldown state carried across the chronological sweep (§5)
    anchor: dict[tuple[str, ...], int] = {}       # scope key -> anchoring ts (µs)
    anchor_ts: dict[tuple[str, ...], str] = {}    # scope key -> Slack ts of the anchor
    day_counted: dict[tuple[str, str], int] = {}  # (target, local date) -> counted snipes
    seen_hashes: dict[str, set[str]] = {}         # semester name -> rendition hashes

    def admit(
        sender: str, target: str, ts: str, ts_us: int, rule: "ResolvedRule"
    ) -> tuple[Status, Reason, str | None]:
        cd = rule.cooldown
        key = (sender, target) if cd.scope is Scope.PAIR else (target,)
        a = anchor.get(key)
        if a is not None and ts_us - a < cd.microseconds:      # within window -> rejected
            blocker = anchor_ts.get(key)
            if cd.rejected_attempts_reset:
                anchor[key] = ts_us                            # rejection restarts the clock
                anchor_ts[key] = ts
            return (Status.COOLDOWN, Reason.COOLDOWN, blocker)
        day = _local_date_str(ts_us, tz)                       # clears cooldown
        cap = rule.max_snipes_per_target_per_day
        if cap is not None and day_counted.get((target, day), 0) >= cap:
            return (Status.NOT_COUNTED, Reason.DAILY_CAP, None)   # creates nothing
        anchor[key] = ts_us
        anchor_ts[key] = ts
        day_counted[(target, day)] = day_counted.get((target, day), 0) + 1
        return (Status.COUNTED, Reason.COUNTED, None)

    for f in rows:
        ts = f.ts
        ts_us = parse_ts(ts)
        rule = rules.in_force_at(ts_us)   # NoRuleInForceError propagates: the run fails
        sem = next((s for s in semesters if s.contains(ts_us)), None)

        sender_group = roster.group_of(f.sender, ts_us)
        sib_tagged = (
            sender_group is not None
            and roster.is_member_at(f.sender, ts_us)
            and any(
                # a self-tag or an opted-out sib alone never makes it sib-tagged (E-W4-29, -30)
                t != f.sender
                and t not in opted_out
                and roster.group_of(t, ts_us) == sender_group
                and roster.is_member_at(t, ts_us)
                for t in f.targets
            )
        )

        live_count = (
            f.live_images
            + (f.live_videos if rule.allow_video else 0)
            + (f.linked_images if rule.count_image_links else 0)
        )

        # --- per-message gates, §4 precedence (first match wins) ---
        gate: Reason | None = None
        if f.deleted:
            gate = Reason.DELETED
        elif (not f.is_top_level) and not rule.count_thread_replies:
            gate = Reason.NOT_TOP_LEVEL
        elif sem is None:
            gate = Reason.OUT_OF_SEASON
        elif not roster.is_member_at(f.sender, ts_us):
            gate = Reason.SENDER_OFF_ROSTER
        elif f.sender in opted_out:
            gate = Reason.SENDER_OPTED_OUT
        elif live_count == 0:
            gate = Reason.NO_LIVE_IMAGE
        elif (
            rule.max_targets_per_message is not None
            and len(f.targets) > rule.max_targets_per_message
        ):
            gate = Reason.TOO_MANY_TARGETS
        elif len(f.vetoes) > 0:
            gate = Reason.VETOED
        elif (
            sib_tagged
            and any(
                h in seen_hashes.get(sem.name, frozenset())
                for h in f.rendition_hash.values()
            )
        ):
            gate = Reason.REPOST

        if gate is not None:
            verdicts.append(
                MessageVerdict(
                    ts=ts,
                    status=reason_status(gate),
                    reason=gate,
                    selfie=SelfieClass.NOT_APPLICABLE,
                    pairs=(),
                )
            )
            _remember_hashes(seen_hashes, sem, f)
            continue

        if len(f.targets) == 0:
            verdicts.append(
                MessageVerdict(
                    ts=ts,
                    status=Status.UNTAGGED,
                    reason=Reason.UNTAGGED,
                    selfie=SelfieClass.NOT_APPLICABLE,
                    pairs=(),
                )
            )
            _remember_hashes(seen_hashes, sem, f)
            continue

        # --- per-target gates up to LATE_TAG (§4 precedence) ---
        pre_reason: dict[str, Reason] = {}
        eligible: list[str] = []
        for t in f.targets:
            if t == f.sender and not rule.allow_self:
                pre_reason[t] = Reason.SELF_SNIPE
            elif t in opted_out:
                pre_reason[t] = Reason.TARGET_OPTED_OUT
            elif not roster.is_member_at(t, ts_us):
                pre_reason[t] = Reason.TARGET_OFF_ROSTER
            elif roster.is_bot(t) and not rule.allow_bots:
                pre_reason[t] = Reason.TARGET_IS_BOT
            elif _late_tag(f, t, rule, ts_us):
                pre_reason[t] = Reason.LATE_TAG
            else:
                eligible.append(t)

        # --- cooldown / cap sweep over still-eligible targets, honoring multi_tag ---
        sweep: dict[str, tuple[Status, Reason, str | None]] = {}
        if rule.multi_tag is MultiTag.SINGLE:
            if eligible:
                chosen = eligible[0]
                sweep[chosen] = admit(f.sender, chosen, ts, ts_us, rule)
                for t in eligible[1:]:
                    sweep[t] = (Status.NOT_COUNTED, Reason.MULTI_TAG_FOLDED, None)
        else:
            for t in eligible:
                sweep[t] = admit(f.sender, t, ts, ts_us, rule)

        # --- pair status/reason/blocked_by in mention order (selfie added below) ---
        pair_sr: list[tuple[str, Status, Reason, str | None]] = []
        for t in f.targets:
            if t in pre_reason:
                pair_sr.append((t, Status.NOT_COUNTED, pre_reason[t], None))
            else:
                st, rn, bl = sweep[t]
                pair_sr.append((t, st, rn, bl))

        # --- message-level status/reason ---
        if any(st is Status.COUNTED for _, st, _, _ in pair_sr):
            m_status, m_reason = Status.COUNTED, Reason.COUNTED
        elif any(st is Status.COOLDOWN for _, st, _, _ in pair_sr):
            m_status, m_reason = Status.COOLDOWN, Reason.COOLDOWN
        else:
            m_status = Status.NOT_COUNTED
            m_reason = min(
                (rn for _, _, rn, _ in pair_sr),
                key=lambda r: _TARGET_REASON_RANK[r],
            )

        # --- selfie classification, then per-pair selfie flag ---
        selfie_class = _classify_selfie(f, roster, rule, m_status, sib_tagged)
        pairs = tuple(
            PairVerdict(
                ts=ts,
                target=t,
                status=st,
                reason=rn,
                blocked_by=bl,
                selfie=(
                    selfie_class is SelfieClass.SELFIE
                    and st is Status.COUNTED
                    and sender_group is not None
                    and roster.group_of(t, ts_us) == sender_group
                ),
            )
            for t, st, rn, bl in pair_sr
        )

        verdicts.append(
            MessageVerdict(
                ts=ts,
                status=m_status,
                reason=m_reason,
                selfie=selfie_class,
                pairs=pairs,
            )
        )
        _remember_hashes(seen_hashes, sem, f)

    return verdicts
