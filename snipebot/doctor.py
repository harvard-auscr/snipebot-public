"""Read-only preflight checks (40-config-cli.md section 5). Two blocks: offline
(config and files, no network) and against Slack (needs a working `SlackIO`).
Every check prints one line, `DOC-XXX PASS|WARN|FAIL <detail>`, IDs only -- never
a name, permalink, URL, file name or token in the detail. `doctor` never posts,
reacts, or writes the ledger/state; it may only read.

Exit `0` when no FAIL fired, else `10` (`Exit.DOCTOR_FAILED`, 40-config-cli.md
section 4.4) -- this module does not import `snipebot.cli` to avoid a cycle, so
the two codes are repeated here as plain ints.
"""

from __future__ import annotations

import json as json_lib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from snipebot.config import (
    INT_MIN_TS,
    Config,
    ConfigError,
    NoRuleInForceError,
    Persistence,
    RosterMode,
    _parse_datetime,
    compute_fingerprints,
    load_config,
)
from snipebot.ledger import (
    LedgerIntegrityError,
    MalformedLedgerError,
    check_integrity,
    dumps_verdicts,
    load_ledger,
    load_state,
)
from snipebot.cli import Exit
from snipebot.rules import evaluate
from snipebot.slack_io import (
    ChannelNotFound,
    MessageNotFound,
    MissingScope,
    NotInChannel,
    SlackError,
)
from snipebot.ts import US_PER_SECOND


def __getattr__(name: str) -> Exit:
    # `doctor.OK` / `doctor.DOCTOR_FAILED` resolve to the shared `Exit` members
    # by reference (00 section 10: never redefined here, 40 section 4.4).
    try:
        return Exit[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


_DETAIL_CLIP = 200  # a ConfigError message names a key path only; still bounded

# The base bot scopes every deployment needs, in force regardless of selfie_bonus
# (40 section 7.3). `files:read` is the one scope the manifest carries that is
# conditional on selfie_bonus (40 section 5.2 DOC-SCOPES) and is handled apart
# from this constant.
_CONDITIONAL_SCOPE = "files:read"

# A private (`is_private`) channel -- watched or a report post_to -- can only be
# read/posted with the `groups:*` counterparts of the public `channels:*` scopes
# (40 section 5.2 DOC-SCOPES); neither is in the manifest's base grant, so a
# private channel demands them on top of `required`.
_PRIVATE_CHANNEL_SCOPES = frozenset({"groups:history", "groups:read"})

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MANIFEST_PATH = _REPO_ROOT / "slack-app-manifest.yaml"
_FACES_MODEL_SIDECAR = (
    Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx.sha256"
)


@dataclass(frozen=True)
class CheckResult:
    id: str
    severity: str  # "FAIL" | "WARN"
    ok: bool
    detail: str = ""


def _clip(msg: str) -> str:
    msg = str(msg).replace("\n", " ")
    return msg if len(msg) <= _DETAIL_CLIP else msg[: _DETAIL_CLIP - 3] + "..."


def _line(result: CheckResult) -> str:
    if result.ok:
        return f"{result.id} PASS"
    word = "FAIL" if result.severity == "FAIL" else "WARN"
    detail = f" {result.detail}" if result.detail else ""
    return f"{result.id} {word}{detail}"


def _now_us(now_us: int | None) -> int:
    return now_us if now_us is not None else int(time.time() * US_PER_SECOND)


# --------------------------------------------------------------------------- #
# Offline checks (40 section 5.1)
# --------------------------------------------------------------------------- #

def _read_manifest_scopes(path: Path) -> frozenset[str]:
    """The bot scopes declared in `slack-app-manifest.yaml` (40 section 7.3):
    the ground truth for DOC-SCOPES' required set. Never patched by tests --
    the control that exercises a missing scope (CTL-DOCTOR-SCOPE) flips the
    *granted* side of that test's FakeSlack double instead, in
    `tests/controls/slack_fixtures.py`."""
    with open(path, "rb") as handle:
        document = yaml.safe_load(handle)
    scopes = document["oauth_config"]["scopes"]["bot"]
    return frozenset(str(s) for s in scopes)


def _check_config_parse(
    config_path: Path, is_bot: Mapping[str, bool] | None = None,
) -> tuple[Config | None, CheckResult]:
    try:
        config = load_config(config_path, is_bot=is_bot)
    except ConfigError as exc:
        return None, CheckResult("DOC-CONFIG-PARSE", "FAIL", False, _clip(str(exc)))
    except OSError:
        return None, CheckResult("DOC-CONFIG-PARSE", "FAIL", False, "config unreadable")
    return config, CheckResult("DOC-CONFIG-PARSE", "FAIL", True)


def _first_dated_effective_us(config: Config, config_path: Path | None) -> int | None:
    # The dated-list form resolves rules[0] at INT_MIN_TS (it covers everything
    # before rules[1]), so the configured first effective_from is re-read from
    # the file for the report detail. None for the single-mapping form.
    if config_path is None:
        return None
    try:
        with open(config_path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        raw = document.get("rules") if isinstance(document, dict) else None
        if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
            return None
        return _parse_datetime(raw[0].get("effective_from"), config.tz, "rules[0].effective_from")
    except (OSError, yaml.YAMLError, ValueError, ConfigError):
        return None


def _check_rules_resolve(config: Config, config_path: Path | None = None) -> CheckResult:
    # load_config already refused a config whose first dated rule falls after
    # the first semester start (RulesEffectiveFromError); reaching here means
    # every period already resolves.
    entries = config.rules.entries
    first_us = entries[0].effective_from_us
    if first_us == INT_MIN_TS:
        first_us = _first_dated_effective_us(config, config_path) or INT_MIN_TS
    if first_us == INT_MIN_TS:
        # The single-mapping (undated) rule form is effective "at any time"
        # (INT_MIN_TS sentinel); report it as effective from the first semester
        # start so the detail is a real date rather than the sentinel.
        first_us = config.semesters[0].start_us
    first_dt = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=first_us)
    first_effective = f"{first_dt.astimezone(config.tz):%Y-%m-%d}"
    return CheckResult(
        "DOC-RULES-RESOLVE", "FAIL", True,
        f"{len(entries)} dated rule(s), first effective {first_effective}",
    )


def _check_semesters(config: Config) -> CheckResult:
    # load_config already refused < 1 semester, overlaps, or start > end.
    return CheckResult("DOC-SEMESTERS", "FAIL", True)


def _check_emoji_format(config: Config) -> CheckResult:
    # load_config already refused a bad-format or colliding emoji.
    return CheckResult("DOC-EMOJI-FORMAT", "FAIL", True)


def _check_state_parse(state_path: Path) -> tuple[Any | None, CheckResult]:
    try:
        state = load_state(state_path)
    except MalformedLedgerError as exc:
        # `load_state` (and the shared UTF-8/read path it uses) prefixes every
        # message with the fixed "state.json: "; strip it so no file name reaches
        # stdout (module contract: IDs only, never a file name in the detail).
        detail = _clip(str(exc).removeprefix("state.json: "))
        return None, CheckResult("DOC-STATE-PARSE", "FAIL", False, detail)
    return state, CheckResult("DOC-STATE-PARSE", "FAIL", True)


def _check_ledger_and_verdicts(
    config: Config, ledger_path: Path, state, verdicts_path: Path,
) -> tuple[list[Any] | None, list[CheckResult]]:
    """DOC-LEDGER-INTEGRITY and DOC-VERDICTS-FRESH share one `evaluate` run,
    exactly as `sync` does at end-of-run (20-sync-ledger.md section 7.3): the
    verdicts `check_integrity` inspects for a valid `blocked_by` chain are the
    same ones compared against the on-disk `verdicts.jsonl`."""
    try:
        rows = load_ledger(ledger_path)
    except MalformedLedgerError as exc:
        # Line-numbered parse errors already carry no file name ("line N: ..."),
        # but the shared UTF-8/read path prefixes "ledger.jsonl: "; strip it so no
        # file name reaches stdout (module contract: IDs only, no file name).
        detail = _clip(str(exc).removeprefix("ledger.jsonl: "))
        return None, [
            CheckResult("DOC-LEDGER-INTEGRITY", "FAIL", False, detail),
            CheckResult("DOC-VERDICTS-FRESH", "FAIL", False, "ledger invalid"),
        ]

    opted_out = set(state.opted_out)
    try:
        verdicts = evaluate(
            rows, config.rules, config.roster, opted_out, config.semesters, config.tz,
        )
    except NoRuleInForceError as exc:
        detail = _clip(str(exc))
        return rows, [
            CheckResult("DOC-LEDGER-INTEGRITY", "FAIL", False, detail),
            CheckResult("DOC-VERDICTS-FRESH", "FAIL", False, detail),
        ]

    try:
        check_integrity(rows, verdicts)
        integrity = CheckResult("DOC-LEDGER-INTEGRITY", "FAIL", True)
    except LedgerIntegrityError as exc:
        integrity = CheckResult("DOC-LEDGER-INTEGRITY", "FAIL", False, _clip(str(exc)))

    fresh = dumps_verdicts(verdicts)
    on_disk = verdicts_path.read_bytes() if verdicts_path.exists() else b""
    ok = on_disk == fresh.encode("utf-8")
    freshness = CheckResult(
        "DOC-VERDICTS-FRESH", "FAIL", ok, "" if ok else "verdicts.jsonl is stale",
    )
    return rows, [integrity, freshness]


def _check_fingerprints(config: Config, rows: list[Any], state) -> list[CheckResult]:
    row_ts_us = [_row_ts_us(r) for r in rows]
    h_us = max(row_ts_us) if row_ts_us else None
    current = compute_fingerprints(config, h_us)
    out = []
    for key, check_id in (
        ("rules", "DOC-FINGERPRINT-RULES"),
        ("players", "DOC-FINGERPRINT-PLAYERS"),
        ("semesters", "DOC-FINGERPRINT-SEMESTERS"),
        ("groups", "DOC-FINGERPRINT-GROUPS"),
    ):
        stored = state.fingerprints.get(key)
        ok = not stored or stored == current[key]
        if ok:
            detail = ""
        elif key == "groups":
            # 40 section 3: a groups-only difference never stops a sync.
            detail = "groups changed mid-semester (sync continues; re-post standings)"
        else:
            detail = "fingerprint mismatch (a sync would refuse)"
        out.append(CheckResult(check_id, "WARN", ok, detail))
    return out


def _row_ts_us(row: Any) -> int:
    from snipebot.ts import parse_ts

    return parse_ts(row.ts)


def _check_persistence_files(config: Config) -> CheckResult:
    ok = config.persistence is Persistence.GIT
    detail = "" if ok else "persistence: files (no history, no off-host copy)"
    return CheckResult("DOC-PERSISTENCE-FILES", "WARN", ok, detail)


def _check_faces_model(config: Config, rule_in_force_selfie_bonus: bool) -> CheckResult:
    if not rule_in_force_selfie_bonus:
        return CheckResult("DOC-FACES-MODEL", "FAIL", True)
    model_path = Path(config.faces.model_path)
    # 40 section 5.1: the hash is compared with the sidecar committed in the
    # package, never with a `<model_path>.sha256` travelling beside the model.
    sidecar_path = _FACES_MODEL_SIDECAR
    if not model_path.is_file():
        return CheckResult("DOC-FACES-MODEL", "FAIL", False, "model file missing")
    if not sidecar_path.is_file():
        return CheckResult("DOC-FACES-MODEL", "FAIL", False, "sidecar sha256 missing")
    import hashlib

    try:
        actual = hashlib.sha256(model_path.read_bytes()).hexdigest()
        tokens = sidecar_path.read_text(encoding="utf-8").strip().split()
    except (OSError, UnicodeDecodeError):
        return CheckResult("DOC-FACES-MODEL", "FAIL", False, "model or sidecar unreadable")
    if not tokens:
        return CheckResult("DOC-FACES-MODEL", "FAIL", False, "sidecar sha256 empty/unparseable")
    expected = tokens[0].lower()
    ok = actual.lower() == expected
    detail = "" if ok else "model sha256 does not match the sidecar"
    return CheckResult("DOC-FACES-MODEL", "FAIL", ok, detail)


def _check_faces_import(rule_in_force_selfie_bonus: bool) -> CheckResult:
    # The selfie bonus needs both cv2 (the face detector) and pillow_heif (HEIC
    # decode). Either import failing is FAIL while selfie_bonus is in force at
    # now, WARN otherwise -- the dependency is only load-bearing when the bonus
    # runs, but a missing decoder is still worth flagging ahead of time.
    severity = "FAIL" if rule_in_force_selfie_bonus else "WARN"
    # Fixed details only: an ImportError's text can carry a module file path
    # or a shared-library name (40 section 5.2: IDs and counts, never paths).
    try:
        import cv2  # noqa: F401

        if not hasattr(cv2, "FaceDetectorYN"):
            raise ImportError
    except ImportError:
        return CheckResult("DOC-FACES-IMPORT", severity, False, "cv2 unavailable")
    try:
        import pillow_heif  # noqa: F401
    except ImportError:
        return CheckResult("DOC-FACES-IMPORT", severity, False, "pillow_heif unavailable")
    return CheckResult("DOC-FACES-IMPORT", severity, True)


# --------------------------------------------------------------------------- #
# Checks against Slack (40 section 5.2)
# --------------------------------------------------------------------------- #

def _check_auth(slack: Any) -> tuple[Any | None, CheckResult]:
    try:
        identity = slack.auth_identity()
    except SlackError as exc:
        return None, CheckResult("DOC-AUTH", "FAIL", False, _clip(str(exc)))
    if not getattr(identity, "bot_id", ""):
        return identity, CheckResult("DOC-AUTH", "FAIL", False, "token is not a bot token")
    return identity, CheckResult("DOC-AUTH", "FAIL", True)


def _check_scopes(
    slack: Any, config: Config, now_us: int, manifest_path: Path,
) -> CheckResult:
    try:
        manifest_scopes = _read_manifest_scopes(manifest_path)
    except OSError:
        return CheckResult("DOC-SCOPES", "FAIL", False, "manifest unreadable")
    except (yaml.YAMLError, KeyError, TypeError):
        return CheckResult("DOC-SCOPES", "FAIL", False, "manifest malformed")

    try:
        selfie_bonus = config.rules.in_force_at(now_us).selfie_bonus
    except NoRuleInForceError:
        selfie_bonus = False

    required = {s for s in manifest_scopes if s != _CONDITIONAL_SCOPE}
    if selfie_bonus:
        required.add(_CONDITIONAL_SCOPE)

    auth_scopes = getattr(slack, "auth_scopes", None)
    if auth_scopes is None:
        return CheckResult("DOC-SCOPES", "FAIL", False, "granted scopes unavailable")
    try:
        granted = auth_scopes()
    except SlackError as exc:
        return CheckResult("DOC-SCOPES", "FAIL", False, _clip(str(exc)))

    granted_set = set(granted)
    missing = sorted(required - granted_set)
    if missing:
        return CheckResult("DOC-SCOPES", "FAIL", False, f"missing scopes: {','.join(missing)}")

    private_channel = _first_private_channel_missing_scopes(slack, config, granted_set)
    if private_channel is not None:
        return CheckResult(
            "DOC-SCOPES", "FAIL", False,
            f"private channel {private_channel} needs groups:history and groups:read",
        )
    return CheckResult("DOC-SCOPES", "FAIL", True)


def _first_private_channel_missing_scopes(
    slack: Any, config: Config, granted: set[str],
) -> str | None:
    """DOC-SCOPES also reads `channel_info.is_private` for the watched channel
    and every report `post_to`: a private channel is only reachable with
    `groups:history` and `groups:read` granted. Returns the first such channel
    ID whose privacy demands a scope not granted, else None. A channel that
    cannot be read here (not found / not a member) is left to the membership
    checks rather than double-penalised by DOC-SCOPES."""
    channels = [config.channel]
    channels.extend(sorted({r.resolved_channel(config.channel) for r in config.reports}))
    for channel in dict.fromkeys(channels):
        try:
            info = slack.channel_info(channel)
        except MissingScope:
            # channels:read is already confirmed granted, so missing_scope on
            # conversations.info means a private channel short of groups:read.
            if not _PRIVATE_CHANNEL_SCOPES <= granted:
                return channel
            continue
        except SlackError:
            continue
        if info.get("is_private", False) and not _PRIVATE_CHANNEL_SCOPES <= granted:
            return channel
    return None


def _check_channel_member(slack: Any, check_id: str, channel: str) -> CheckResult:
    try:
        info = slack.channel_info(channel)
    except (NotInChannel, ChannelNotFound) as exc:
        return CheckResult(check_id, "FAIL", False, _clip(str(exc)))
    except SlackError as exc:
        return CheckResult(check_id, "FAIL", False, _clip(str(exc)))
    if not info.get("is_member", False):
        return CheckResult(check_id, "FAIL", False, "bot is not a member")
    return CheckResult(check_id, "FAIL", True)


def _check_postto_member(slack: Any, config: Config) -> CheckResult:
    channels = sorted({r.resolved_channel(config.channel) for r in config.reports})
    for channel in channels:
        result = _check_channel_member(slack, "DOC-POSTTO-MEMBER", channel)
        if not result.ok:
            return result
    return CheckResult("DOC-POSTTO-MEMBER", "FAIL", True)


def _check_roster_resolve(slack: Any, config: Config) -> CheckResult:
    try:
        users = {u["id"]: u for u in slack.users_list()}
        members = set(slack.conversations_members(config.channel))
    except SlackError as exc:
        return CheckResult("DOC-ROSTER-RESOLVE", "FAIL", False, _clip(str(exc)))
    missing = []
    for uid in sorted(config.roster.entries):
        if uid not in users or bool(users[uid].get("deleted", False)):
            missing.append(uid)
        elif uid not in members:
            missing.append(uid)
    ok = not missing
    detail = "" if ok else f"{len(missing)} roster ID(s) not resolved"
    return CheckResult("DOC-ROSTER-RESOLVE", "FAIL", ok, detail)


def _check_admin_resolve(slack: Any, config: Config) -> CheckResult:
    try:
        users = {u["id"]: u for u in slack.users_list()}
    except SlackError as exc:
        return CheckResult("DOC-ADMIN-RESOLVE", "WARN", False, _clip(str(exc)))
    missing = [
        uid for uid in config.admins
        if uid not in users or bool(users[uid].get("deleted", False))
    ]
    ok = not missing
    detail = "" if ok else f"{len(missing)} admin ID(s) not resolved"
    return CheckResult("DOC-ADMIN-RESOLVE", "WARN", ok, detail)


def _check_roster_bot(slack: Any, config: Config, now_us: int) -> CheckResult:
    try:
        allow_bots = config.rules.in_force_at(now_us).allow_bots
    except NoRuleInForceError:
        allow_bots = False
    # Under players.mode auto a grouped bot is off-roster whatever allow_bots says, so
    # the grouped members are always checked (E-W4-42).
    if allow_bots and config.roster.mode is not RosterMode.AUTO:
        return CheckResult("DOC-ROSTER-BOT", "WARN", True)
    try:
        users = {u["id"]: u for u in slack.users_list()}
    except SlackError as exc:
        return CheckResult("DOC-ROSTER-BOT", "WARN", False, _clip(str(exc)))
    bots = [
        uid for uid in config.roster.entries
        if bool(users.get(uid, {}).get("is_bot", False))
    ]
    ok = not bots
    detail = "" if ok else f"{len(bots)} rostered user(s) are bots"
    return CheckResult("DOC-ROSTER-BOT", "WARN", ok, detail)


def _check_optout_msg(slack: Any, config: Config) -> CheckResult:
    unreadable = 0
    for ts in config.consent.optout_message_ts:
        try:
            slack.reactions_get(config.channel, ts)
        except MessageNotFound:
            unreadable += 1
        except SlackError:
            unreadable += 1
    ok = unreadable == 0
    detail = "" if ok else f"{unreadable} opt-out message(s) unreadable"
    return CheckResult("DOC-OPTOUT-MSG", "WARN", ok, detail)


# --------------------------------------------------------------------------- #
# run()
# --------------------------------------------------------------------------- #

def run(
    args: Any,
    slack_factory: Callable[[], Any] | None = None,
    *,
    now_us: int | None = None,
    manifest_path: str | Path | None = None,
) -> int:
    """Entry point cli.py's `_cmd_doctor` calls. `args` carries `.config`,
    `.data_dir`, `.offline` and `.json` (the `doctor` subparser, cli.py). `now_us`
    and `manifest_path` are test seams; cli.py never passes them."""
    now = _now_us(now_us)
    mpath = Path(manifest_path) if manifest_path is not None else _DEFAULT_MANIFEST_PATH
    data_dir = Path(args.data_dir)
    ledger_path = data_dir / "ledger.jsonl"
    state_path = data_dir / "state.json"
    verdicts_path = data_dir / "verdicts.jsonl"

    results: list[CheckResult] = []

    online = not getattr(args, "offline", False)
    slack = slack_factory() if online and slack_factory is not None else None
    # 40 section 2.1: doctor and sync pass a real is_bot map from users.list, so
    # RosterEntry.is_bot matches what sync scored and fingerprinted. A Slack failure
    # here falls back to no map; DOC-AUTH / DOC-ROSTER-RESOLVE report it.
    is_bot: dict[str, bool] | None = None
    if slack is not None:
        try:
            is_bot = {u["id"]: bool(u.get("is_bot")) for u in slack.users_list() if u.get("id")}
        except SlackError:
            is_bot = None

    config, r = _check_config_parse(Path(args.config), is_bot)
    cache = getattr(args, "is_bot_cache", None)
    if config is not None and is_bot is None and cache:
        # 40 section 2.1 (E-W4-18, E-W4-42): with no users.list, judge bots (rostered
        # ones under listed, every user under auto) from the users.json is_bot map, as
        # report/export do, so the fingerprint and verdicts checks agree with sync.
        config, r = _check_config_parse(Path(args.config), cache)
    results.append(r)

    if config is None:
        for cid in ("DOC-RULES-RESOLVE", "DOC-SEMESTERS", "DOC-EMOJI-FORMAT"):
            results.append(CheckResult(cid, "FAIL", False, "config invalid"))
        results.append(CheckResult("DOC-STATE-PARSE", "FAIL", False, "config invalid"))
        results.append(CheckResult("DOC-LEDGER-INTEGRITY", "FAIL", False, "config invalid"))
        results.append(CheckResult("DOC-VERDICTS-FRESH", "FAIL", False, "config invalid"))
        for cid in (
            "DOC-FINGERPRINT-RULES", "DOC-FINGERPRINT-PLAYERS",
            "DOC-FINGERPRINT-SEMESTERS", "DOC-FINGERPRINT-GROUPS",
        ):
            results.append(CheckResult(cid, "WARN", False, "config invalid"))
        results.append(CheckResult("DOC-PERSISTENCE-FILES", "WARN", False, "config invalid"))
        results.append(CheckResult("DOC-FACES-MODEL", "FAIL", False, "config invalid"))
        results.append(CheckResult("DOC-FACES-IMPORT", "FAIL", False, "config invalid"))
        _emit(results, args)
        return Exit.DOCTOR_FAILED

    results.append(_check_rules_resolve(config, Path(args.config)))
    results.append(_check_semesters(config))
    results.append(_check_emoji_format(config))

    state, r = _check_state_parse(state_path)
    results.append(r)

    if state is None:
        results.append(CheckResult("DOC-LEDGER-INTEGRITY", "FAIL", False, "state invalid"))
        results.append(CheckResult("DOC-VERDICTS-FRESH", "FAIL", False, "state invalid"))
        for cid in (
            "DOC-FINGERPRINT-RULES", "DOC-FINGERPRINT-PLAYERS",
            "DOC-FINGERPRINT-SEMESTERS", "DOC-FINGERPRINT-GROUPS",
        ):
            results.append(CheckResult(cid, "WARN", False, "state invalid"))
    else:
        rows, ledger_results = _check_ledger_and_verdicts(
            config, ledger_path, state, verdicts_path,
        )
        results.extend(ledger_results)
        if rows is not None:
            results.extend(_check_fingerprints(config, rows, state))
        else:
            for cid in (
                "DOC-FINGERPRINT-RULES", "DOC-FINGERPRINT-PLAYERS",
                "DOC-FINGERPRINT-SEMESTERS", "DOC-FINGERPRINT-GROUPS",
            ):
                results.append(CheckResult(cid, "WARN", False, "ledger invalid"))

    results.append(_check_persistence_files(config))

    try:
        rule_now = config.rules.in_force_at(now)
        selfie_bonus_now = rule_now.selfie_bonus
    except NoRuleInForceError:
        selfie_bonus_now = False

    results.append(_check_faces_model(config, selfie_bonus_now))
    results.append(_check_faces_import(selfie_bonus_now))

    if online:
        if slack is None:
            results.append(CheckResult("DOC-AUTH", "FAIL", False, "no Slack client available"))
        else:
            identity, r = _check_auth(slack)
            results.append(r)
            results.append(_check_scopes(slack, config, now, mpath))
            results.append(_check_channel_member(slack, "DOC-CHANNEL-MEMBER", config.channel))
            results.append(_check_postto_member(slack, config))
            results.append(_check_roster_resolve(slack, config))
            results.append(_check_admin_resolve(slack, config))
            results.append(_check_roster_bot(slack, config, now))
            results.append(_check_optout_msg(slack, config))

    _emit(results, args)
    failed = any(r.severity == "FAIL" and not r.ok for r in results)
    return Exit.DOCTOR_FAILED if failed else Exit.OK


def _emit(results: list[CheckResult], args: Any) -> None:
    if getattr(args, "json", False):
        for r in results:
            print(json_lib.dumps(
                {"id": r.id, "severity": r.severity, "ok": r.ok, "detail": r.detail},
                sort_keys=True,
            ))
        return
    for r in results:
        print(_line(r))
    total = len(results)
    failed = sum(1 for r in results if r.severity == "FAIL" and not r.ok)
    warned = sum(1 for r in results if r.severity == "WARN" and not r.ok)
    print(f"{total} checks, {failed} failed, {warned} warned")
