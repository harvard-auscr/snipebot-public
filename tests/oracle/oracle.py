"""Independent, brute-force reimplementation of the snipe rules.

Written from the definitions (plan section 1) and the type/precedence declarations,
never from the production sweep. Where production carries a running cooldown anchor,
this version rescans all earlier rows for every pair: an O(n^2) path to the same
boundary result, so agreement is evidence rather than a shared implementation.

All timing is integer microseconds via ``parse_ts``; ``float`` is never applied to a
Slack ts. Pure: no I/O, no clock, no randomness.
"""
from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import AbstractSet
from zoneinfo import ZoneInfo

from snipebot.config import (
    DatedRules, ResolvedRule, Roster, RosterMode, Scope, MultiTag, Semester,
)
from snipebot.parse import Candidate
from snipebot.rules import (
    MessageVerdict,
    NoRuleInForceError,
    PairVerdict,
    Reason,
    SelfieClass,
    Status,
    reason_status,
)
from snipebot.ts import parse_ts

# Per-target reasons, most severe first. Used to pick the dominant reason of a
# NOT_COUNTED message (highest-precedence reason present among its pairs). COUNTED and
# COOLDOWN never reach that selection (handled by earlier clauses); MULTI_TAG_FOLDED is
# a would-have-counted outcome and ranks last.
_TARGET_ORDER: tuple[Reason, ...] = (
    Reason.SELF_SNIPE,
    Reason.TARGET_OPTED_OUT,
    Reason.TARGET_OFF_ROSTER,
    Reason.TARGET_IS_BOT,
    Reason.LATE_TAG,
    Reason.COOLDOWN,
    Reason.DAILY_CAP,
    Reason.COUNTED,
    Reason.MULTI_TAG_FOLDED,
)
_TARGET_RANK = {r: i for i, r in enumerate(_TARGET_ORDER)}


def _local_day(ts_us: int, tz: ZoneInfo) -> datetime.date:
    # Integer seconds only; the sub-second part never changes the local date.
    return datetime.datetime.fromtimestamp(ts_us // 1_000_000, tz).date()


def _scope_key(scope: Scope, sender: str, target: str) -> tuple:
    if scope is Scope.TARGET:
        return ("T", target)
    return ("P", sender, target)


def _semester_of(ts_us: int, semesters: Sequence[Semester]) -> Semester | None:
    for s in semesters:
        if s.contains(ts_us):
            return s
    return None


def _live_image_count(row: Candidate, rule: ResolvedRule) -> int:
    n = row.live_images
    if rule.allow_video:
        n += row.live_videos
    if rule.count_image_links:
        n += row.linked_images
    return n


# Slack's built-in assistant account; never a player under players.mode auto.
_SLACKBOT = "USLACKBOT"


def _group_of(roster: Roster, user: str, ts_us: int) -> str | None:
    """The group at ``ts_us``. Under auto a ``from:`` dates group membership only, so
    before it the user plays ungrouped (E-W4-42). Under listed a user before their
    ``from:`` is off the roster, so the date never matters here."""
    entry = roster.entries.get(user)
    if entry is None:
        return None
    if roster.mode is RosterMode.AUTO and entry.join_us > ts_us:
        return None
    return entry.group


def _player_at(roster: Roster, user: str, ts_us: int) -> bool:
    """Roster membership read straight off the definitions (00 section 5; E-W4-42), not
    through ``Roster.is_member_at``. Listed: on the roster and joined by ``ts_us``.
    Auto: anyone who is not a bot and not USLACKBOT, at every ts; an ID the bot map
    never saw is a human; a ``from:`` dates only the group (``_group_of``)."""
    entry = roster.entries.get(user)
    if roster.mode is RosterMode.AUTO:
        if user == _SLACKBOT or user in roster.bots:
            return False
        return entry is None or not entry.is_bot
    return entry is not None and entry.join_us <= ts_us


def player_at(roster: Roster, user: str, ts_us: int) -> bool:
    """Public face of the oracle's own membership rule, for the property tests."""
    return _player_at(roster, user, ts_us)


def _bot_target(roster: Roster, user: str) -> bool:
    if roster.mode is RosterMode.AUTO and user in roster.bots:
        return True
    entry = roster.entries.get(user)
    return entry is not None and entry.is_bot


def _intra_group(roster: Roster, sender: str, target: str, ts_us: int) -> bool:
    g = _group_of(roster, sender, ts_us)
    return g is not None and g == _group_of(roster, target, ts_us)


def _sib_tagged(row: Candidate, roster: Roster, ts_us: int,
                opted_out: AbstractSet[str]) -> bool:
    g = _group_of(roster, row.sender, ts_us)
    if g is None:
        return False
    if not _player_at(roster, row.sender, ts_us):
        return False
    for t in row.targets:
        # A self-tag or an opted-out sib never counts toward sib-tagged (E-W4-29, E-W4-30).
        if t == row.sender or t in opted_out:
            continue
        if _group_of(roster, t, ts_us) == g and _player_at(roster, t, ts_us):
            return True
    return False


def _is_repost(row: Candidate, ts_us: int, roster: Roster,
               opted_out: AbstractSet[str],
               facts_by_ts: Sequence[tuple[int, Candidate]],
               semesters: Sequence[Semester]) -> bool:
    # Only a sib-tagged message carries a rendition hash worth matching.
    if not _sib_tagged(row, roster, ts_us, opted_out):
        return False
    my_hashes = set(row.rendition_hash.values())
    if not my_hashes:
        return False
    sem = _semester_of(ts_us, semesters)
    if sem is None:  # out of season handled by an earlier gate; nothing to match in.
        return False
    for other_us, other in facts_by_ts:
        if other_us >= ts_us:
            break  # earlier-ts only; list is sorted ascending
        if not sem.contains(other_us):
            continue
        if my_hashes & set(other.rendition_hash.values()):
            return True
    return False


def _message_gate(row: Candidate, ts_us: int, rule: ResolvedRule, roster: Roster,
                  opted_out: AbstractSet[str], semesters: Sequence[Semester],
                  facts_by_ts: Sequence[tuple[int, Candidate]]) -> Reason | None:
    """First per-message gate that fires, or None. Precedence per 00 section 4."""
    if row.deleted:
        return Reason.DELETED
    if not row.is_top_level and not rule.count_thread_replies:
        return Reason.NOT_TOP_LEVEL
    if _semester_of(ts_us, semesters) is None:
        return Reason.OUT_OF_SEASON
    if not _player_at(roster, row.sender, ts_us):
        return Reason.SENDER_OFF_ROSTER
    if row.sender in opted_out:
        return Reason.SENDER_OPTED_OUT
    if _live_image_count(row, rule) == 0:
        return Reason.NO_LIVE_IMAGE
    if rule.max_targets_per_message is not None and len(row.targets) > rule.max_targets_per_message:
        return Reason.TOO_MANY_TARGETS
    if len(row.vetoes) > 0:
        return Reason.VETOED
    if _is_repost(row, ts_us, roster, opted_out, facts_by_ts, semesters):
        return Reason.REPOST
    return None


def _pre_gate(row: Candidate, ts_us: int, target: str, rule: ResolvedRule,
              roster: Roster, opted_out: AbstractSet[str]) -> Reason | None:
    """Per-target gates 1-5 (identity, then late tag). None means still-eligible."""
    if target == row.sender and not rule.allow_self:
        return Reason.SELF_SNIPE
    if target in opted_out:
        return Reason.TARGET_OPTED_OUT
    if not _player_at(roster, target, ts_us):
        return Reason.TARGET_OFF_ROSTER
    if _bot_target(roster, target) and not rule.allow_bots:
        return Reason.TARGET_IS_BOT
    if _late_tag(row, ts_us, target, rule):
        return Reason.LATE_TAG
    return None


def _late_tag(row: Candidate, ts_us: int, target: str, rule: ResolvedRule) -> bool:
    # Evidence rule: a tag counts as late only if it was edited in (absent at creation)
    # and the recorded edit ts is outside the grace window. A target present at first
    # sight is tagged at posting and is never late; grace is inclusive.
    if target in row.first_seen_targets:
        return False
    for te in row.target_edited_in:
        if te.user == target:
            if te.edit_ts is None:
                return False
            return parse_ts(te.edit_ts) > ts_us + rule.edit_grace_us
    # No evidence that the bot ever saw it untagged: treat as tagged at posting.
    return False


class _Attempt:
    __slots__ = ("ts_us", "ts", "key", "target", "day", "status", "advanced")

    def __init__(self, ts_us: int, ts: str, key: tuple, target: str,
                 day: datetime.date, status: Status, advanced: bool) -> None:
        self.ts_us = ts_us
        self.ts = ts
        self.key = key
        self.target = target
        self.day = day
        self.status = status
        self.advanced = advanced


def _admit(row: Candidate, ts_us: int, target: str, rule: ResolvedRule,
           tz: ZoneInfo, history: Sequence[_Attempt]) -> tuple[Reason, str | None]:
    """Cooldown / daily-cap / counted decision for one eligible pair, by rescanning
    all earlier attempts in the same scope. Returns (reason, blocked_by)."""
    key = _scope_key(rule.cooldown.scope, row.sender, target)
    cd = rule.cooldown.microseconds

    anchor_us = None
    anchor_ts = None
    for a in history:
        if a.key != key or not a.advanced:
            continue
        if anchor_us is None or a.ts_us > anchor_us:
            anchor_us = a.ts_us
            anchor_ts = a.ts

    if anchor_us is not None and (ts_us - anchor_us) < cd:
        return Reason.COOLDOWN, anchor_ts

    # Clears cooldown. Daily cap counts this target's earlier COUNTED pairs on the same
    # local day (any sniper).
    cap = rule.max_snipes_per_target_per_day
    if cap is not None:
        day = _local_day(ts_us, tz)
        counted_today = sum(
            1 for a in history
            if a.target == target and a.status is Status.COUNTED and a.day == day
        )
        if counted_today >= cap:
            return Reason.DAILY_CAP, None
    return Reason.COUNTED, None


def evaluate(
    facts: Sequence[Candidate],
    rules: DatedRules,
    roster: Roster,
    opted_out: AbstractSet[str],
    semesters: Sequence[Semester],
    tz: ZoneInfo,
) -> list[MessageVerdict]:
    ordered = sorted(facts, key=lambda c: parse_ts(c.ts))
    facts_by_ts: list[tuple[int, Candidate]] = [(parse_ts(c.ts), c) for c in ordered]

    verdicts: list[MessageVerdict] = []
    history: list[_Attempt] = []

    for ts_us, row in facts_by_ts:
        rule = rules.in_force_at(ts_us)  # raises NoRuleInForceError if none

        gate = _message_gate(row, ts_us, rule, roster, opted_out, semesters, facts_by_ts)
        if gate is not None:
            verdicts.append(MessageVerdict(
                ts=row.ts, status=reason_status(gate), reason=gate,
                selfie=SelfieClass.NOT_APPLICABLE, pairs=(),
            ))
            continue

        if len(row.targets) == 0:
            verdicts.append(MessageVerdict(
                ts=row.ts, status=Status.UNTAGGED, reason=Reason.UNTAGGED,
                selfie=SelfieClass.NOT_APPLICABLE, pairs=(),
            ))
            continue

        # Stage 1: per-target gates 1-5.
        prelim: list[tuple[str, Reason | None]] = [
            (t, _pre_gate(row, ts_us, t, rule, roster, opted_out)) for t in row.targets
        ]
        eligible_idx = [i for i, (_t, r) in enumerate(prelim) if r is None]

        # Stage 2: cooldown / daily-cap / counted, honoring multi_tag.
        day = _local_day(ts_us, tz)
        new_attempts: list[_Attempt] = []
        pair_reason: list[Reason] = []
        pair_blocked: list[str | None] = []

        if rule.multi_tag is MultiTag.SINGLE:
            chosen = eligible_idx[0] if eligible_idx else None
            for i, (t, r) in enumerate(prelim):
                if r is not None:
                    reason, blocked = r, None
                elif i == chosen:
                    reason, blocked = _admit(row, ts_us, t, rule, tz, history)
                else:
                    reason, blocked = Reason.MULTI_TAG_FOLDED, None
                pair_reason.append(reason)
                pair_blocked.append(blocked)
        else:
            for _i, (t, r) in enumerate(prelim):
                if r is not None:
                    reason, blocked = r, None
                else:
                    reason, blocked = _admit(row, ts_us, t, rule, tz, history)
                pair_reason.append(reason)
                pair_blocked.append(blocked)

        # Record attempts (for later messages' rescans). Only COUNTED, or a COOLDOWN
        # rejection while rejected_attempts_reset is in force, advances the anchor.
        for (t, _r), reason in zip(prelim, pair_reason):
            status = reason_status(reason)
            advanced = status is Status.COUNTED or (
                status is Status.COOLDOWN and rule.cooldown.rejected_attempts_reset
            )
            new_attempts.append(_Attempt(
                ts_us=ts_us, ts=row.ts,
                key=_scope_key(rule.cooldown.scope, row.sender, t),
                target=t, day=day, status=status, advanced=advanced,
            ))

        # Message-level status/reason from the pairs.
        statuses = [reason_status(r) for r in pair_reason]
        if Status.COUNTED in statuses:
            msg_status, msg_reason = Status.COUNTED, Reason.COUNTED
        elif Status.COOLDOWN in statuses:
            msg_status, msg_reason = Status.COOLDOWN, Reason.COOLDOWN
        else:
            msg_status = Status.NOT_COUNTED
            msg_reason = min(pair_reason, key=lambda r: _TARGET_RANK[r])

        # Selfie classification (message level).
        # A self-tag is never a selfie target (E-W4-29).
        T = sum(1 for t in row.targets if t != row.sender)
        msg_class = _classify_selfie(row, ts_us, rule, roster, opted_out, T, msg_status)

        pairs = tuple(
            PairVerdict(
                ts=row.ts, target=t, status=reason_status(reason), reason=reason,
                blocked_by=blocked,
                selfie=(msg_class is SelfieClass.SELFIE
                        and _intra_group(roster, row.sender, t, ts_us)
                        and reason_status(reason) is Status.COUNTED),
            )
            for (t, _r), reason, blocked in zip(prelim, pair_reason, pair_blocked)
        )

        verdicts.append(MessageVerdict(
            ts=row.ts, status=msg_status, reason=msg_reason,
            selfie=msg_class, pairs=pairs,
        ))
        history.extend(new_attempts)

    return verdicts


def counted_pairs(verdicts: Sequence[MessageVerdict]) -> list[PairVerdict]:
    return [p for mv in verdicts for p in mv.pairs if p.status is Status.COUNTED]


def points_breakdown(
    verdicts: Sequence[MessageVerdict], sender_by_ts: Mapping[str, str],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Returns (plain_snipe_points, selfie_photo_points, participation_points), each a
    person -> count map, derived purely from the verdicts (00 section 4 "Points")."""
    plain: dict[str, int] = defaultdict(int)
    selfie_photo: dict[str, int] = defaultdict(int)
    participation: dict[str, int] = defaultdict(int)
    for mv in verdicts:
        sender = sender_by_ts[mv.ts]
        counted_intra_selfie = False
        for p in mv.pairs:
            if p.status is not Status.COUNTED:
                continue
            if p.selfie:
                participation[p.target] += 1
                counted_intra_selfie = True
            else:
                plain[sender] += 1
        if mv.selfie is SelfieClass.SELFIE and counted_intra_selfie:
            selfie_photo[sender] += 1
    return dict(plain), dict(selfie_photo), dict(participation)


def people_points(
    verdicts: Sequence[MessageVerdict], sender_by_ts: Mapping[str, str],
) -> dict[str, int]:
    plain, selfie_photo, participation = points_breakdown(verdicts, sender_by_ts)
    out: dict[str, int] = defaultdict(int)
    for m in (plain, selfie_photo, participation):
        for person, n in m.items():
            out[person] += n
    return dict(out)


def _classify_selfie(row: Candidate, ts_us: int, rule: ResolvedRule, roster: Roster,
                     opted_out: AbstractSet[str], T: int,
                     msg_status: Status) -> SelfieClass:
    if msg_status is not Status.COUNTED:
        return SelfieClass.NOT_APPLICABLE
    if not _sib_tagged(row, roster, ts_us, opted_out):
        return SelfieClass.NOT_APPLICABLE
    if not rule.selfie_bonus:
        return SelfieClass.NOT_APPLICABLE
    if row.selfie_override is not None:
        return SelfieClass.SELFIE if row.selfie_override.value else SelfieClass.SNIPE
    # Every current live image must carry a count; all equal to T -> SNIPE, all equal
    # to T+1 -> SELFIE, anything else (missing, other value, or a mix) -> AMBIGUOUS.
    # An empty live-image set satisfies the first clause vacuously and reads as SNIPE.
    L = row.live_image_ids
    fc = row.face_counts
    if all(l in fc and fc[l] == T for l in L):
        return SelfieClass.SNIPE
    if all(l in fc and fc[l] == T + 1 for l in L):
        return SelfieClass.SELFIE
    return SelfieClass.AMBIGUOUS
