"""Hypothesis strategies: generated timelines as ``(config, facts)`` pairs feeding both
``evaluate`` implementations (50 section 3.3).

A timeline draws a player pool with sibling groups, opt-outs, join instants and an
optional bot, under either players.mode (listed, or auto with unnamed people and
USLACKBOT posting and tagged too);1-40 candidate messages with senders and targets from the pool; first-seen
edits clustered at the grace boundary; deletions and vetoes; 1-2 non-overlapping
semesters; and, for sib-tagged messages, face counts and rendition hashes (some reused
across messages to exercise the repost key) plus optional selfie overrides. Inter-arrival
gaps for a scope cluster on the cooldown boundary so ``>=`` is dense in the sample.
"""
from __future__ import annotations

import dataclasses
import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

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
from snipebot.ts import format_ts, parse_ts

US_PER_MIN = 60_000_000
DAY_US = 86_400_000_000
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

USERS = tuple(f"U{i}" for i in range(6))
GROUPS = ("g1", "g2")
AUTO_OUTSIDERS = ("U9", "USLACKBOT")  # under players.mode auto only
TZ_NAMES = ("UTC", "Etc/GMT-5", "America/New_York")
BASE_DATE = datetime.date(2026, 10, 20)  # near the 2026-11-01 NY DST change


def _local_us(d: datetime.date, hour: int, minute: int, second: int,
              micro: int, tz: ZoneInfo) -> int:
    dt = datetime.datetime(d.year, d.month, d.day, hour, minute, second, micro, tzinfo=tz)
    return (dt - _EPOCH) // datetime.timedelta(microseconds=1)


@dataclass(frozen=True)
class Scenario:
    facts: tuple[Candidate, ...]
    rules: DatedRules
    roster: Roster
    opted_out: frozenset[str]
    semesters: tuple[Semester, ...]
    tz: ZoneInfo
    review: ReviewFlag

    def eval_args(self) -> tuple:
        return (self.facts, self.rules, self.roster, self.opted_out,
                self.semesters, self.tz)

    @property
    def sender_by_ts(self) -> dict[str, str]:
        return {c.ts: c.sender for c in self.facts}


def _dedup(seq: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for x in seq:
        if x not in out:
            out.append(x)
    return tuple(out)


def _sib_tagged(sender: str, targets: tuple[str, ...], ts_us: int,
                roster: Roster, opted_out: frozenset[str]) -> bool:
    # A self-tag or an opted-out sib never counts toward sib-tagged (E-W4-29, E-W4-30).
    g = roster.group_of(sender, ts_us)
    if g is None or not roster.is_member_at(sender, ts_us):
        return False
    return any(t != sender and t not in opted_out
               and roster.group_of(t, ts_us) == g and roster.is_member_at(t, ts_us)
               for t in targets)


@st.composite
def scenarios(draw) -> Scenario:
    tz = ZoneInfo(draw(st.sampled_from(TZ_NAMES)))

    # --- semesters: 1-2 non-overlapping day ranges ---
    len0 = draw(st.integers(min_value=2, max_value=6))
    sem0_start_d = BASE_DATE
    sem0_end_d = BASE_DATE + datetime.timedelta(days=len0 - 1)
    sem0 = Semester(
        "s0",
        _local_us(sem0_start_d, 0, 0, 0, 0, tz),
        _local_us(sem0_end_d, 23, 59, 59, 999999, tz),
    )
    semesters = [sem0]
    if draw(st.booleans()):
        gap = draw(st.integers(min_value=1, max_value=3))
        s1_start_d = sem0_end_d + datetime.timedelta(days=gap + 1)
        s1_end_d = s1_start_d + datetime.timedelta(days=draw(st.integers(2, 5)) - 1)
        semesters.append(Semester(
            "s1",
            _local_us(s1_start_d, 0, 0, 0, 0, tz),
            _local_us(s1_end_d, 23, 59, 59, 999999, tz),
        ))
    last_end = semesters[-1].end_us

    # --- player pool ---
    pool = list(_dedup(draw(st.lists(st.sampled_from(USERS), min_size=2, max_size=6))))
    while len(pool) < 2:
        for u in USERS:
            if u not in pool:
                pool.append(u)
                break
    bot_user = draw(st.one_of(st.none(), st.sampled_from(pool)))
    # players.mode (E-W4-42): under auto only grouped users carry an entry, every other
    # non-bot ID plays ungrouped, and the bot map (not the roster) marks bots.
    mode = draw(st.sampled_from((RosterMode.LISTED, RosterMode.AUTO)))
    entries: dict[str, RosterEntry] = {}
    for u in pool:
        group = draw(st.sampled_from((*GROUPS, None)))
        if draw(st.booleans()):
            join = INT_MIN_TS
        else:
            join = draw(st.integers(min_value=sem0.start_us, max_value=last_end))
        if mode is RosterMode.AUTO and group is None:
            continue
        entries[u] = RosterEntry(u, join, group, u == bot_user)
    count_intra_group = draw(st.booleans())
    if mode is RosterMode.AUTO:
        bots = frozenset({bot_user} if bot_user is not None else ())
        roster = Roster(entries, count_intra_group, RosterMode.AUTO, bots)
        # people the config never names: an unknown human and Slack's own account
        pool = [*pool, *AUTO_OUTSIDERS]
    else:
        roster = Roster(entries, count_intra_group)

    opted_out = frozenset(draw(st.lists(st.sampled_from(pool), max_size=2, unique=True)))

    review = ReviewFlag(
        draw(st.sampled_from((None, 2, 5))),
        draw(st.sampled_from((None, "question"))),
    )

    # --- rules ---
    scope = draw(st.sampled_from((Scope.PAIR, Scope.TARGET)))
    reset = draw(st.booleans())
    multi = draw(st.sampled_from((MultiTag.PER_TARGET, MultiTag.SINGLE)))
    cd_min = draw(st.sampled_from((0, 1, 15, 30)))
    base = ResolvedRule(
        effective_from_us=INT_MIN_TS,
        cooldown=CooldownRule(cd_min * US_PER_MIN, scope, reset),
        multi_tag=multi,
        max_targets_per_message=draw(st.sampled_from((None, 1, 2, 3, 4, 5))),
        edit_grace_us=draw(st.sampled_from((0, 10))) * US_PER_MIN,
        max_snipes_per_target_per_day=draw(st.sampled_from((None, 1, 3))),
        allow_self=draw(st.booleans()),
        allow_bots=draw(st.booleans()),
        count_thread_replies=draw(st.booleans()),
        count_image_links=draw(st.booleans()),
        allow_video=draw(st.booleans()),
        selfie_bonus=draw(st.booleans()),
    )
    if draw(st.booleans()):
        eff = (sem0.start_us + sem0.end_us) // 2
        rule2 = dataclasses.replace(
            base,
            effective_from_us=eff,
            cooldown=CooldownRule(
                draw(st.sampled_from((0, 1, 15, 30))) * US_PER_MIN, scope, reset),
            selfie_bonus=draw(st.booleans()),
        )
        rules = DatedRules((base, rule2))
    else:
        rules = DatedRules((base,))

    nominal_cd = max(base.cooldown.microseconds, 1 * US_PER_MIN)

    # --- messages ---
    n = draw(st.integers(min_value=1, max_value=40))
    cur = draw(st.integers(min_value=sem0.start_us - DAY_US, max_value=sem0.start_us + DAY_US))
    hash_pool: list[str] = []
    facts: list[Candidate] = []

    for i in range(n):
        step = draw(st.sampled_from((
            nominal_cd - 1, nominal_cd, nominal_cd + 1,
            2 * nominal_cd - 1, 2 * nominal_cd,
            60_000_000, DAY_US, DAY_US // 2,
        )))
        # occasionally jump far to hit out-of-season / semester gap tails
        if draw(st.integers(0, 6)) == 0:
            step = draw(st.integers(min_value=1, max_value=3 * DAY_US))
        cur += max(1, step)
        if cur > last_end + 2 * DAY_US:
            cur = draw(st.integers(min_value=sem0.start_us - DAY_US,
                                   max_value=sem0.start_us + DAY_US))
        ts = format_ts(cur)
        ts_us = cur

        sender = draw(st.sampled_from(pool))
        targets = _dedup(draw(st.lists(st.sampled_from(pool), max_size=7)))

        live_images = draw(st.integers(0, 2))
        live_videos = draw(st.integers(0, 1))
        linked_images = draw(st.integers(0, 1))
        live_image_ids = tuple(f"{ts}#{j}" for j in range(live_images))

        # reply / subtype (reach NOT_TOP_LEVEL and count_thread_replies)
        thread_ts = None
        subtype = None
        if draw(st.integers(0, 4)) == 0:
            thread_ts = format_ts(max(1, cur - 500))
            subtype = draw(st.sampled_from((None, "thread_broadcast")))

        # first-seen edits: 0-1 target edited in near the grace boundary
        first_seen = set(targets)
        target_edited_in: list[TargetEdit] = []
        first_sight_edited = draw(st.booleans()) and bool(targets)
        if targets and draw(st.booleans()):
            edited = draw(st.sampled_from(list(targets)))
            first_seen.discard(edited)
            grace = base.edit_grace_us
            off = draw(st.sampled_from((-1, 0, 1, 60_000_000, -60_000_000)))
            edit_ts_val = draw(st.one_of(
                st.none(), st.just(format_ts(ts_us + grace + off))))
            target_edited_in.append(TargetEdit(edited, edit_ts_val))
            first_sight_edited = True
        last_edit_ts = None
        if first_sight_edited or target_edited_in:
            last_edit_ts = format_ts(ts_us + draw(st.integers(1, 2 * DAY_US)))

        # face facts + rendition hashes for sib-tagged messages only
        face_counts: dict[str, int] = {}
        rendition_hash: dict[str, str] = {}
        selfie_override = None
        if live_images and _sib_tagged(sender, targets, ts_us, roster, opted_out):
            T = sum(1 for t in targets if t != sender)
            for fid in live_image_ids:
                choice = draw(st.sampled_from(("T-1", "T", "T+1", "T+2", "missing")))
                if choice == "missing":
                    continue
                face_counts[fid] = {"T-1": T - 1, "T": T, "T+1": T + 1,
                                    "T+2": T + 2}[choice]
                if hash_pool and draw(st.booleans()):
                    rendition_hash[fid] = draw(st.sampled_from(hash_pool))
                else:
                    h = f"h{i}_{fid}"
                    hash_pool.append(h)
                    rendition_hash[fid] = h
            if draw(st.integers(0, 3)) == 0:
                selfie_override = SelfieOverride(
                    draw(st.booleans()),
                    draw(st.sampled_from(pool)),
                    draw(st.sampled_from((VetoSource.REACTION, VetoSource.CLI))),
                )

        # vetoes: 0-1
        vetoes: tuple[Veto, ...] = ()
        if draw(st.integers(0, 4)) == 0:
            vetoes = (Veto(draw(st.sampled_from((*pool, "ADMIN"))),
                          draw(st.sampled_from((VetoSource.REACTION, VetoSource.CLI)))),)

        missing_runs = draw(st.sampled_from((0, 0, 0, 1, 2)))

        facts.append(Candidate(
            ts=ts,
            sender=sender,
            subtype=subtype,
            thread_ts=thread_ts,
            targets=targets,
            live_images=live_images,
            live_image_ids=live_image_ids,
            live_videos=live_videos,
            linked_images=linked_images,
            last_edit_ts=last_edit_ts,
            file_sigs=(),
            vetoes=vetoes,
            missing_runs=missing_runs,
            first_seen_targets=frozenset(first_seen),
            first_sight_edited=first_sight_edited,
            target_edited_in=tuple(target_edited_in),
            face_counts=face_counts,
            rendition_hash=rendition_hash,
            detect_attempts=0,
            selfie_override=selfie_override,
        ))

    return Scenario(
        facts=tuple(facts),
        rules=rules,
        roster=roster,
        opted_out=opted_out,
        semesters=tuple(semesters),
        tz=tz,
        review=review,
    )
