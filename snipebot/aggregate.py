"""Aggregation: the single eligibility boundary and the six standings tables.

`eligible_snipes` is the only place a snipe is included or excluded (30 section 1):
it re-runs `evaluate` over the whole ledger and keeps the COUNTED / COOLDOWN pairs
whose ts falls in the requested semester. Every table builder then only groups,
counts and sorts an `Eligibility`; none re-checks roster, opt-out, veto, season,
deletion, late-tag or self-snipe.

The layer is pure: no I/O, no clock. A ts is integer microseconds (snipebot.ts);
the only division that yields a float is the "%.2f" per-member rendering the spec
fixes on the group row (30 section 2.4), and it never enters a table cell, a hash
or a golden file. Ranking is exposed here as name-free canonical orders (metric
descending, earliest snipe ascending, ID ascending) so report.py only inserts the
display-name tiebreak and renders.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from typing import AbstractSet
from zoneinfo import ZoneInfo

from snipebot.config import DatedRules, Roster, RosterMode, Semester
from snipebot.parse import Candidate
from snipebot.rules import Status, evaluate
from snipebot.ts import US_PER_SECOND, parse_ts

# Group key for a player with no sibling group (an `extra`); defined once here and
# used by every table and section (30 section 2).
UNGROUPED = "(ungrouped)"

# Sort/rank sentinel for a person with no snipe made / received: larger than any
# real ts, so no-snipe people sort last on that key (30 section 2.3).
_NO_SNIPE_US = 1 << 62


# --------------------------------------------------------------------------- #
# Eligibility (section 1)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EligibleSnipe:
    ts: str
    ts_us: int
    date: str
    time: str
    sniper: str
    target: str
    sniper_group: str | None
    target_group: str | None
    selfie: bool


@dataclass(frozen=True)
class RejectedAttempt:
    ts: str
    ts_us: int
    date: str
    sniper: str
    target: str
    blocked_by: str


@dataclass(frozen=True)
class Eligibility:
    semester: Semester
    snipes: tuple[EligibleSnipe, ...]
    rejections: tuple[RejectedAttempt, ...]


def _local_parts(ts_us: int, tz: ZoneInfo) -> tuple[str, str]:
    """(local date "YYYY-MM-DD", local time "HH:MM:SS") in `tz` from integer us."""
    local = datetime.fromtimestamp(ts_us // US_PER_SECOND, tz=timezone.utc).astimezone(tz)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")


def eligible_snipes(
    ledger: Sequence[Candidate],
    rules: DatedRules,
    roster: Roster,
    opted_out: AbstractSet[str],
    semesters: Sequence[Semester],
    tz: ZoneInfo,
    semester: Semester,
) -> Eligibility:
    """The single boundary. Fresh `evaluate` over the whole ledger, then keep the
    COUNTED pairs (as `snipes`) and COOLDOWN pairs (as `rejections`) whose message
    ts is inside `semester`. Order follows `evaluate`'s output: ts ascending, pairs
    in mention order."""
    verdicts = evaluate(ledger, rules, roster, opted_out, semesters, tz)
    sender_by_ts = {c.ts: c.sender for c in ledger}

    snipes: list[EligibleSnipe] = []
    rejections: list[RejectedAttempt] = []
    for mv in verdicts:
        for pair in mv.pairs:
            ts_us = parse_ts(pair.ts)
            if not semester.contains(ts_us):
                continue
            if pair.status is Status.COUNTED:
                date, time = _local_parts(ts_us, tz)
                sniper = sender_by_ts[pair.ts]
                snipes.append(
                    EligibleSnipe(
                        ts=pair.ts,
                        ts_us=ts_us,
                        date=date,
                        time=time,
                        sniper=sniper,
                        target=pair.target,
                        sniper_group=roster.group_of(sniper),
                        target_group=roster.group_of(pair.target),
                        selfie=pair.selfie,
                    )
                )
            elif pair.status is Status.COOLDOWN:
                date, _ = _local_parts(ts_us, tz)
                rejections.append(
                    RejectedAttempt(
                        ts=pair.ts,
                        ts_us=ts_us,
                        date=date,
                        sniper=sender_by_ts[pair.ts],
                        target=pair.target,
                        blocked_by=pair.blocked_by or "",
                    )
                )
    return Eligibility(
        semester=semester,
        snipes=tuple(snipes),
        rejections=tuple(rejections),
    )


def _gkey(group: str | None) -> str:
    return UNGROUPED if group is None else group


# --------------------------------------------------------------------------- #
# Points (rendering of 00 section 4 "Points"); read off `selfie: bool` only
# --------------------------------------------------------------------------- #

def _person_points(snipes: Sequence[EligibleSnipe], person: str) -> int:
    """This person's three point kinds summed: a plain snipe point per COUNTED pair
    they sent with selfie == False, one selfie photo point per distinct SELFIE
    message ts they sent, and a participation point per COUNTED pair they received
    with selfie == True."""
    plain = sum(1 for s in snipes if s.sniper == person and not s.selfie)
    photo_ts = {s.ts for s in snipes if s.sniper == person and s.selfie}
    participation = sum(1 for s in snipes if s.target == person and s.selfie)
    return plain + len(photo_ts) + participation


# --------------------------------------------------------------------------- #
# 2.1 snipes
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SnipeRow:
    ts_us: int
    date: str
    time: str
    sniper: str
    target: str
    sniper_group: str
    target_group: str


def build_snipes_table(elig: Eligibility, roster: Roster) -> tuple[SnipeRow, ...]:
    rows = [
        SnipeRow(
            ts_us=s.ts_us,
            date=s.date,
            time=s.time,
            sniper=s.sniper,
            target=s.target,
            sniper_group=_gkey(s.sniper_group),
            target_group=_gkey(s.target_group),
        )
        for s in elig.snipes
    ]
    rows.sort(key=lambda r: r.ts_us)
    return tuple(rows)


# --------------------------------------------------------------------------- #
# 2.2 daily
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DailyRow:
    date: str
    snipes: int
    unique_snipers: int
    unique_targets: int
    cooldown_rejections: int
    points: int


def build_daily_table(elig: Eligibility) -> tuple[DailyRow, ...]:
    dates: set[str] = set()
    for s in elig.snipes:
        dates.add(s.date)
    for r in elig.rejections:
        dates.add(r.date)

    rows: list[DailyRow] = []
    for date in sorted(dates):
        day = [s for s in elig.snipes if s.date == date]
        plain = sum(1 for s in day if not s.selfie)
        participation = sum(1 for s in day if s.selfie)
        photos = {s.ts for s in day if s.selfie}
        rows.append(
            DailyRow(
                date=date,
                snipes=len(day),
                unique_snipers=len({s.sniper for s in day}),
                unique_targets=len({s.target for s in day}),
                cooldown_rejections=sum(1 for r in elig.rejections if r.date == date),
                points=plain + participation + len(photos),
            )
        )
    return tuple(rows)


# --------------------------------------------------------------------------- #
# 2.3 people
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PersonRow:
    person: str
    group: str
    snipes_made: int
    times_sniped: int
    unique_targets: int
    unique_snipers: int
    best_day: str | None
    points: int
    first_snipe_us: int


def _best_day(made: Sequence[EligibleSnipe]) -> str | None:
    """Local date with the most snipes made; ties broken by the earliest such date.
    None when the person made no snipe."""
    if not made:
        return None
    per_day: dict[str, int] = {}
    for s in made:
        per_day[s.date] = per_day.get(s.date, 0) + 1
    return min(per_day, key=lambda d: (-per_day[d], d))


def build_people_table(elig: Eligibility, roster: Roster) -> tuple[PersonRow, ...]:
    people: set[str] = set()
    for s in elig.snipes:
        people.add(s.sniper)
        people.add(s.target)

    rows: list[PersonRow] = []
    for person in people:
        made = [s for s in elig.snipes if s.sniper == person]
        received = [s for s in elig.snipes if s.target == person]
        rows.append(
            PersonRow(
                person=person,
                group=_gkey(roster.group_of(person)),
                snipes_made=len(made),
                times_sniped=len(received),
                unique_targets=len({s.target for s in made}),
                unique_snipers=len({s.sniper for s in received}),
                best_day=_best_day(made),
                points=_person_points(elig.snipes, person),
                first_snipe_us=min((s.ts_us for s in made), default=_NO_SNIPE_US),
            )
        )
    rows.sort(
        key=lambda r: (-r.snipes_made, -r.times_sniped, r.first_snipe_us, r.person)
    )
    return tuple(rows)


# --------------------------------------------------------------------------- #
# 2.4 groups
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GroupRow:
    group: str
    members: int
    made: int
    sniped: int
    made_num: int
    sniped_num: int
    intra_group: int
    cross_group: int
    points: int
    points_num: int

    def made_per_member(self) -> str:
        return "—" if self.members == 0 else f"{self.made_num / self.members:.2f}"

    def sniped_per_member(self) -> str:
        return "—" if self.members == 0 else f"{self.sniped_num / self.members:.2f}"

    def points_per_member(self) -> str:
        return "—" if self.members == 0 else f"{self.points_num / self.members:.2f}"


def _per_member(numerator: int, members: int) -> Fraction:
    """Exact per-member value for ordering (no float). A members == 0 row sorts as
    if its value were -1, below every non-negative value (30 section 2.4)."""
    return Fraction(-1) if members == 0 else Fraction(numerator, members)


def build_groups_table(
    elig: Eligibility, roster: Roster, opted_out: AbstractSet[str]
) -> tuple[GroupRow, ...]:
    intra = roster.count_intra_group

    # Candidate keys: every rostered player's group, plus every group a snipe touches.
    keys: set[str] = set()
    for entry in roster.entries.values():
        keys.add(_gkey(entry.group))
    for s in elig.snipes:
        keys.add(_gkey(s.sniper_group))
        keys.add(_gkey(s.target_group))

    # A person is in exactly one group row (its key); points sum over those people.
    people_points: dict[str, int] = {}
    for s in elig.snipes:
        for person in (s.sniper, s.target):
            if person not in people_points:
                people_points[person] = _person_points(elig.snipes, person)

    rows: list[GroupRow] = []
    for key in keys:
        members = sum(
            1
            for u, e in roster.entries.items()
            if _gkey(e.group) == key and u not in opted_out
        )
        if roster.mode is RosterMode.AUTO and key == UNGROUPED:
            # players.mode auto lists no ungrouped players: the (ungrouped) members are
            # the ungrouped people active in this period's snipes (30 §2.4, E-W4-42).
            members = len({
                person
                for s in elig.snipes
                for person in (s.sniper, s.target)
                if roster.group_of(person) is None and person not in opted_out
            })

        by_member_made = [s for s in elig.snipes if _gkey(s.sniper_group) == key]
        into_member = [s for s in elig.snipes if _gkey(s.target_group) == key]
        intra_count = sum(
            1
            for s in elig.snipes
            if _gkey(s.sniper_group) == key and _gkey(s.target_group) == key
        )
        cross_count = sum(
            1
            for s in elig.snipes
            if _gkey(s.sniper_group) == key and _gkey(s.target_group) != key
        )
        into_from_other = sum(
            1
            for s in elig.snipes
            if _gkey(s.target_group) == key and _gkey(s.sniper_group) != key
        )

        if intra:
            made = len(by_member_made)
            sniped = len(into_member)
        else:
            made = cross_count
            sniped = into_from_other

        points = sum(
            pts
            for person, pts in people_points.items()
            if _gkey(roster.group_of(person)) == key
        )

        # A row is kept only if the group has any member or any counted snipe (30
        # section 2.4); this drops real sibling groups whose sole player opted out
        # and no snipe touches, as well as an empty "(ungrouped)" key.
        touches = bool(by_member_made) or bool(into_member) or members > 0
        if not touches:
            continue

        rows.append(
            GroupRow(
                group=key,
                members=members,
                made=made,
                sniped=sniped,
                made_num=made,
                sniped_num=sniped,
                intra_group=intra_count,
                cross_group=cross_count,
                points=points,
                points_num=points,
            )
        )

    def sort_key(r: GroupRow):
        return (
            -_per_member(r.points_num, r.members),
            -_per_member(r.made_num, r.members),
            -r.points,
            -r.made,
            r.group,
        )

    real = sorted((r for r in rows if r.group != UNGROUPED), key=sort_key)
    ungrouped = [r for r in rows if r.group == UNGROUPED]
    return tuple(real) + tuple(ungrouped)


# --------------------------------------------------------------------------- #
# 2.5 most_sniped
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MostSnipedRow:
    rank: int
    person: str
    group: str
    times_sniped: int
    top_sniper_of_them: str
    first_sniped_us: int


def _top_sniper_of(target: str, snipes: Sequence[EligibleSnipe]) -> str:
    """Sniper with the most COUNTED snipes on `target`; ties broken by the earliest
    snipe of that (sniper, target) pair, then sniper ID ascending."""
    counts: dict[str, int] = {}
    earliest: dict[str, int] = {}
    for s in snipes:
        if s.target != target:
            continue
        counts[s.sniper] = counts.get(s.sniper, 0) + 1
        earliest[s.sniper] = min(earliest.get(s.sniper, _NO_SNIPE_US), s.ts_us)
    return min(counts, key=lambda u: (-counts[u], earliest[u], u))


def build_most_sniped_table(
    elig: Eligibility, roster: Roster
) -> tuple[MostSnipedRow, ...]:
    targets: dict[str, list[EligibleSnipe]] = {}
    for s in elig.snipes:
        targets.setdefault(s.target, []).append(s)

    ordered = sorted(
        targets.items(),
        key=lambda kv: (
            -len(kv[1]),
            min(s.ts_us for s in kv[1]),
            kv[0],
        ),
    )

    rows: list[MostSnipedRow] = []
    for index, (person, received) in enumerate(ordered):
        times = len(received)
        # Standard competition ranking on times_sniped alone (1, 2, 2, 4).
        if rows and rows[-1].times_sniped == times:
            rank = rows[-1].rank
        else:
            rank = index + 1
        rows.append(
            MostSnipedRow(
                rank=rank,
                person=person,
                group=_gkey(roster.group_of(person)),
                times_sniped=times,
                top_sniper_of_them=_top_sniper_of(person, elig.snipes),
                first_sniped_us=min(s.ts_us for s in received),
            )
        )
    return tuple(rows)


# --------------------------------------------------------------------------- #
# 2.6 pairs
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PairRow:
    sniper: str
    target: str
    count: int
    points: int
    first_us: int


def build_pairs_table(elig: Eligibility) -> tuple[PairRow, ...]:
    pairs: dict[tuple[str, str], list[EligibleSnipe]] = {}
    for s in elig.snipes:
        pairs.setdefault((s.sniper, s.target), []).append(s)

    rows: list[PairRow] = []
    for (sniper, target), members in pairs.items():
        plain = sum(1 for s in members if not s.selfie)
        participation = sum(1 for s in members if s.selfie)
        rows.append(
            PairRow(
                sniper=sniper,
                target=target,
                count=len(members),
                points=plain + participation,
                first_us=min(s.ts_us for s in members),
            )
        )
    rows.sort(key=lambda r: (-r.count, r.first_us, r.sniper, r.target))
    return tuple(rows)


# --------------------------------------------------------------------------- #
# 3. Ranking (name-free canonical orders; report.py inserts the name tiebreak)
# --------------------------------------------------------------------------- #

def rank_top_snipers(people: Sequence[PersonRow]) -> tuple[PersonRow, ...]:
    """`top_snipers` order: points desc, snipes_made desc, earliest snipe made asc,
    person ID asc."""
    return tuple(
        sorted(
            people,
            key=lambda p: (-p.points, -p.snipes_made, p.first_snipe_us, p.person),
        )
    )


def rank_most_sniped(rows: Sequence[MostSnipedRow]) -> tuple[MostSnipedRow, ...]:
    """`most_sniped` order: times_sniped desc, earliest snipe received asc, ID asc.
    `build_most_sniped_table` already emits this order; kept as a pure function so
    report.py never re-sorts."""
    return tuple(
        sorted(
            rows,
            key=lambda r: (-r.times_sniped, r.first_sniped_us, r.person),
        )
    )


def rank_pairs(rows: Sequence[PairRow]) -> tuple[PairRow, ...]:
    """`pairs` order: count desc, earliest snipe asc, sniper ID asc, target ID asc."""
    return tuple(
        sorted(rows, key=lambda r: (-r.count, r.first_us, r.sniper, r.target))
    )


def rank_groups(rows: Sequence[GroupRow]) -> tuple[GroupRow, ...]:
    """`groups` ranking order: points_per_member desc, made_per_member desc, points
    desc, made desc, group ID asc, with "(ungrouped)" always last (it is not a
    contestant, 30 section 3)."""
    real = sorted(
        (r for r in rows if r.group != UNGROUPED),
        key=lambda r: (
            -_per_member(r.points_num, r.members),
            -_per_member(r.made_num, r.members),
            -r.points,
            -r.made,
            r.group,
        ),
    )
    ungrouped = [r for r in rows if r.group == UNGROUPED]
    return tuple(real) + tuple(ungrouped)


@dataclass(frozen=True)
class TopNCut:
    head: tuple
    boundary_tie: tuple
    cutoff_value: object | None


def top_n_cutoff(
    rows: Sequence[object], top_n: int, metric: Callable[[object], object]
) -> TopNCut:
    """Split already-ranked `rows` at `top_n` (30 section 3). `head` is always shown;
    `boundary_tie` is every row after the head whose leading `metric` equals the value
    at the cutoff position; rows strictly below are dropped. When `len(rows) <= top_n`
    there is no overflow."""
    if len(rows) <= top_n:
        return TopNCut(head=tuple(rows), boundary_tie=(), cutoff_value=None)
    cut = metric(rows[top_n - 1])
    head = tuple(rows[:top_n])
    tie = tuple(r for r in rows[top_n:] if metric(r) == cut)
    return TopNCut(head=head, boundary_tie=tie, cutoff_value=cut)
