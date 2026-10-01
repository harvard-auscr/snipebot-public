"""Parse and validate config.yaml into frozen, resolved dataclasses.

This module is the single site where local wall-clock config values are
converted to UTC integer microseconds in the configured timezone, where dated
rules are patched and sorted, and where the roster join instants are computed.
No YAML is read anywhere downstream: every consumer sees the resolved objects.

It also homes the resolved config dataclasses shared across the package
(00-data.md section 6) and the config-only containers (40-config-cli.md
section 2.3), plus the fingerprint guard (40-config-cli.md section 3).
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import yaml

from snipebot.ts import US_PER_MINUTE, TsFormatError, parse_ts

INT_MIN_TS: int = -(1 << 62)   # "join at any time" sentinel


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps an impossible timestamp (2026-02-30) as its raw string
    and refuses a repeated mapping key.

    PyYAML's timestamp constructor raises a bare ValueError mid-parse, which loses
    the key path; as a string the value reaches _parse_date/_parse_datetime, which
    reject it with the offending key named (40 section 2.2). A repeated key would
    otherwise keep only its last value (a second `reds:` group silently replaces
    the first), so it raises DuplicateKeyError naming the key and its line (E-W4-5).
    """

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict:
        if isinstance(node, yaml.MappingNode):
            _check_duplicate_keys(self, node)
        return super().construct_mapping(node, deep=deep)


def _check_duplicate_keys(loader: yaml.SafeLoader, node: yaml.MappingNode) -> None:
    # Checked on the explicit keys, before a `<<` merge is flattened in (a merged
    # key overridden locally is YAML's documented merge behaviour, not a typo).
    seen: set[object] = set()
    for key_node, _value in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        try:
            repeated = key in seen
        except TypeError:          # an unhashable key: SafeConstructor rejects it itself
            continue
        if repeated:
            raise DuplicateKeyError(
                f"config: duplicate key {str(key)!r} at line {key_node.start_mark.line + 1}")
        seen.add(key)


def _construct_timestamp(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    try:
        return yaml.constructor.SafeConstructor.construct_yaml_timestamp(loader, node)
    except ValueError:
        return loader.construct_scalar(node)


_Loader.add_constructor("tag:yaml.org,2002:timestamp", _construct_timestamp)


# ---------------------------------------------------------------------------
# Enums (00-data.md section 6 and 40-config-cli.md section 2.3)
# ---------------------------------------------------------------------------

class Scope(str, Enum):
    PAIR = "pair"
    TARGET = "target"


class MultiTag(str, Enum):
    PER_TARGET = "per_target"
    SINGLE = "single"


class Cadence(str, Enum):
    DAILY = "daily"       # every: 1d
    WEEKLY = "weekly"     # every: 1w
    FINAL = "final"       # every: semester_end


class Section(str, Enum):
    DAY = "day"
    WEEK = "week"
    SEMESTER = "semester"
    TOP_SNIPERS = "top_snipers"
    MOST_SNIPED = "most_sniped"
    GROUPS = "groups"
    PAIRS = "pairs"


class Weekday(str, Enum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


class VetoActor(str, Enum):
    TARGET = "target"
    ADMINS = "admins"


class Persistence(str, Enum):
    GIT = "git"           # data-branch commit + push protocol (20 section 8)
    FILES = "files"       # temp-file + rename only (20 section 8.6)


class RosterMode(str, Enum):
    LISTED = "listed"     # only the configured groups + extras play (default)
    AUTO = "auto"         # every non-bot user plays; groups still dated (E-W4-42)


# Slack's built-in assistant user: never a player under players.mode auto (E-W4-42).
SLACKBOT_USER: str = "USLACKBOT"


# ---------------------------------------------------------------------------
# Resolved dataclasses (00-data.md section 6, homed here)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CooldownRule:
    microseconds: int              # cooldown.minutes * US_PER_MINUTE
    scope: Scope
    rejected_attempts_reset: bool


@dataclass(frozen=True)
class ResolvedRule:
    effective_from_us: int         # local effective_from wall-clock -> UTC us
    cooldown: CooldownRule
    multi_tag: MultiTag
    max_targets_per_message: int | None   # None = no cap (default)
    edit_grace_us: int             # edit_grace_minutes * US_PER_MINUTE
    max_snipes_per_target_per_day: int | None
    allow_self: bool
    allow_bots: bool
    count_thread_replies: bool
    count_image_links: bool
    allow_video: bool
    selfie_bonus: bool             # sibfam selfie bonus in force at this rule's date


@dataclass(frozen=True)
class DatedRules:
    entries: tuple[ResolvedRule, ...]   # fully patched, sorted by effective_from_us asc

    def in_force_at(self, ts_us: int) -> ResolvedRule:
        """Last entry whose effective_from_us <= ts_us. Raises NoRuleInForceError if none."""
        found: ResolvedRule | None = None
        for entry in self.entries:
            if entry.effective_from_us <= ts_us:
                found = entry
            else:
                break
        if found is None:
            raise NoRuleInForceError(f"no rule in force at {ts_us}")
        return found


@dataclass(frozen=True)
class RosterEntry:
    user: str
    join_us: int                   # from: local wall-clock -> UTC us; INT_MIN_TS if none
    group: str | None              # sibling group name, or None for extras
    is_bot: bool                   # resolved from the users cache at config/doctor time


@dataclass(frozen=True)
class Roster:
    entries: Mapping[str, RosterEntry]   # by user ID (under AUTO: grouped members only)
    count_intra_group: bool
    mode: RosterMode = RosterMode.LISTED
    # AUTO only: IDs the is_bot map flags as bots (empty under LISTED). An ID absent
    # from the map is a human player (E-W4-42).
    bots: frozenset[str] = frozenset()

    def is_member_at(self, user: str, ts_us: int) -> bool:
        e = self.entries.get(user)
        if self.mode is RosterMode.AUTO:
            # every human plays at every ts; a from: dates group membership only
            return not (user == SLACKBOT_USER or user in self.bots
                        or (e is not None and e.is_bot))
        return e is not None and e.join_us <= ts_us

    def is_bot(self, user: str) -> bool:
        e = self.entries.get(user)
        if self.mode is RosterMode.AUTO and user in self.bots:
            return True
        return e is not None and e.is_bot

    def group_of(self, user: str, ts_us: int | None = None) -> str | None:
        """The user's group; with `ts_us` under AUTO, None before their from: (the
        user plays ungrouped until then). Under LISTED `ts_us` changes nothing."""
        e = self.entries.get(user)
        if e is None:
            return None
        if self.mode is RosterMode.AUTO and ts_us is not None and ts_us < e.join_us:
            return None
        return e.group


@dataclass(frozen=True)
class Semester:
    name: str
    start_us: int                  # start date 00:00:00.000000 local -> UTC us (inclusive)
    end_us: int                    # end date 23:59:59.999999 local -> UTC us (inclusive)

    def contains(self, ts_us: int) -> bool:
        return self.start_us <= ts_us <= self.end_us


@dataclass(frozen=True)
class FeedbackReactions:
    counted: str
    cooldown: str
    untagged: str | None           # null = stay quiet on untagged photos
    not_counted: str
    selfie: str | None             # placed on an awarded sibfam selfie; null = stay quiet

    def emoji_for(self, status: "Status") -> str | None:  # noqa: F821 - Status lives in 00 section 4
        return getattr(self, status.value)


@dataclass(frozen=True)
class ReviewFlag:
    min_targets: int | None        # null = never flag
    emoji: str | None              # null = audit list and summary line only, no reaction


# ---------------------------------------------------------------------------
# Config-only dataclasses (40-config-cli.md section 2.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SyncSettings:
    interval_minutes: int
    scan_days: int
    history_horizon_days: int | None   # None = no horizon (paid plan)
    max_deletes_per_run: int
    large_movement_rows: int


@dataclass(frozen=True)
class ConsentConfig:
    veto_emoji: str
    veto_by: tuple[VetoActor, ...]     # who may cast a counting-reaction veto
    optout_message_ts: tuple[str, ...] # Slack ts strings of the pinned opt-out message(s)
    seed_opted_out: tuple[str, ...]    # config opted_out: seeds, deduped, sorted


@dataclass(frozen=True)
class FacesConfig:                      # section 1.10; operational, never an evaluate input
    model_path: str                    # vendored YuNet ONNX
    fetch_timeout_seconds: int         # per-image rendition fetch timeout
    max_image_bytes: int               # rendition size cap -> FileTooLarge (10 section 2)
    max_attempts: int                  # detection retries per message
    score_threshold: str               # decimal STRING; parsed only inside the detector


@dataclass(frozen=True)
class ReportSpec:
    name: str
    cadence: Cadence
    at_hour: int                       # 0..23 local
    at_minute: int                     # 0..59 local
    weekday: Weekday | None            # set iff cadence == WEEKLY
    post_to: str | None                # None -> the watched channel
    sections: tuple[Section, ...]      # config order, deduped, period section present
    top_n: int

    def resolved_channel(self, watched: str) -> str:
        return self.post_to if self.post_to is not None else watched


@dataclass(frozen=True)
class Config:
    enabled: bool
    persistence: Persistence           # git (default) | files
    channel: str                       # watched channel ID
    tz: ZoneInfo
    sync: SyncSettings
    semesters: tuple[Semester, ...]    # sorted by start_us
    rules: DatedRules
    roster: Roster
    consent: ConsentConfig
    admins: tuple[str, ...]            # deduped, sorted
    feedback: FeedbackReactions
    review: ReviewFlag                 # manual-check flag
    reports: tuple[ReportSpec, ...]    # config order preserved
    faces: FacesConfig                 # section 1.10; all-default when faces: is omitted
    # Step-9 digest switch (section 1, E-W4-39): False posts and revises no digest. An
    # output switch, never an evaluate input, so no fingerprint covers it (00 section 7).
    recaps: bool = True
    # Step-7 reaction switch (section 1, E-W4-43): False adds and removes no reaction for
    # every command, whatever its flags. An output switch like `recaps`, in no fingerprint.
    reactions: bool = True


# ---------------------------------------------------------------------------
# Error hierarchy (40-config-cli.md section 2.2)
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """Base for every config.yaml validation failure. The message names the
    offending key path and the reason; it never contains a token."""


class UnknownKeyError(ConfigError):
    """Unrecognised key at any level, including a dated rules entry."""


class MissingRequiredKeyError(ConfigError):
    """A required key is absent (including slack.channel)."""


class InvalidValueError(ConfigError):
    """Wrong type, bad enum, out-of-range int, or bad ID/ts/date/time."""


class EmptyValueError(ConfigError):
    """A required non-empty list is empty (semesters, sections)."""


class DuplicateNameError(ConfigError):
    """Duplicate semester or report name."""


class DuplicateGroupMemberError(ConfigError):
    """A user in two groups, in a group and extras, or twice in one list."""


class AutoRosterExtrasError(InvalidValueError):
    """players.extras is non-empty while players.mode is auto (E-W4-42)."""


class EmptyGroupError(ConfigError):
    """A group with no members."""


class OverlappingSemestersError(ConfigError):
    """Two semesters' [start, end] closed intervals overlap."""


class RulesEffectiveFromError(ConfigError):
    """First dated entry after the first semester start, or non-monotonic dates."""


class BadEmojiError(ConfigError):
    """An emoji name fails the Emoji pattern."""


class EmojiCollisionError(ConfigError):
    """The veto, selfie, or review emoji collides where it must stay distinct."""


class DuplicateKeyError(ConfigError):
    """A mapping key repeated at any level; names the key and its line (E-W4-5)."""


class RecapsValueError(InvalidValueError):
    """`recaps` is not a plain `true` or `false` (E-W4-39)."""


class ReactionsValueError(InvalidValueError):
    """`reactions` is not a plain `true` or `false` (E-W4-43)."""


class NoRuleInForceError(RuntimeError):
    """No dated rule governs a timestamp (a run failure, never a config error)."""


class FingerprintGuardError(RuntimeError):
    """A rules/players/semesters change would re-judge existing rows (exit 3)."""


# ---------------------------------------------------------------------------
# Scalar-format patterns (section 1)
# ---------------------------------------------------------------------------

_RE_CHANNEL = re.compile(r"^[CG][A-Z0-9]{6,}$")   # G: a legacy private channel (E-W4-28)
_RE_USER = re.compile(r"^[UW][A-Z0-9]{6,}$")
_RE_TS = re.compile(r"^\d+\.\d{6}$")
_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?$")
_RE_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_RE_EMOJI = re.compile(r"^[a-z0-9][a-z0-9_+'-]*$")
_RE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RE_THRESHOLD = re.compile(r"^(0\.[5-9][0-9]{0,2}|1\.0{1,3})$")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_UNGROUPED_LABEL = "(ungrouped)"


# ---------------------------------------------------------------------------
# Small validation helpers. Each raises with the full key path.
# ---------------------------------------------------------------------------

def _require_mapping(value: object, path: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise InvalidValueError(f"{path}: expected a mapping")
    return value


def _optional_list(mapping: Mapping, key: str, path: str) -> list:
    if key not in mapping:
        return []
    value = mapping[key]
    if not isinstance(value, list):
        raise InvalidValueError(f"{path}: expected a list")
    return value


def _reject_unknown(mapping: Mapping, allowed: set[str], path: str) -> None:
    for key in mapping:
        if key not in allowed:
            where = f"{path}.{key}" if path else str(key)
            raise UnknownKeyError(f"{where}: unknown key")


def _key_path(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


# Exact-match set of canonical IANA names: ZoneInfo alone accepts case variants
# ("utc") on case-insensitive filesystems that then fail on a Linux runner.
_available_zones = functools.cache(available_timezones)


def _get_bool(mapping: Mapping, key: str, default: bool, path: str) -> bool:
    if key not in mapping:
        return default
    value = mapping[key]
    if not isinstance(value, bool):
        raise InvalidValueError(f"{_key_path(path, key)}: expected true or false")
    return value


def _get_int(mapping: Mapping, key: str, default: int, path: str, *, minimum: int) -> int:
    if key not in mapping:
        return default
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidValueError(f"{_key_path(path, key)}: expected an integer")
    if value < minimum:
        raise InvalidValueError(f"{_key_path(path, key)}: must be >= {minimum}")
    return value


def _get_int_or_null(
    mapping: Mapping, key: str, default: int | None, path: str, *, minimum: int
) -> int | None:
    if key not in mapping:
        return default
    value = mapping[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidValueError(f"{_key_path(path, key)}: expected an integer or null")
    if value < minimum:
        raise InvalidValueError(f"{_key_path(path, key)}: must be >= {minimum}")
    return value


def _get_str(mapping: Mapping, key: str, path: str) -> str:
    value = mapping[key]
    if not isinstance(value, str):
        raise InvalidValueError(f"{_key_path(path, key)}: expected a string")
    return value


def _channel(value: object, path: str) -> str:
    if not isinstance(value, str) or not _RE_CHANNEL.fullmatch(value):
        raise InvalidValueError(f"{path}: not a channel ID (C... or G...)")
    return value


def _user(value: object, path: str) -> str:
    if not isinstance(value, str) or not _RE_USER.fullmatch(value):
        raise InvalidValueError(f"{path}: not a user ID")
    return value


def _slack_ts(value: object, path: str) -> str:
    if not isinstance(value, str) or not _RE_TS.fullmatch(value):
        raise InvalidValueError(f"{path}: not a Slack ts")
    try:
        parse_ts(value)
    except TsFormatError as exc:
        raise InvalidValueError(f"{path}: not a Slack ts") from exc
    return value


def _emoji(value: object, path: str) -> str:
    if not isinstance(value, str) or not _RE_EMOJI.fullmatch(value):
        raise BadEmojiError(f"{path}: not a bare emoji short name")
    return value


def _local_to_us(naive: datetime, tz: ZoneInfo) -> int:
    """Local wall-clock -> UTC integer microseconds, without any float."""
    aware = naive.replace(tzinfo=tz)
    delta = aware.astimezone(timezone.utc) - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _parse_date(value: object, tz: ZoneInfo, path: str, *, end: bool) -> int:
    # A bare YAML date (2026-09-01) loads as datetime.date; a string is also accepted.
    if isinstance(value, datetime):
        raise InvalidValueError(f"{path}: expected a date, not a date-time")
    if isinstance(value, date):
        base = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and _RE_DATE.fullmatch(value):
        try:
            base = datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise InvalidValueError(f"{path}: not a valid calendar date") from exc
    else:
        raise InvalidValueError(f"{path}: not a date (YYYY-MM-DD)")
    try:
        if end:
            # Inclusive end: the local midnight starting the next day, minus 1 us. That
            # midnight resolves by the 00 section 8 rules (fold=0), so a last hour
            # repeated by a fall-back at midnight stays inside (E-W4-9).
            return _local_to_us(base + timedelta(days=1), tz) - 1
        return _local_to_us(base, tz)
    except (OverflowError, ValueError) as exc:
        raise InvalidValueError(f"{path}: date outside the representable range") from exc


def _parse_datetime(value: object, tz: ZoneInfo, path: str) -> int:
    # Accepts a YAML date/timestamp or a string; local wall-clock at minute granularity.
    # A native timestamp is held to the same grammar as the string form: no seconds, no
    # UTC offset (an offset would silently shift the instant when re-read as local).
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise InvalidValueError(f"{path}: a date-time carries no UTC offset (local wall-clock only)")
        if value.second or value.microsecond:
            raise InvalidValueError(f"{path}: a date-time is given to the minute (YYYY-MM-DD HH:MM)")
        naive = datetime(value.year, value.month, value.day, value.hour, value.minute)
    elif isinstance(value, date):
        naive = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and _RE_DATETIME.fullmatch(value):
        text = value.replace("T", " ")
        fmt = "%Y-%m-%d %H:%M" if " " in text else "%Y-%m-%d"
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError as exc:
            raise InvalidValueError(f"{path}: not a valid date-time") from exc
    else:
        raise InvalidValueError(f"{path}: not a date or date-time")
    try:
        return _local_to_us(naive, tz)
    except (OverflowError, ValueError) as exc:
        raise InvalidValueError(f"{path}: date-time outside the representable range") from exc


def _enum(value: object, options: type[Enum], path: str) -> Enum:
    if isinstance(value, str):
        for member in options:
            if member.value == value:
                return member
    allowed = " | ".join(m.value for m in options)
    raise InvalidValueError(f"{path}: expected one of {allowed}")


# ---------------------------------------------------------------------------
# Section resolvers
# ---------------------------------------------------------------------------

_RULE_KEYS = {
    "cooldown",
    "multi_tag",
    "max_targets_per_message",
    "edit_grace_minutes",
    "max_snipes_per_target_per_day",
    "allow_self",
    "allow_bots",
    "count_thread_replies",
    "count_image_links",
    "allow_video",
    "selfie_bonus",
}


def _default_rule(effective_from_us: int) -> ResolvedRule:
    return ResolvedRule(
        effective_from_us=effective_from_us,
        cooldown=CooldownRule(microseconds=15 * US_PER_MINUTE, scope=Scope.PAIR,
                              rejected_attempts_reset=False),
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


def _resolve_cooldown(raw: Mapping, base: CooldownRule, path: str) -> CooldownRule:
    _reject_unknown(raw, {"minutes", "scope", "rejected_attempts_reset"}, path)
    if "minutes" in raw:
        minutes = _get_int(raw, "minutes", 0, path, minimum=0)
        microseconds = minutes * US_PER_MINUTE
    else:
        microseconds = base.microseconds
    scope = base.scope
    if "scope" in raw:
        scope = _enum(raw["scope"], Scope, f"{path}.scope")  # type: ignore[assignment]
    reset = base.rejected_attempts_reset
    if "rejected_attempts_reset" in raw:
        reset = _get_bool(raw, "rejected_attempts_reset", False, path)
    return CooldownRule(microseconds=microseconds, scope=scope, rejected_attempts_reset=reset)


def _patch_rule(
    raw: Mapping, base: ResolvedRule, effective_from_us: int, path: str,
    *, allow_effective_from: bool = False,
) -> ResolvedRule:
    allowed = _RULE_KEYS | {"effective_from"} if allow_effective_from else _RULE_KEYS
    _reject_unknown(raw, allowed, path)

    cooldown = base.cooldown
    if "cooldown" in raw:
        cooldown = _resolve_cooldown(
            _require_mapping(raw["cooldown"], f"{path}.cooldown"), base.cooldown, f"{path}.cooldown")

    multi_tag = base.multi_tag
    if "multi_tag" in raw:
        multi_tag = _enum(raw["multi_tag"], MultiTag, f"{path}.multi_tag")  # type: ignore[assignment]

    max_targets = base.max_targets_per_message
    if "max_targets_per_message" in raw:
        max_targets = _get_int_or_null(raw, "max_targets_per_message", None, path, minimum=1)

    edit_grace_us = base.edit_grace_us
    if "edit_grace_minutes" in raw:
        edit_grace_us = _get_int(raw, "edit_grace_minutes", 0, path, minimum=0) * US_PER_MINUTE

    max_snipes = base.max_snipes_per_target_per_day
    if "max_snipes_per_target_per_day" in raw:
        max_snipes = _get_int_or_null(raw, "max_snipes_per_target_per_day", None, path, minimum=1)

    return ResolvedRule(
        effective_from_us=effective_from_us,
        cooldown=cooldown,
        multi_tag=multi_tag,  # type: ignore[arg-type]
        max_targets_per_message=max_targets,
        edit_grace_us=edit_grace_us,
        max_snipes_per_target_per_day=max_snipes,
        allow_self=_get_bool(raw, "allow_self", base.allow_self, path),
        allow_bots=_get_bool(raw, "allow_bots", base.allow_bots, path),
        count_thread_replies=_get_bool(raw, "count_thread_replies", base.count_thread_replies, path),
        count_image_links=_get_bool(raw, "count_image_links", base.count_image_links, path),
        allow_video=_get_bool(raw, "allow_video", base.allow_video, path),
        selfie_bonus=_get_bool(raw, "selfie_bonus", base.selfie_bonus, path),
    )


def _resolve_rules(raw: object, tz: ZoneInfo, first_semester_start: int) -> DatedRules:
    if isinstance(raw, Mapping):
        rule = _patch_rule(raw, _default_rule(INT_MIN_TS), INT_MIN_TS, "rules")
        return DatedRules(entries=(rule,))

    if not isinstance(raw, list):
        raise InvalidValueError("rules: expected a mapping or a list")
    if not raw:
        raise EmptyValueError("rules: at least one entry required")

    parsed: list[tuple[int, Mapping]] = []
    for index, entry in enumerate(raw):
        path = f"rules[{index}]"
        entry = _require_mapping(entry, path)
        if "effective_from" not in entry:
            raise MissingRequiredKeyError(f"{path}.effective_from: required in the dated-list form")
        eff = _parse_datetime(entry["effective_from"], tz, f"{path}.effective_from")
        parsed.append((eff, entry))

    for i in range(1, len(parsed)):
        if parsed[i][0] <= parsed[i - 1][0]:
            raise RulesEffectiveFromError(
                f"rules[{i}].effective_from: must be strictly after the previous entry")

    if parsed[0][0] > first_semester_start:
        raise RulesEffectiveFromError(
            "rules[0].effective_from: must be at or before the earliest semester start")

    entries: list[ResolvedRule] = []
    # The first entry covers everything before the second (00 section 6; 40 section
    # 1.5 rule 3), so it resolves at INT_MIN_TS like the single-mapping form.
    base = _default_rule(INT_MIN_TS)
    base = _patch_rule(parsed[0][1], base, INT_MIN_TS, "rules[0]", allow_effective_from=True)
    entries.append(base)
    for index in range(1, len(parsed)):
        eff, entry = parsed[index]
        base = _patch_rule(entry, base, eff, f"rules[{index}]", allow_effective_from=True)
        entries.append(base)
    return DatedRules(entries=tuple(entries))


def _resolve_semesters(raw: object, tz: ZoneInfo) -> tuple[Semester, ...]:
    if not isinstance(raw, list):
        raise InvalidValueError("semesters: expected a non-empty list")
    if not raw:
        raise EmptyValueError("semesters: at least one semester required")

    seen_names: set[str] = set()
    semesters: list[Semester] = []
    for index, entry in enumerate(raw):
        path = f"semesters[{index}]"
        entry = _require_mapping(entry, path)
        _reject_unknown(entry, {"name", "start", "end"}, path)
        for key in ("name", "start", "end"):
            if key not in entry:
                raise MissingRequiredKeyError(f"{path}.{key}: required")
        name = _get_str(entry, "name", path)
        if not _RE_NAME.fullmatch(name):
            raise InvalidValueError(f"{path}.name: bad semester name")
        if name in seen_names:
            raise DuplicateNameError(f"{path}.name: duplicate semester name")
        seen_names.add(name)
        start_us = _parse_date(entry["start"], tz, f"{path}.start", end=False)
        end_us = _parse_date(entry["end"], tz, f"{path}.end", end=True)
        if start_us > end_us:
            raise InvalidValueError(f"{path}: start is after end")
        semesters.append(Semester(name=name, start_us=start_us, end_us=end_us))

    semesters.sort(key=lambda s: s.start_us)
    for i in range(1, len(semesters)):
        if semesters[i].start_us <= semesters[i - 1].end_us:
            raise OverlappingSemestersError(
                f"semesters: '{semesters[i - 1].name}' and '{semesters[i].name}' overlap")
    return tuple(semesters)


def _resolve_member(
    member: object, group: str | None, path: str, tz: ZoneInfo,
    seen: dict[str, str], is_bot: Mapping[str, bool] | None,
) -> RosterEntry:
    if isinstance(member, str):
        user = _user(member, path)
        join_us = INT_MIN_TS
    elif isinstance(member, Mapping):
        _reject_unknown(member, {"id", "from"}, path)
        if "id" not in member:
            raise MissingRequiredKeyError(f"{path}.id: required")
        user = _user(member["id"], f"{path}.id")
        if "from" in member:
            join_us = _parse_datetime(member["from"], tz, f"{path}.from")
        else:
            join_us = INT_MIN_TS
    else:
        raise InvalidValueError(f"{path}: expected a user ID or a {{id, from}} mapping")

    if user in seen:
        raise DuplicateGroupMemberError(
            f"{path}: user already listed in {seen[user]}")
    seen[user] = path
    bot = bool(is_bot.get(user, False)) if is_bot is not None else False
    return RosterEntry(user=user, join_us=join_us, group=group, is_bot=bot)


def _resolve_players(raw: object, tz: ZoneInfo, is_bot: Mapping[str, bool] | None) -> Roster:
    raw = _require_mapping(raw, "players")
    _reject_unknown(raw, {"mode", "groups", "extras", "count_intra_group"}, "players")

    mode = RosterMode.LISTED
    if "mode" in raw:
        mode = _enum(raw["mode"], RosterMode, "players.mode")  # type: ignore[assignment]

    entries: dict[str, RosterEntry] = {}
    seen: dict[str, str] = {}

    groups = raw.get("groups", {})
    groups = _require_mapping(groups, "players.groups")
    for group_name, members in groups.items():
        if not isinstance(group_name, str) or not group_name or group_name == _UNGROUPED_LABEL:
            raise InvalidValueError(f"players.groups: bad group name {group_name!r}")
        path = f"players.groups.{group_name}"
        if not isinstance(members, list):
            raise InvalidValueError(f"{path}: expected a list of members")
        if not members:
            raise EmptyGroupError(f"{path}: a group must have at least one member")
        for index, member in enumerate(members):
            entry = _resolve_member(member, group_name, f"{path}[{index}]", tz, seen, is_bot)
            entries[entry.user] = entry

    extras = raw.get("extras", [])
    if not isinstance(extras, list):
        raise InvalidValueError("players.extras: expected a list of members")
    if mode is RosterMode.AUTO and extras:
        # Under auto every non-bot user already plays ungrouped (E-W4-42).
        raise AutoRosterExtrasError(
            "players.extras: must be empty or absent when players.mode is auto")
    for index, member in enumerate(extras):
        entry = _resolve_member(member, None, f"players.extras[{index}]", tz, seen, is_bot)
        entries[entry.user] = entry

    count_intra_group = _get_bool(raw, "count_intra_group", True, "players")
    if mode is RosterMode.AUTO:
        bots = frozenset(u for u, b in (is_bot or {}).items() if b)
        return Roster(entries=entries, count_intra_group=count_intra_group,
                      mode=RosterMode.AUTO, bots=bots)
    return Roster(entries=entries, count_intra_group=count_intra_group)


def _resolve_consent(raw: object) -> ConsentConfig:
    raw = _require_mapping(raw, "consent")
    _reject_unknown(raw, {"veto", "optout_messages", "opted_out"}, "consent")

    if "veto" not in raw:
        raise MissingRequiredKeyError("consent.veto: required")
    veto = _require_mapping(raw["veto"], "consent.veto")
    _reject_unknown(veto, {"emoji", "by"}, "consent.veto")
    if "emoji" not in veto:
        raise MissingRequiredKeyError("consent.veto.emoji: required")
    veto_emoji = _emoji(veto["emoji"], "consent.veto.emoji")

    if "by" in veto:
        by_raw = veto["by"]
        if not isinstance(by_raw, list) or not by_raw:
            raise InvalidValueError("consent.veto.by: expected a non-empty list")
        by: list[VetoActor] = []
        for index, actor in enumerate(by_raw):
            resolved = _enum(actor, VetoActor, f"consent.veto.by[{index}]")
            if resolved not in by:
                by.append(resolved)  # type: ignore[arg-type]
        veto_by = tuple(by)
    else:
        veto_by = (VetoActor.ADMINS,)

    optout_ts: list[str] = []
    for index, ts in enumerate(_optional_list(raw, "optout_messages", "consent.optout_messages")):
        optout_ts.append(_slack_ts(ts, f"consent.optout_messages[{index}]"))

    seeds: set[str] = set()
    for index, uid in enumerate(_optional_list(raw, "opted_out", "consent.opted_out")):
        seeds.add(_user(uid, f"consent.opted_out[{index}]"))

    return ConsentConfig(
        veto_emoji=veto_emoji,
        veto_by=veto_by,
        optout_message_ts=tuple(optout_ts),
        seed_opted_out=tuple(sorted(seeds)),
    )


def _resolve_feedback(raw: object, veto_emoji: str) -> tuple[FeedbackReactions, ReviewFlag]:
    raw = _require_mapping(raw, "feedback")
    _reject_unknown(raw, {"reactions", "review"}, "feedback")

    reactions_raw = raw.get("reactions", {})
    reactions_raw = _require_mapping(reactions_raw, "feedback.reactions")
    _reject_unknown(
        reactions_raw,
        {"counted", "cooldown", "untagged", "not_counted", "selfie"},
        "feedback.reactions",
    )

    def _req(key: str, default: str) -> str:
        if key not in reactions_raw:
            return default
        if reactions_raw[key] is None:
            raise MissingRequiredKeyError(f"feedback.reactions.{key}: must not be null")
        return _emoji(reactions_raw[key], f"feedback.reactions.{key}")

    def _nullable(key: str, default: str | None) -> str | None:
        if key not in reactions_raw:
            return default
        if reactions_raw[key] is None:
            return None
        return _emoji(reactions_raw[key], f"feedback.reactions.{key}")

    reactions = FeedbackReactions(
        counted=_req("counted", "white_check_mark"),
        cooldown=_req("cooldown", "hourglass_flowing_sand"),
        untagged=_nullable("untagged", "label"),
        not_counted=_req("not_counted", "no_entry_sign"),
        selfie=_nullable("selfie", "selfie"),
    )

    review_raw = raw.get("review", {})
    review_raw = _require_mapping(review_raw, "feedback.review")
    _reject_unknown(review_raw, {"min_targets", "emoji"}, "feedback.review")
    review = ReviewFlag(
        min_targets=_get_int_or_null(review_raw, "min_targets", 5, "feedback.review", minimum=1),
        emoji=(None if review_raw.get("emoji", "question") is None
               else _emoji(review_raw.get("emoji", "question"), "feedback.review.emoji")),
    )

    _check_emoji_collisions(veto_emoji, reactions, review)
    return reactions, review


def _check_emoji_collisions(veto: str, reactions: FeedbackReactions, review: ReviewFlag) -> None:
    feedback = {
        "counted": reactions.counted,
        "cooldown": reactions.cooldown,
        "untagged": reactions.untagged,
        "not_counted": reactions.not_counted,
    }
    # The veto emoji must differ from every feedback emoji, selfie and review.
    for name, emoji in feedback.items():
        if emoji is not None and emoji == veto:
            raise EmojiCollisionError(
                f"consent.veto.emoji collides with feedback.reactions.{name}")
    if reactions.selfie is not None and reactions.selfie == veto:
        raise EmojiCollisionError(
            "consent.veto.emoji collides with feedback.reactions.selfie")
    if review.emoji is not None and review.emoji == veto:
        raise EmojiCollisionError("consent.veto.emoji collides with feedback.review.emoji")

    # The selfie emoji must differ from every other feedback and from review.
    if reactions.selfie is not None:
        for name, emoji in feedback.items():
            if emoji is not None and emoji == reactions.selfie:
                raise EmojiCollisionError(
                    f"feedback.reactions.selfie collides with feedback.reactions.{name}")
        if review.emoji is not None and review.emoji == reactions.selfie:
            raise EmojiCollisionError(
                "feedback.reactions.selfie collides with feedback.review.emoji")

    # The review emoji must differ from every feedback emoji.
    if review.emoji is not None:
        for name, emoji in feedback.items():
            if emoji is not None and emoji == review.emoji:
                raise EmojiCollisionError(
                    f"feedback.review.emoji collides with feedback.reactions.{name}")


_SECTION_PERIOD = {Cadence.DAILY: Section.DAY, Cadence.WEEKLY: Section.WEEK,
                   Cadence.FINAL: Section.SEMESTER}
_ALL_PERIOD = {Section.DAY, Section.WEEK, Section.SEMESTER}


def _resolve_reports(raw: object) -> tuple[ReportSpec, ...]:
    if not isinstance(raw, list):
        raise InvalidValueError("reports: expected a list")

    seen_names: set[str] = set()
    reports: list[ReportSpec] = []
    for index, entry in enumerate(raw):
        path = f"reports[{index}]"
        entry = _require_mapping(entry, path)
        _reject_unknown(
            entry,
            {"name", "every", "at", "weekday", "post_to", "sections", "top_n"},
            path,
        )
        for key in ("name", "every", "at", "sections"):
            if key not in entry:
                raise MissingRequiredKeyError(f"{path}.{key}: required")

        name = _get_str(entry, "name", path)
        if not _RE_NAME.fullmatch(name):
            raise InvalidValueError(f"{path}.name: bad report name")
        if name in seen_names:
            raise DuplicateNameError(f"{path}.name: duplicate report name")
        seen_names.add(name)

        every = entry["every"]
        cadence = {
            "1d": Cadence.DAILY,
            "1w": Cadence.WEEKLY,
            "semester_end": Cadence.FINAL,
        }.get(every if isinstance(every, str) else None)
        if cadence is None:
            raise InvalidValueError(f"{path}.every: expected 1d | 1w | semester_end")

        at = entry["at"]
        if not isinstance(at, str) or not _RE_CLOCK.fullmatch(at):
            raise InvalidValueError(f"{path}.at: expected HH:MM")
        at_hour, at_minute = (int(part) for part in at.split(":"))

        weekday: Weekday | None = None
        if cadence == Cadence.WEEKLY:
            if "weekday" not in entry:
                raise InvalidValueError(f"{path}.weekday: required for a weekly report")
            weekday = _enum(entry["weekday"], Weekday, f"{path}.weekday")  # type: ignore[assignment]
        elif "weekday" in entry:
            raise InvalidValueError(f"{path}.weekday: only allowed on a weekly report")

        post_to = None
        if entry.get("post_to") is not None:
            post_to = _channel(entry["post_to"], f"{path}.post_to")

        sections_raw = entry["sections"]
        if not isinstance(sections_raw, list):
            raise InvalidValueError(f"{path}.sections: expected a list")
        if not sections_raw:
            raise EmptyValueError(f"{path}.sections: at least one section required")
        sections: list[Section] = []
        for s_index, value in enumerate(sections_raw):
            section = _enum(value, Section, f"{path}.sections[{s_index}]")
            if section not in sections:
                sections.append(section)  # type: ignore[arg-type]

        required_period = _SECTION_PERIOD[cadence]
        present_periods = set(sections) & _ALL_PERIOD
        if required_period not in sections:
            raise InvalidValueError(
                f"{path}.sections: missing the '{required_period.value}' section for this cadence")
        if present_periods - {required_period}:
            raise InvalidValueError(
                f"{path}.sections: a period section does not match the '{every}' cadence")

        top_n = _get_int(entry, "top_n", 5, path, minimum=1)
        if top_n > 20:
            # 30 §5.5's character budget is sized for at most 20 ranked rows.
            raise InvalidValueError(f"{path}.top_n: must be between 1 and 20")
        reports.append(ReportSpec(
            name=name, cadence=cadence, at_hour=at_hour, at_minute=at_minute,
            weekday=weekday, post_to=post_to, sections=tuple(sections), top_n=top_n,
        ))
    return tuple(reports)


def _resolve_faces(raw: object) -> FacesConfig:
    raw = _require_mapping(raw, "faces")
    _reject_unknown(
        raw,
        {"model_path", "fetch_timeout_seconds", "max_image_bytes", "max_attempts",
         "score_threshold"},
        "faces",
    )

    model_path = "snipebot/models/face_detection_yunet_2023mar.onnx"
    if "model_path" in raw:
        model_path = _get_str(raw, "model_path", "faces")
        if not model_path:
            raise EmptyValueError("faces.model_path: must not be empty")

    threshold = "0.9"
    if "score_threshold" in raw:
        threshold = raw["score_threshold"]
        if not isinstance(threshold, str) or not _RE_THRESHOLD.fullmatch(threshold):
            raise InvalidValueError(
                "faces.score_threshold: expected a decimal string in [0.5, 1.0]"
            )

    return FacesConfig(
        model_path=model_path,
        fetch_timeout_seconds=_get_int(raw, "fetch_timeout_seconds", 10, "faces", minimum=1),
        max_image_bytes=_get_int(raw, "max_image_bytes", 25_000_000, "faces", minimum=1),
        max_attempts=_get_int(raw, "max_attempts", 3, "faces", minimum=1),
        score_threshold=threshold,
    )


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

_TOP_LEVEL = {
    "enabled", "recaps", "reactions", "persistence", "slack", "timezone", "sync", "semesters",
    "rules", "players", "consent", "admins", "feedback", "reports", "faces",
}


def _resolve_recaps(root: Mapping, node: yaml.Node | None) -> bool:
    """`recaps` as a strict boolean (E-W4-39): only the scalars true/false, in the YAML
    1.2 spellings. SafeLoader also reads yes/no/on/off as booleans, so the written
    scalar is checked on the node, not just the constructed value. Absent: True."""
    return _resolve_switch(root, node, "recaps", RecapsValueError)


def _resolve_switch(root: Mapping, node: yaml.Node | None, key: str,
                    error: type[InvalidValueError]) -> bool:
    """A top-level output switch (`recaps`, `reactions`) as a strict boolean. Absent: True."""
    if key not in root:
        return True
    value = root[key]
    raw = None
    if isinstance(node, yaml.MappingNode):
        raw = next((v for k, v in node.value
                    if isinstance(k, yaml.ScalarNode) and k.value == key), None)
    written = raw.value if isinstance(raw, yaml.ScalarNode) else None
    if not isinstance(value, bool) or (
            raw is not None and (written or "").lower() not in {"true", "false"}):
        raise error(f"{key}: expected true or false")
    return value


def load_config(
    path: str | os.PathLike[str] = "config.yaml",
    *,
    is_bot: Mapping[str, bool] | None = None,
) -> Config:
    """Parse and validate config.yaml into frozen, resolved dataclasses.

    On success the returned Config is fully resolved: local wall-clock values
    are already converted to UTC integer microseconds in `timezone`, dated rules
    are patched and sorted, and roster join instants are computed. Any invalid
    input raises a subclass of ConfigError; the message names the key path.
    """
    with open(path, "rb") as handle:
        # yaml.load's steps, keeping the composed node for the strict `recaps` check.
        loader = _Loader(handle)
        try:
            node = loader.get_single_node()
            document = loader.construct_document(node) if node is not None else None
        except yaml.YAMLError as exc:
            raise InvalidValueError("config: malformed YAML") from exc
        finally:
            loader.dispose()
    root = _require_mapping(document, "config")
    _reject_unknown(root, _TOP_LEVEL, "")

    for key in ("slack", "timezone", "semesters", "rules", "players", "consent", "feedback"):
        if key not in root:
            raise MissingRequiredKeyError(f"{key}: required")

    tz_name = root["timezone"]
    if not isinstance(tz_name, str):
        raise InvalidValueError("timezone: expected an IANA zone name")
    if tz_name not in _available_zones():
        raise InvalidValueError(f"timezone: unknown zone {tz_name!r}")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise InvalidValueError(f"timezone: unknown zone {tz_name!r}") from exc

    enabled = _get_bool(root, "enabled", True, "")
    recaps = _resolve_recaps(root, node)
    reactions = _resolve_switch(root, node, "reactions", ReactionsValueError)

    persistence = Persistence.GIT
    if "persistence" in root:
        persistence = _enum(root["persistence"], Persistence, "persistence")  # type: ignore[assignment]

    slack = _require_mapping(root["slack"], "slack")
    _reject_unknown(slack, {"channel"}, "slack")
    if "channel" not in slack:
        raise MissingRequiredKeyError("slack.channel: required")
    channel = _channel(slack["channel"], "slack.channel")

    sync = _resolve_sync(root["sync"]) if "sync" in root else _resolve_sync({})

    semesters = _resolve_semesters(root["semesters"], tz)
    first_semester_start = semesters[0].start_us

    rules = _resolve_rules(root["rules"], tz, first_semester_start)

    roster = _resolve_players(root["players"], tz, is_bot)

    admins_raw = root.get("admins", [])
    if not isinstance(admins_raw, list):
        raise InvalidValueError("admins: expected a list of user IDs")
    admins = tuple(sorted({_user(uid, f"admins[{i}]") for i, uid in enumerate(admins_raw)}))

    consent = _resolve_consent(root["consent"])
    feedback, review = _resolve_feedback(root["feedback"], consent.veto_emoji)
    reports = _resolve_reports(root.get("reports", []))
    faces = _resolve_faces(root["faces"]) if "faces" in root else _resolve_faces({})

    return Config(
        enabled=enabled,
        persistence=persistence,  # type: ignore[arg-type]
        channel=channel,
        tz=tz,
        sync=sync,
        semesters=semesters,
        rules=rules,
        roster=roster,
        consent=consent,
        admins=admins,
        feedback=feedback,
        review=review,
        reports=reports,
        faces=faces,
        recaps=recaps,
        reactions=reactions,
    )


def _resolve_sync(raw: object) -> SyncSettings:
    raw = _require_mapping(raw, "sync")
    _reject_unknown(
        raw,
        {"interval_minutes", "scan_days", "history_horizon_days",
         "max_deletes_per_run", "large_movement_rows"},
        "sync",
    )
    interval = _get_int(raw, "interval_minutes", 15, "sync", minimum=1)
    scan_days = _get_int(raw, "scan_days", 14, "sync", minimum=1)
    horizon = _get_int_or_null(raw, "history_horizon_days", 80, "sync", minimum=1)
    if horizon is not None and horizon < scan_days:
        if "history_horizon_days" not in raw:
            raise InvalidValueError(
                "sync.history_horizon_days: must be set explicitly (null or >= scan_days) "
                "when scan_days exceeds the default 80")
        raise InvalidValueError("sync.history_horizon_days: must be null or >= scan_days")
    return SyncSettings(
        interval_minutes=interval,
        scan_days=scan_days,
        history_horizon_days=horizon,
        max_deletes_per_run=_get_int(raw, "max_deletes_per_run", 5, "sync", minimum=0),
        large_movement_rows=_get_int(raw, "large_movement_rows", 10, "sync", minimum=0),
    )


# ---------------------------------------------------------------------------
# Fingerprint guard (40-config-cli.md section 3; canonical shapes 00 section 7)
# ---------------------------------------------------------------------------

_FINGERPRINT_SUBTREES = ("rules", "players", "semesters", "groups")


def _canon(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def _sha(obj: object) -> str:
    return hashlib.sha256(_canon(obj).encode("ascii")).hexdigest()


def _rule_fingerprint_obj(rule: ResolvedRule) -> dict:
    return {
        "effective_from_us": rule.effective_from_us,
        "cooldown": {
            "microseconds": rule.cooldown.microseconds,
            "scope": rule.cooldown.scope.value,
            "rejected_attempts_reset": rule.cooldown.rejected_attempts_reset,
        },
        "multi_tag": rule.multi_tag.value,
        "max_targets_per_message": rule.max_targets_per_message,
        "edit_grace_us": rule.edit_grace_us,
        "max_snipes_per_target_per_day": rule.max_snipes_per_target_per_day,
        "allow_self": rule.allow_self,
        "allow_bots": rule.allow_bots,
        "count_thread_replies": rule.count_thread_replies,
        "count_image_links": rule.count_image_links,
        "allow_video": rule.allow_video,
        "selfie_bonus": rule.selfie_bonus,
    }


def compute_fingerprints(config: Config, h_us: int | None) -> dict[str, str]:
    """The four section-7 fingerprints over the subtree affecting rows with ts
    <= H. `h_us` is None on an empty ledger, where rules/players/semesters are
    over the empty set; the groups fingerprint is over all groups regardless."""
    def within(value: int) -> bool:
        return h_us is not None and value <= h_us

    rules_objs = [
        _rule_fingerprint_obj(rule)
        for rule in config.rules.entries
        if within(rule.effective_from_us)
    ]
    rules_objs.sort(key=lambda o: o["effective_from_us"])

    players_objs: object
    if config.roster.mode is RosterMode.AUTO:
        # 00 section 7 (E-W4-42): the mode plus the grouped entries only, never the
        # is_bot map, so people joining or leaving the channel never trip the guard.
        players_objs = {
            "mode": RosterMode.AUTO.value,
            "grouped": sorted(
                (
                    {"user": e.user, "join_us": e.join_us, "group": e.group}
                    for e in config.roster.entries.values()
                    if within(e.join_us)
                ),
                key=lambda o: o["user"],
            ),
        }
    else:
        players_objs = sorted(
            (
                {"user": e.user, "join_us": e.join_us, "group": e.group, "is_bot": e.is_bot}
                for e in config.roster.entries.values()
                if within(e.join_us)
            ),
            key=lambda o: o["user"],
        )

    semesters_objs = sorted(
        (
            {"name": s.name, "start_us": s.start_us, "end_us": s.end_us}
            for s in config.semesters
            if within(s.start_us)
        ),
        key=lambda o: o["start_us"],
    )

    groups: dict[str, list[str]] = {}
    for entry in config.roster.entries.values():
        if entry.group is not None:
            groups.setdefault(entry.group, []).append(entry.user)
    groups_obj = {name: sorted(groups[name]) for name in sorted(groups)}

    return {
        "rules": _sha(rules_objs),
        "players": _sha(players_objs),
        "semesters": _sha(semesters_objs),
        "groups": _sha(groups_obj),
    }


def fingerprint_guard(
    config: Config,
    row_ts_us: Sequence[int],
    stored: Mapping[str, str],
    h_us: int | None = None,
) -> None:
    """Recompute the four fingerprints and refuse on a rules/players/semesters
    mismatch (FingerprintGuardError, exit 3). A groups-only difference never
    refuses (doctor warns). Absent or empty stored fingerprints (first run) and
    an empty ledger never trip the guard.

    `h_us` is the H the stored fingerprints were computed over (state.json
    `fingerprints_at`); None recomputes at the ledger's newest row. Recomputing at
    the stored H lets a files-mode crash between the three renames converge
    (E-W4-17)."""
    if not stored or not any(stored.get(k) for k in _FINGERPRINT_SUBTREES):
        return
    if not row_ts_us:
        return
    if h_us is None:
        h_us = max(row_ts_us)
    current = compute_fingerprints(config, h_us)

    refused: list[tuple[str, str, str]] = []
    for key in ("rules", "players", "semesters"):
        old = stored.get(key)
        if old and old != current[key]:
            refused.append((key, old, current[key]))
    if refused:
        count = sum(1 for ts in row_ts_us if ts <= h_us)
        raise FingerprintGuardError(_guard_message(refused, count))


def _guard_message(refused: list[tuple[str, str, str]], count: int) -> str:
    blocks: list[str] = []
    for key, old, new in refused:
        short = f"{key} {old[:8]}→{new[:8]}"
        if key == "rules":
            blocks.append(
                f"config change to `rules` would re-judge {count} existing message(s) [{short}].\n"
                "  Apply going forward only:   snipebot rules bump --effective-from now\n"
                "  Rewrite history on purpose:  snipebot sync --reevaluate"
            )
        elif key == "players":
            blocks.append(
                f"config change to `players` would re-judge {count} existing message(s) [{short}].\n"
                "  Add a player from now:       give them `from: <date>` in config.yaml\n"
                "  Remove a player:             use the opt-out (react to the pinned message, or `consent.opted_out:`)\n"
                "  Rewrite history on purpose:  snipebot sync --reevaluate"
            )
        else:
            blocks.append(
                f"config change to `semesters` would re-judge existing rows [{short}].\n"
                "  Rewrite history on purpose:  snipebot sync --reevaluate"
            )
    return "\n".join(blocks)
