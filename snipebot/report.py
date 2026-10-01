"""Digest rendering: name resolution, Block Kit assembly and the numbers hash.

`render_digest` turns a precomputed `Eligibility` into a Slack block list plus a
notification fallback and the message metadata (00-data section 9). Standings print
plain display names (never `<@…>` mentions), so a digest pings nobody. Every section
renders from the period key, never from `now`, so a late run reports the right period.

The numbers hash is over IDs and integers only, in render order, so a revision fires
on a standings change but not on a cosmetic name-cache change (00-data section 9).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from fractions import Fraction
from typing import AbstractSet

from zoneinfo import ZoneInfo

from snipebot.aggregate import (
    Eligibility,
    GroupRow,
    MostSnipedRow,
    PairRow,
    PersonRow,
    UNGROUPED,
    _gkey,
    build_groups_table,
    build_most_sniped_table,
    build_pairs_table,
    build_people_table,
    rank_groups,
    rank_most_sniped,
    rank_pairs,
    rank_top_snipers,
    top_n_cutoff,
)
from snipebot.config import Cadence, ReportSpec, Roster, Section, Semester
from snipebot.parse import DigestMetadata
from snipebot.ts import US_PER_SECOND

# Slack Block Kit limits (30 section 5.5).
MAX_BLOCKS = 50
MAX_SECTION_TEXT = 3000
MAX_FIELDS = 10
MAX_FIELD_TEXT = 2000
MAX_HEADER_TEXT = 150

_SCOPE_FOR = {
    Cadence.DAILY: Section.DAY,
    Cadence.WEEKLY: Section.WEEK,
    Cadence.FINAL: Section.SEMESTER,
}
_SCOPE_TITLE = {
    Section.DAY: "*Today*",
    Section.WEEK: "*This week*",
    Section.SEMESTER: "*Semester*",
}
_SCOPE_SECTIONS = frozenset(_SCOPE_TITLE)


class DigestTooLargeError(Exception):
    """A Block Kit limit would be exceeded (30 section 5.5). Reaches sync step 9 as an
    untolerated error -> exit 1, never a silently truncated digest."""


# --------------------------------------------------------------------------- #
# Name resolution (section 4)
# --------------------------------------------------------------------------- #

class NameResolver:
    """Built from the users cache (id -> display name, 00 section 2 `users.json`) and
    the roster (for group-tag disambiguation). Pure given its inputs."""

    def __init__(self, cache: Mapping[str, str], roster: Roster) -> None:
        self._cache = cache
        self._roster = roster

    def resolve_all(self, ids: AbstractSet[str],
                    max_len: int | None = None) -> dict[str, str]:
        """Display name per ID, disambiguated within `ids` only (section 4). With
        `max_len` (a digest, E-W4-27) every name is clipped to that many code points;
        the base is clipped first, so a disambiguation suffix survives the clip."""
        return {i: _clip_name(base, suffix, max_len)
                for i, (base, suffix) in self._resolve_parts(ids).items()}

    def _resolve_parts(self, ids: AbstractSet[str]) -> dict[str, tuple[str, str]]:
        """(base, disambiguation suffix) per ID; the display name is their concatenation."""
        result: dict[str, tuple[str, str]] = {}
        by_base: dict[str, list[str]] = defaultdict(list)
        for i in ids:
            raw = self._cache.get(i)
            base = raw.strip() if isinstance(raw, str) else ""
            if base == "":
                # Unresolved fallback: bracketed raw ID, inherently unique, never pings.
                result[i] = (f"[{i}]", "")
            else:
                by_base[base].append(i)

        for base, members in by_base.items():
            if len(members) == 1:
                result[members[0]] = (base, "")
                continue
            # Collision by name: append the group tag.
            by_group: dict[str, list[str]] = defaultdict(list)
            for i in members:
                group = self._roster.group_of(i) or "ungrouped"
                by_group[group].append(i)
            for group, gmembers in by_group.items():
                if len(gmembers) == 1:
                    result[gmembers[0]] = (base, f" ({group})")
                else:
                    # Collision by name AND group: the raw ID makes it unique.
                    for i in gmembers:
                        result[i] = (base, f" ({group}) [{i}]")
        return result


# Digest names are clipped to this many code points before escaping (section 4,
# E-W4-27); a clipped name ends in `...` inside the limit. This bounds section 5.5.
MAX_DIGEST_NAME = 40
_ELLIPSIS = "..."


def _clip_name(base: str, suffix: str, max_len: int | None) -> str:
    """`base + suffix`, clipped to `max_len` code points when it is longer. The base
    takes the cut (keeping the suffix) unless the suffix leaves it no room."""
    name = base + suffix
    if max_len is None or len(name) <= max_len:
        return name
    room = max_len - len(suffix) - len(_ELLIPSIS)
    if room >= 1:
        return base[:room] + _ELLIPSIS + suffix
    return name[:max_len - len(_ELLIPSIS)] + _ELLIPSIS


def _escape(text: str) -> str:
    """Escape the three characters Slack mrkdwn requires, `&` first (section 4)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mrkdwn(text: str) -> dict:
    """A mrkdwn text object. `verbatim: true` keeps every one out of Slack's mention
    preprocessing, so a bare @channel/@here/@everyone in a name pings nobody
    (section 4, E-W4-1)."""
    return {"type": "mrkdwn", "text": text, "verbatim": True}


# --------------------------------------------------------------------------- #
# Period bounds and labels (section 5.2)
# --------------------------------------------------------------------------- #

def _sem_dates(semester: Semester, tz: ZoneInfo) -> tuple[date, date]:
    start = datetime.fromtimestamp(semester.start_us // US_PER_SECOND, tz=timezone.utc).astimezone(tz).date()
    end = datetime.fromtimestamp(semester.end_us // US_PER_SECOND, tz=timezone.utc).astimezone(tz).date()
    return start, end


def _parse_week(tail: str) -> tuple[int, int]:
    year_s, week_s = tail.split("-W")
    return int(year_s), int(week_s)


def period_bounds(section: Section, period_key: str, semester: Semester,
                  tz: ZoneInfo) -> tuple[str, str]:
    """Inclusive local-date range ("YYYY-MM-DD", "YYYY-MM-DD"), clipped to `semester`."""
    tail = period_key.split(":", 1)[1]
    s_start, s_end = _sem_dates(semester, tz)
    if section is Section.DAY:
        d = date.fromisoformat(tail)
        return d.isoformat(), d.isoformat()
    if section is Section.WEEK:
        iso_year, iso_week = _parse_week(tail)
        monday = date.fromisocalendar(iso_year, iso_week, 1)
        sunday = date.fromisocalendar(iso_year, iso_week, 7)
        lo = max(monday, s_start)
        hi = min(sunday, s_end)
        return lo.isoformat(), hi.isoformat()
    # SEMESTER
    return s_start.isoformat(), s_end.isoformat()


def period_label(section: Section, period_key: str, semester: Semester,
                 tz: ZoneInfo) -> str:
    """The header/`text` label; states the local dates the digest covers (section 5.2)."""
    tail = period_key.split(":", 1)[1]
    lo, hi = period_bounds(section, period_key, semester, tz)
    if section is Section.DAY:
        return lo
    if section is Section.WEEK:
        iso_year, iso_week = _parse_week(tail)
        monday = date.fromisocalendar(iso_year, iso_week, 1).isoformat()
        sunday = date.fromisocalendar(iso_year, iso_week, 7).isoformat()
        marker = "" if (lo == monday and hi == sunday) else ", partial week"
        return f"{tail} ({lo} to {hi}{marker})"
    # SEMESTER
    return f"{semester.name} ({lo} to {hi})"


# --------------------------------------------------------------------------- #
# Scope totals (section 5.4)
# --------------------------------------------------------------------------- #

def _scope_totals(elig: Eligibility, lo: str, hi: str) -> tuple[int, int, int, int, int]:
    snipes = [s for s in elig.snipes if lo <= s.date <= hi]
    rejections = [r for r in elig.rejections if lo <= r.date <= hi]
    plain = sum(1 for s in snipes if not s.selfie)
    participation = sum(1 for s in snipes if s.selfie)
    photos = len({s.ts for s in snipes if s.selfie})
    points = plain + participation + photos
    unique_snipers = len({s.sniper for s in snipes})
    unique_targets = len({s.target for s in snipes})
    return len(snipes), points, unique_snipers, unique_targets, len(rejections)


def _scope_block(section: Section, period_key: str, semester: Semester,
                 elig: Eligibility, tz: ZoneInfo) -> dict:
    lo, hi = period_bounds(section, period_key, semester, tz)
    snipes, points, u_snipers, u_targets, rejections = _scope_totals(elig, lo, hi)
    title = f"{_SCOPE_TITLE[section]} — {period_label(section, period_key, semester, tz)}"
    if snipes == 0:
        title += "\n_No snipes recorded._"
    fields = [
        _mrkdwn(f"*Snipes*\n{snipes}"),
        _mrkdwn(f"*Points*\n{points}"),
        _mrkdwn(f"*Unique snipers*\n{u_snipers}"),
        _mrkdwn(f"*Unique targets*\n{u_targets}"),
        _mrkdwn(f"*Cooldown rejections*\n{rejections}"),
    ]
    if len(fields) > MAX_FIELDS or any(len(f["text"]) > MAX_FIELD_TEXT for f in fields):
        raise DigestTooLargeError(f"scope section {section.value} exceeds field limits")
    if len(title) > MAX_SECTION_TEXT:
        raise DigestTooLargeError(f"scope section {section.value} text exceeds {MAX_SECTION_TEXT}")
    return {"type": "section", "text": _mrkdwn(title), "fields": fields}


# --------------------------------------------------------------------------- #
# Ranked-section windows (section 3) shared by rendering and the numbers hash
# --------------------------------------------------------------------------- #

def _group_metric(row: GroupRow) -> Fraction:
    return Fraction(-1) if row.members == 0 else Fraction(row.points_num, row.members)


def _ranked_window(section: Section, elig: Eligibility, roster: Roster,
                   opted_out: AbstractSet[str], top_n: int) -> tuple[tuple, Callable]:
    """The section 3 logical window (head + boundary ties) in canonical ID order, and
    the leading-metric callable. Name tiebreaks and char-based trimming are not applied
    here — the window is what the numbers hash covers. A row whose leading metric is
    zero never enters a person or pair ranking (section 3, E-W4-32); the groups table
    keeps every group with members."""
    if section is Section.TOP_SNIPERS:
        rows = rank_top_snipers(build_people_table(elig, roster))
        metric = lambda r: r.points  # noqa: E731
    elif section is Section.MOST_SNIPED:
        rows = rank_most_sniped(build_most_sniped_table(elig, roster))
        metric = lambda r: r.times_sniped  # noqa: E731
    elif section is Section.GROUPS:
        ranked = rank_groups(build_groups_table(elig, roster, opted_out))
        rows = tuple(r for r in ranked if r.group != UNGROUPED and r.members > 0)
        metric = _group_metric
    elif section is Section.PAIRS:
        rows = rank_pairs(build_pairs_table(elig))
        metric = lambda r: r.count  # noqa: E731
    else:
        raise ValueError(f"not a ranked section: {section}")
    if section is not Section.GROUPS:
        # Groups keep every members > 0 row (section 5.4); only person and pair
        # rankings drop a zero leading metric.
        rows = tuple(r for r in rows if metric(r) > 0)
    cut = top_n_cutoff(rows, top_n, metric)
    window = cut.head + cut.boundary_tie
    return window, metric


def _section_ids(section: Section, window: tuple) -> set[str]:
    ids: set[str] = set()
    if section is Section.TOP_SNIPERS:
        for r in window:
            ids.add(r.person)
    elif section is Section.MOST_SNIPED:
        for r in window:
            ids.add(r.person)
            ids.add(r.top_sniper_of_them)
    elif section is Section.PAIRS:
        for r in window:
            ids.add(r.sniper)
            ids.add(r.target)
    return ids


def _rendered_order(section: Section, window: tuple, names: Mapping[str, str]) -> list:
    """Insert the display-name tiebreak (section 3 position 3) into the canonical order.
    Only rows tied on metric AND earliest snipe can move; every other pair keeps its
    canonical order, so a name change never reshuffles the standings."""
    if section is Section.TOP_SNIPERS:
        key = lambda r: (-r.points, -r.snipes_made, r.first_snipe_us,  # noqa: E731
                         names[r.person].casefold(), r.person)
    elif section is Section.MOST_SNIPED:
        key = lambda r: (-r.times_sniped, r.first_sniped_us,  # noqa: E731
                         names[r.person].casefold(), r.person)
    elif section is Section.PAIRS:
        key = lambda r: (-r.count, r.first_us,  # noqa: E731
                         names[r.sniper].casefold(), names[r.target].casefold(),
                         r.sniper, r.target)
    else:  # GROUPS: no display name; canonical order is the rendered order.
        return list(window)
    return sorted(window, key=key)


def _row_line(section: Section, row, position: int, names: Mapping[str, str]) -> str:
    if section is Section.TOP_SNIPERS:
        name = _escape(names[row.person])
        return f"{position}. {name} — {row.points} pts ({row.snipes_made} snipes)"
    if section is Section.MOST_SNIPED:
        name = _escape(names[row.person])
        top = _escape(names[row.top_sniper_of_them])
        return f"{row.rank}. {name} — {row.times_sniped} (top: {top})"
    if section is Section.GROUPS:
        group = _escape(row.group)
        return (f"{position}. {group} — {row.points_per_member()} pts/member "
                f"({row.points} pts, {row.made}/{row.members} made)")
    # PAIRS
    sniper = _escape(names[row.sniper])
    target = _escape(names[row.target])
    return f"{position}. {sniper} → {target} — {row.count}"


def _cutoff_value_str(section: Section, cutoff_row) -> str:
    if section is Section.TOP_SNIPERS:
        return str(cutoff_row.points)
    if section is Section.MOST_SNIPED:
        return str(cutoff_row.times_sniped)
    if section is Section.GROUPS:
        return cutoff_row.points_per_member()
    return str(cutoff_row.count)


def _others_tied_line(collapsed: int, cutoff_str: str) -> str:
    """The boundary-tie collapse line (section 5.5): `\n...and +K others tied at V`."""
    return f"\n…and +{collapsed} others tied at {cutoff_str}"


_RANKED_TITLE = {
    Section.TOP_SNIPERS: "*Top snipers*",
    Section.MOST_SNIPED: "*Most sniped*",
    Section.GROUPS: "*Groups*",
    Section.PAIRS: "*Top pairs*",
}


def _ranked_block(section: Section, elig: Eligibility, roster: Roster,
                  opted_out: AbstractSet[str], names: NameResolver, top_n: int) -> dict:
    window, _metric = _ranked_window(section, elig, roster, opted_out, top_n)
    title = _RANKED_TITLE[section]

    # Zero-metric person and pair rows never enter the window (E-W4-32); groups keep
    # their members > 0 rows and collapse only when no counted snipe touches any group.
    empty = not window or (section is Section.GROUPS
                           and all(r.made == 0 and r.sniped == 0 and r.points == 0
                                   for r in window))
    if empty:
        text = f"{title}\n_No snipes yet._"
        return {"type": "section", "text": _mrkdwn(text)}

    # The name tiebreak orders by the full display name; lines print the clipped one
    # (section 4, E-W4-27).
    ids = _section_ids(section, window)
    ordered = _rendered_order(section, window, names.resolve_all(ids))
    resolved = names.resolve_all(ids, max_len=MAX_DIGEST_NAME)

    # The overflow collapses only boundary-tie rows (all at the cutoff value V), so the
    # value shown is well formed. Collapse can be triggered either by the top_n cutoff or
    # by the character budget; in both cases every collapsed row ties at V.
    text = title
    shown = 0
    line_lens: list[int] = []
    for row in ordered:
        line = _row_line(section, row, shown + 1, resolved)
        if len(text) + len("\n" + line) <= MAX_SECTION_TEXT:
            text += "\n" + line
            line_lens.append(len("\n" + line))
            shown += 1
        else:
            break

    # Only boundary-tie rows may collapse (section 3, section 5.5). The head is the first
    # min(top_n, len(rows)) rows and is always shown: with len(rows) <= top_n every row is
    # head and nothing collapses (section 3 case 1, E-W4-20a); otherwise the rest of the
    # window ties at V, so any row hidden after the head is a boundary tie. A head that
    # does not fit is a violated limit (DigestTooLargeError), never a silently truncated
    # section.
    head = min(top_n, len(ordered))
    if shown < head:
        raise DigestTooLargeError(
            f"ranked section {section.value} cannot show its top_n head rows "
            f"within {MAX_SECTION_TEXT}")

    if shown < len(ordered):
        # Every unshown row is a boundary tie at V (head always fits, section 5.5), so V
        # is the metric value of the first collapsed row.
        # When the filled rows leave no room for the overflow line, collapse more
        # boundary-tie rows (never a head row, never across a value change) until it
        # fits; K can gain a digit, so the line is re-measured on every pass.
        overflow = _others_tied_line(len(ordered) - shown,
                                     _cutoff_value_str(section, ordered[shown]))
        while (len(text) + len(overflow) > MAX_SECTION_TEXT and shown > head
               and _metric(ordered[shown - 1]) == _metric(ordered[shown])):
            text = text[:len(text) - line_lens.pop()]
            shown -= 1
            overflow = _others_tied_line(len(ordered) - shown,
                                         _cutoff_value_str(section, ordered[shown]))
        if len(text) + len(overflow) > MAX_SECTION_TEXT:
            raise DigestTooLargeError(f"ranked section {section.value} cannot fit its overflow line")
        text += overflow

    if len(text) > MAX_SECTION_TEXT:
        raise DigestTooLargeError(f"ranked section {section.value} text exceeds {MAX_SECTION_TEXT}")
    return {"type": "section", "text": _mrkdwn(text)}


# --------------------------------------------------------------------------- #
# numbers_hash (section 5.6, must match 00-data section 9)
# --------------------------------------------------------------------------- #

def _rendered_selfie_photos(report: ReportSpec, period_key: str, semester: Semester,
                            elig: Eligibility, roster: Roster,
                            opted_out: AbstractSet[str], tz: ZoneInfo) -> set[str]:
    """Distinct SELFIE message ts (photos) carried by the rows the digest renders: the
    period-clipped scope sections plus each ranked section's logical window (30 section
    5.4). A selfie photo lands in a scope section when its message falls in the clipped
    range, and in a ranked section when its pair is aggregated into a windowed row —
    top_snipers by poster or tagged sib, most_sniped by tagged sib, groups by either
    endpoint's group, pairs by the exact sniper->target. F = len(...) gates the legend
    and the `selfie_legend` boolean; it reads IDs and dates only, never the name cache,
    so a cosmetic rename never flips it (30 section 5.6 logical window)."""
    selfies = [s for s in elig.snipes if s.selfie]
    if not selfies:
        return set()
    photos: set[str] = set()
    for section in report.sections:
        if section in _SCOPE_SECTIONS:
            lo, hi = period_bounds(section, period_key, semester, tz)
            photos.update(s.ts for s in selfies if lo <= s.date <= hi)
            continue
        window, _metric = _ranked_window(section, elig, roster, opted_out, report.top_n)
        if section is Section.TOP_SNIPERS:
            people = {r.person for r in window}
            photos.update(s.ts for s in selfies
                          if s.sniper in people or s.target in people)
        elif section is Section.MOST_SNIPED:
            people = {r.person for r in window}
            photos.update(s.ts for s in selfies if s.target in people)
        elif section is Section.GROUPS:
            groups = {r.group for r in window}
            photos.update(s.ts for s in selfies
                          if _gkey(s.sniper_group) in groups
                          or _gkey(s.target_group) in groups)
        elif section is Section.PAIRS:
            pairs = {(r.sniper, r.target) for r in window}
            photos.update(s.ts for s in selfies if (s.sniper, s.target) in pairs)
    return photos


def numbers_payload(report: ReportSpec, period_key: str, semester: Semester,
                    elig: Eligibility, roster: Roster, opted_out: AbstractSet[str],
                    tz: ZoneInfo) -> list:
    """Ordered [[section_value, rows], ...] in report.sections order, then one trailing
    ["selfie_legend", F > 0] boolean (30 section 5.6). IDs + ints + that bool only."""
    payload: list = []
    for section in report.sections:
        if section in _SCOPE_SECTIONS:
            lo, hi = period_bounds(section, period_key, semester, tz)
            snipes, points, u_snipers, u_targets, rejections = _scope_totals(elig, lo, hi)
            payload.append([section.value, [[snipes, points, u_snipers, u_targets, rejections]]])
            continue

        window, _metric = _ranked_window(section, elig, roster, opted_out, report.top_n)
        rows: list = []
        if section is Section.TOP_SNIPERS:
            rows = [[r.person, r.points, r.snipes_made] for r in window]
        elif section is Section.MOST_SNIPED:
            rows = [[r.person, r.times_sniped, r.top_sniper_of_them] for r in window]
        elif section is Section.GROUPS:
            rows = [[r.group, r.made, r.sniped, r.members, r.intra_group, r.cross_group, r.points]
                    for r in window]
        elif section is Section.PAIRS:
            rows = [[r.sniper, r.target, r.count] for r in window]
        payload.append([section.value, rows])
    payload.append(["selfie_legend", bool(_rendered_selfie_photos(
        report, period_key, semester, elig, roster, opted_out, tz))])
    return payload


def numbers_hash(payload: list) -> str:
    """sha256 of the canonical JSON of `payload` (00-data section 3 rules)."""
    canon = json.dumps(payload, ensure_ascii=True, separators=(",", ":"),
                       sort_keys=False, allow_nan=False)
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


# --------------------------------------------------------------------------- #
# render_digest (section 5)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DigestRender:
    blocks: tuple[dict, ...]
    text: str
    metadata: DigestMetadata


def render_digest(
    report: ReportSpec,
    period_key: str,
    semester: Semester,
    elig: Eligibility,
    roster: Roster,
    opted_out: AbstractSet[str],
    names: NameResolver,
    tz: ZoneInfo,
    revision: int,
    selfie_emoji: str | None,
) -> DigestRender:
    scope = _SCOPE_FOR[report.cadence]
    label = period_label(scope, period_key, semester, tz)
    header_text = f"Snipes: {label}"
    if len(header_text) > MAX_HEADER_TEXT:
        raise DigestTooLargeError(f"header exceeds {MAX_HEADER_TEXT} chars")

    # F: distinct selfie photos among the rendered rows (section 5.4) — the scope
    # sections' clipped range plus each ranked section's window. Gates the legend and,
    # through numbers_payload, the `selfie_legend` revision boolean (section 5.6).
    selfie_photos = len(_rendered_selfie_photos(
        report, period_key, semester, elig, roster, opted_out, tz))

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text, "emoji": True}},
    ]
    if revision > 0:
        blocks.append({"type": "context", "elements": [_mrkdwn("_revised_")]})

    first = True
    for section in report.sections:
        if not first:
            blocks.append({"type": "divider"})
        first = False
        if section in _SCOPE_SECTIONS:
            blocks.append(_scope_block(section, period_key, semester, elig, tz))
        else:
            blocks.append(_ranked_block(section, elig, roster, opted_out, names, report.top_n))

    if selfie_photos > 0 and selfie_emoji is not None:
        legend = (f":{selfie_emoji}: sibfam selfie: +1 point each to the poster and "
                  f"every tagged sib in frame.")
        blocks.append({"type": "context", "elements": [_mrkdwn(legend)]})

    if len(blocks) > MAX_BLOCKS:
        raise DigestTooLargeError(f"digest has {len(blocks)} blocks (limit {MAX_BLOCKS})")

    payload = numbers_payload(report, period_key, semester, elig, roster, opted_out, tz)
    metadata = DigestMetadata(
        event_type="snipe_digest",
        report=report.name,
        period_key=period_key,
        channel=report.post_to or "",
        semester=semester.name,
        numbers_hash=numbers_hash(payload),
        revision=revision,
    )
    return DigestRender(blocks=tuple(blocks), text=header_text, metadata=metadata)
