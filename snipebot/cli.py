"""Command-line entry point (40-config-cli.md section 4).

`python -m snipebot <command>` / console script `snipebot`. Argparse dispatches the
commands of plan section 7 plus `selfie`. Two flags are accepted by every command:
`--config`, `--data-dir` and `-v/--verbose` (section 4). Logs go to stderr with Slack IDs
only; command output goes to stdout. Exit codes are the `Exit` enum (section 4.4, homed
here).

Slack-touching commands build their client through an injected `slack_factory` (default: a
lazy import of the real transport, a later io-wave module) and their face detector through
`detector_factory` (default: the vendored YuNet). Tests inject a `FakeSlack` and a
`FakeFaceDetector` and drive every command without a network or a git checkout.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from snipebot.aggregate import eligible_snipes
from snipebot.config import (
    Config,
    ConfigError,
    FingerprintGuardError,
    NoRuleInForceError,
    Persistence,
    RosterMode,
    _local_to_us,
    _parse_datetime,
    compute_fingerprints,
    fingerprint_guard,
    load_config,
)
from snipebot.export import BY_TO_TABLE, collect_display_ids, export_all, render_table_text
from snipebot.ledger import (
    MalformedLedgerError,
    load_ledger,
    load_state,
    save_ledger,
    save_state,
    save_verdicts,
)
from snipebot.rules import evaluate
from snipebot.persistence import GitCommandError, GitStore, LeaseRejected, store_for
from snipebot.report import NameResolver
from snipebot.sync import (
    Command,
    SyncResult,
    _baseline_reason_counts,
    _baseline_row_ts,
    _commit_message,
    _delta_line_names,
    _reason_deltas,
    run_sync,
    run_sync_git,
)
from snipebot.ts import US_PER_SECOND, TsFormatError, parse_ts

logger = logging.getLogger("snipebot")


# --- Exit codes (40 section 4.4, homed here) ---------------------------------

class Exit(int, Enum):
    OK = 0
    UNEXPECTED = 1               # uncaught error
    CONFIG_INVALID = 2           # ConfigError; unknown semester / malformed arg; NoRuleInForceError
    GUARD_REFUSED = 3            # FingerprintGuardError (section 3)
    LEDGER_MALFORMED = 4         # MalformedLedgerError
    SLACK_ERROR = 5              # SlackApiError / network / a failed history page / missing token
    BREAKER_TRIPPED = 6          # newly_deleted > max_deletes_per_run, no --count
    ACCEPT_DELETES_MISMATCH = 7  # accept-deletes --count N != newly_deleted
    INTEGRITY_FAILED = 8         # end-of-sync integrity / verdicts-staleness check failed
    LEASE_FAILED = 9             # --force-with-lease rejected after bounded retries
    DOCTOR_FAILED = 10           # `doctor` found at least one FAIL check


# Injection seams (module-level so `main`'s signature stays fixed; tests patch these).
def _now_us() -> int:
    """Current wall clock as integer microseconds since the epoch."""
    return int(time.time() * US_PER_SECOND)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")
_TS_RE = re.compile(r"^\d+\.\d{1,6}$")


class _CliError(Exception):
    """A handled failure: carries the exit code and a one-line stderr message."""

    def __init__(self, code: Exit, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- helpers -----------------------------------------------------------------

def _load_config(args: argparse.Namespace, is_bot: dict[str, bool] | None = None) -> Config:
    path = Path(args.config)
    if not path.exists():
        raise _CliError(Exit.CONFIG_INVALID, f"config not found: {path}")
    try:
        return load_config(path, is_bot=is_bot)
    except ConfigError as exc:
        raise _CliError(Exit.CONFIG_INVALID, f"config invalid: {exc}") from exc


def _load_config_with_bots(args: argparse.Namespace, slack) -> Config:
    """Reload the config with the real is_bot map from `users.list` (40 §2.1: sync passes
    a real map), so RosterEntry.is_bot and the TARGET_IS_BOT gate see rostered bots."""
    from snipebot.slack_io import SlackError

    try:
        users = slack.users_list()
    except SlackError as exc:
        raise _CliError(Exit.SLACK_ERROR, f"slack error: {exc}") from exc
    is_bot = {u["id"]: bool(u.get("is_bot")) for u in users if u.get("id")}
    config = _load_config(args, is_bot)
    _record_roster_is_bot(args, _is_bot_to_record(config, is_bot))
    return config


def _is_bot_to_record(config: Config, is_bot: Mapping[str, bool]) -> dict[str, bool]:
    """The is_bot entries kept in users.json: every rostered user under `listed`; every
    user users.list returned under `auto`, where any bot target is off-roster, so report
    and export judge bots exactly as sync did (E-W4-42)."""
    if config.roster.mode is RosterMode.AUTO:
        return {uid: bool(flag) for uid, flag in is_bot.items()}
    return {uid: bool(is_bot.get(uid, False)) for uid in config.roster.entries}


def _read_users_json(args: argparse.Namespace) -> dict:
    """The raw local users.json object, or {} when absent or unreadable."""
    path = Path(args.data_dir) / "users.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _record_roster_is_bot(args: argparse.Namespace, roster_is_bot: dict[str, bool]) -> None:
    """Keep the resolved is_bot of every rostered user in the local users.json cache (40
    §2.1 names it an is_bot source), so a later Slack-free command (`purge`) resolves the
    roster exactly as the last sync did. User IDs and booleans only; an existing display
    name is kept as is. users.json is local only and never committed."""
    if not roster_is_bot:
        return
    raw = _read_users_json(args)
    changed = False
    for uid, flag in roster_is_bot.items():
        cur = raw.get(uid)
        if isinstance(cur, dict):
            entry = dict(cur)
        elif isinstance(cur, str) and cur:
            entry = {"display_name": cur}
        else:
            entry = {}
        entry["is_bot"] = flag
        if cur != entry:
            raw[uid] = entry
            changed = True
    if not changed:
        return
    data_dir = Path(args.data_dir)
    tmp_name: str | None = None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".users-", suffix=".json.tmp", dir=data_dir)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(raw, ensure_ascii=False, sort_keys=True))
        os.replace(tmp_name, data_dir / "users.json")
        tmp_name = None
    except OSError:
        # A cache refresh never fails the run; purge then falls back to Slack.
        logger.warning("users cache not refreshed")
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)


def _cached_is_bot(args: argparse.Namespace) -> dict[str, bool]:
    """user ID -> is_bot recorded in the local users.json cache (entries without it omitted)."""
    return {
        uid: val["is_bot"]
        for uid, val in _read_users_json(args).items()
        if isinstance(val, dict) and isinstance(val.get("is_bot"), bool)
    }


def _data_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    d = Path(args.data_dir)
    return d / "ledger.jsonl", d / "state.json"


def _bot_token() -> str:
    """SLACK_BOT_TOKEN, stripped and checked once. The value never appears in a message: a
    token with inner whitespace or control characters is refused before any request carries
    it (an HTTP layer would echo it back in its error text)."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise _CliError(Exit.SLACK_ERROR, "SLACK_BOT_TOKEN is not set")
    if any(c.isspace() or not c.isprintable() or not c.isascii() for c in token):
        raise _CliError(
            Exit.SLACK_ERROR,
            "SLACK_BOT_TOKEN is malformed (whitespace or control characters); "
            "re-enter the secret on one line",
        )
    return token


def _get_slack(config: Config, slack_factory: Callable[[], object] | None):
    if slack_factory is not None:
        return slack_factory()
    token = _bot_token()
    try:
        from snipebot.slack_io import make_client  # type: ignore[attr-defined]
    except ImportError as exc:
        raise _CliError(
            Exit.CONFIG_INVALID, "Slack transport is not available in this build"
        ) from exc
    return make_client(
        token,
        fetch_timeout_seconds=config.faces.fetch_timeout_seconds,
        max_image_bytes=config.faces.max_image_bytes,
    )


def _get_detector(config: Config, detector_factory: Callable[[], object] | None):
    if detector_factory is not None:
        return detector_factory()
    from snipebot.faces import LazyDetector

    # Construct YuNetDetector (and import cv2) lazily, on the first count_faces call:
    # a run whose rules never put selfie_bonus in force never counts a face, so it
    # imports no OpenCV (10 §9). The decimal score_threshold is a string, parsed only
    # inside the detector (40 §1.10), so it is handed over verbatim.
    model_path = config.faces.model_path
    score_threshold = config.faces.score_threshold

    def _build():
        from snipebot.faces import YuNetDetector

        return YuNetDetector(model_path, score_threshold)

    return LazyDetector(_build)


def _run_sync(config: Config, slack, detector, args: argparse.Namespace, **flags) -> SyncResult:
    """Dispatch to the persistence-appropriate sync runner (git wrapper vs. plain)."""
    ledger_path, state_path = _data_paths(args)
    now_us = _now_us()
    if config.persistence == Persistence.GIT:
        try:
            return run_sync_git(
                slack, config, detector=detector, ledger_path=ledger_path,
                state_path=state_path, now_us=now_us, now_fn=_now_us, **flags,
            )
        except LeaseRejected:
            return SyncResult(int(Exit.LEASE_FAILED), False, None, 0, 0, 0, 0, 0)
    return run_sync(
        slack, config, detector=detector, ledger_path=ledger_path,
        state_path=state_path, now_us=now_us, **flags,
    )


def _print_write_outcome(config: Config, result: SyncResult) -> None:
    """The section 4.3 confirming block: the persistence line plus the `moved:` line of
    §8.4 verdict flips by reason (never IDs or names). When nothing moved (empty
    `moved_lines`) the block is a single `no change` under both modes, even when the run
    rewrote state.json (the watermark moves on every scheduled pass)."""
    if not result.moved_lines:
        print("no change")
        return
    if config.persistence == Persistence.FILES:
        print("pushed local only")
    else:
        if result.commit_sha:
            if result.sealed_sha:
                print(f"commit {result.sealed_sha}")
            print(f"movement {result.commit_sha}")
            print("pushed data")
        else:
            print("no change")
    print("moved: " + " ".join(result.moved_lines))


def _print_run_summary(result: SyncResult) -> None:
    """The 40 §4.2 one-line `sync` run summary on stdout: rows scanned, verdict flips by
    reason, messages flagged for review, selfies, faces_fetched, ambiguous_selfie and repost
    counts, digests posted/revised, and the delete-breaker state. Counts only — never IDs or
    names (§4 preamble). Logs stay on stderr; this line precedes the §4.3 confirming block."""
    flips = " ".join(result.moved_lines) if result.moved_lines else "none"
    print(
        "summary: "
        f"scanned {result.rows_scanned} "
        f"flips {flips} "
        f"review {result.needs_review} "
        f"selfies {result.selfies} "
        f"faces {result.faces_fetched} "
        f"ambiguous_selfie {result.ambiguous_selfie} "
        f"repost {result.repost} "
        f"digests posted {result.digests_posted} revised {result.digests_revised} "
        f"breaker {'released' if result.breaker_released else 'ok'}"
    )


def _users_cache(args: argparse.Namespace) -> dict[str, str]:
    """id -> display name from data-dir/users.json, tolerating either a flat map or the
    raw {id: {display_name, real_name}} shape. Absent file -> empty (names fall back to
    the bracketed ID)."""
    path = Path(args.data_dir) / "users.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cache: dict[str, str] = {}
    if isinstance(raw, dict):
        for uid, val in raw.items():
            if isinstance(val, str):
                cache[uid] = val
            elif isinstance(val, dict):
                cache[uid] = str(val.get("display_name") or val.get("real_name") or "")
    return cache


def _pick_semester(config: Config, name: str | None):
    if name is not None:
        for sem in config.semesters:
            if sem.name == name:
                return sem
        raise _CliError(Exit.CONFIG_INVALID, f"unknown semester: {name}")
    # 30 §7: the latest semester that started on or before now, i.e. the one containing now,
    # else (between semesters) the one that just ended, never one that has not started yet
    # (the tiling `history` uses). With every semester still ahead, the latest-ending one.
    now = _now_us()
    started = [sem for sem in config.semesters if sem.start_us <= now]
    if started:
        return max(started, key=lambda s: s.start_us)
    return max(config.semesters, key=lambda s: s.end_us)


def _opted_out(config: Config, args: argparse.Namespace) -> set[str]:
    _, state_path = _data_paths(args)
    try:
        state = load_state(state_path)
    except MalformedLedgerError as exc:
        raise _CliError(Exit.LEDGER_MALFORMED, f"state malformed: {exc}") from exc
    # 40 §2: `evaluate` receives the durable state set, never the config seeds; only a sync
    # folds seeds into state (20 §5.2), so purge/report/export agree with DOC-VERDICTS-FRESH.
    return set(state.opted_out)


def _load_rows(args: argparse.Namespace):
    ledger_path, _ = _data_paths(args)
    try:
        return load_ledger(ledger_path)
    except MalformedLedgerError as exc:
        raise _CliError(Exit.LEDGER_MALFORMED, f"ledger malformed: {exc}") from exc


def _resolve_from(config: Config, value: str) -> int:
    for sem in config.semesters:
        if sem.name == value:
            return sem.start_us
    if _TS_RE.match(value):
        try:
            return parse_ts(value)
        except TsFormatError as exc:
            raise _CliError(
                Exit.CONFIG_INVALID, f"--from: not a ts, date, or semester name: {value}"
            ) from exc
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise _CliError(
            Exit.CONFIG_INVALID, f"--from: not a ts, date, or semester name: {value}"
        ) from exc
    local = datetime(d.year, d.month, d.day, tzinfo=config.tz)
    return int(local.timestamp() * US_PER_SECOND)


def _local_day(config: Config) -> str:
    """The local day of the injected clock (`_now_us`), for a commit header (20 §8.4)."""
    return _us_local_day(_now_us(), config)


def _us_local_day(us: int, config: Config) -> str:
    """The local calendar date (YYYY-MM-DD) of an instant in integer microseconds."""
    return datetime.fromtimestamp(us // US_PER_SECOND, config.tz).strftime("%Y-%m-%d")


# --- purge --rewrite-history: data-branch rewrite (40 §4.2) -------------------

_LEDGER_BLOB = "data/ledger.jsonl"
_VERDICTS_BLOB = "data/verdicts.jsonl"


def _git_out(repo: Path, args: list[str], *, env: dict[str, str] | None = None,
             stdin: bytes | None = None) -> bytes:
    """Run one git plumbing command in `repo`; raise GitCommandError on a non-zero exit.
    The message names the subcommand only (never paths, ids or git's own stderr)."""
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=str(repo), capture_output=True, env=env, input=stdin,
    )
    if proc.returncode != 0:
        raise GitCommandError(f"git {args[0]} failed ({proc.returncode})")
    return proc.stdout


def _blob_at(repo: Path, rev: str, path: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "show", f"{rev}:{path}"],
        cwd=str(repo), capture_output=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def _row_names_user(user: str, sender: object, targets, first_seen, edited_in_users) -> bool:
    """True when a ledger row names `user` anywhere a purge must erase (40 §4.2): as the
    sender, a current target, a first-seen target (tagged at posting, 00-data §2) or an
    edited-in target. Shared by the live purge and the history rewrite."""
    return (
        sender == user
        or user in targets
        or user in first_seen
        or user in edited_in_users
    )


def _names_user(line: bytes, user: str, dropped_ts: set[str] | None) -> bool:
    """True when a ledger line (dropped_ts None) or a verdicts line must go for `user`."""
    try:
        obj = json.loads(line)
    except ValueError:
        return user.encode("ascii") in line          # unparseable: erase on any mention
    if not isinstance(obj, dict):
        return user.encode("ascii") in line
    if dropped_ts is None:
        edited = obj.get("target_edited_in") or []
        return _row_names_user(
            user, obj.get("sender"), obj.get("targets") or [],
            obj.get("first_seen_targets") or [],
            [e.get("user") for e in edited if isinstance(e, dict)],
        )
    if obj.get("ts") in dropped_ts:
        return True
    return any(isinstance(p, dict) and p.get("target") == user for p in obj.get("pairs") or [])


def _drop_user(ledger: bytes, verdicts: bytes | None, user: str) -> tuple[bytes, bytes | None]:
    """One snapshot's ledger/verdicts bytes with `user`'s rows (sender or target) and the
    verdict rows of those messages removed; every other line is kept byte for byte."""
    kept: list[bytes] = []
    dropped_ts: set[str] = set()
    for line in ledger.splitlines(keepends=True):
        if line.strip() and _names_user(line, user, None):
            try:
                ts = json.loads(line).get("ts")
            except (ValueError, AttributeError):
                ts = None
            if isinstance(ts, str):
                dropped_ts.add(ts)
            continue
        kept.append(line)
    if verdicts is None:
        return b"".join(kept), None
    kept_v = [
        line for line in verdicts.splitlines(keepends=True)
        if not (line.strip() and _names_user(line, user, dropped_ts))
    ]
    return b"".join(kept), b"".join(kept_v)


def _rewrite_data_history(store: GitStore, user: str) -> int:
    """Rewrite every commit of the local data branch with `user`'s ledger rows (sender or
    target) and their verdict rows dropped, keeping each commit's message, author and dates.
    Moves the local branch to the rewritten tip (the working tree is left as is) and returns
    how many commits had their snapshot changed. The caller's lease-guarded push publishes
    it."""
    repo = store.repo_path
    ref = f"refs/heads/{store.branch}"
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=str(repo), capture_output=True, text=True,
    )
    if head.returncode != 0 or not head.stdout.strip():
        return 0                                     # unborn branch: nothing to rewrite
    old_tip = head.stdout.strip()
    revs = _git_out(repo, ["rev-list", "--reverse", "--topo-order", ref]).decode().split()
    mapping: dict[str, str] = {}
    rewritten = 0
    with tempfile.TemporaryDirectory() as tmp:
        index_env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        for sha in revs:
            parents = _git_out(repo, ["rev-list", "--parents", "-n", "1", sha]).decode().split()[1:]
            new_parents = [mapping.get(p, p) for p in parents]
            tree = _git_out(repo, ["rev-parse", f"{sha}^{{tree}}"]).decode().strip()
            new_tree = tree
            ledger = _blob_at(repo, sha, _LEDGER_BLOB)
            if ledger is not None:
                verdicts = _blob_at(repo, sha, _VERDICTS_BLOB)
                new_ledger, new_verdicts = _drop_user(ledger, verdicts, user)
                if new_ledger != ledger or new_verdicts != verdicts:
                    _git_out(repo, ["read-tree", sha], env=index_env)
                    for path, content in ((_LEDGER_BLOB, new_ledger),
                                          (_VERDICTS_BLOB, new_verdicts)):
                        if content is None:
                            continue
                        blob = _git_out(repo, ["hash-object", "-w", "--stdin"], stdin=content)
                        _git_out(repo, ["update-index", "--add", "--cacheinfo",
                                        f"100644,{blob.decode().strip()},{path}"],
                                 env=index_env)
                    new_tree = _git_out(repo, ["write-tree"], env=index_env).decode().strip()
            if new_tree == tree and new_parents == parents:
                mapping[sha] = sha
                continue
            if new_tree != tree:
                rewritten += 1
            meta = _git_out(
                repo, ["log", "-1", "--date=raw",
                       "--format=%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd", sha],
            ).decode("utf-8").rstrip("\n").split("\x00")
            message = _git_out(repo, ["log", "-1", "--format=%B", sha])
            commit_env = dict(
                os.environ,
                GIT_AUTHOR_NAME=meta[0], GIT_AUTHOR_EMAIL=meta[1], GIT_AUTHOR_DATE=meta[2],
                GIT_COMMITTER_NAME=meta[3], GIT_COMMITTER_EMAIL=meta[4],
                GIT_COMMITTER_DATE=meta[5],
            )
            cmd = ["commit-tree", new_tree]
            for p in new_parents:
                cmd += ["-p", p]
            cmd += ["-F", "-"]
            mapping[sha] = _git_out(repo, cmd, env=commit_env, stdin=message).decode().strip()
    new_tip = mapping[revs[-1]]
    if new_tip != old_tip:
        _git_out(repo, ["update-ref", ref, new_tip, old_tip])
    return rewritten


def _acting_by(config: Config, given: str | None) -> str:
    if given is not None:
        if not _USER_ID_RE.match(given):
            raise _CliError(Exit.CONFIG_INVALID, f"--by: not a user ID: {given}")
        return given
    if not config.admins:
        raise _CliError(Exit.CONFIG_INVALID, "--by is required (no admins configured)")
    return config.admins[0]


# --- command handlers --------------------------------------------------------

def _cmd_sync(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    slack = _get_slack(config, slack_factory)
    config = _load_config_with_bots(args, slack)
    detector = _get_detector(config, detector_factory)
    result = _run_sync(
        config, slack, detector, args, command=Command.SYNC,
        no_react=args.no_react, no_post=args.no_post, reevaluate=args.reevaluate,
    )
    if result.exit_code == 0:
        _print_run_summary(result)
        _print_write_outcome(config, result)
    return result.exit_code


def _cmd_run(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    slack = _get_slack(config, slack_factory)
    config = _load_config_with_bots(args, slack)
    detector = _get_detector(config, detector_factory)
    interval_seconds = config.sync.interval_minutes * 60
    try:
        while True:
            result = _run_sync(
                config, slack, detector, args, command=Command.SYNC,
                no_react=args.no_react, no_post=args.no_post, reevaluate=args.reevaluate,
            )
            if result.exit_code == int(Exit.GUARD_REFUSED):
                return int(Exit.GUARD_REFUSED)
            if result.exit_code == 0:
                _print_run_summary(result)
                _print_write_outcome(config, result)
            if args.once:
                return result.exit_code
            _sleep(interval_seconds)
    except KeyboardInterrupt:
        return int(Exit.OK)


def _cmd_report(args, slack_factory, detector_factory) -> int:
    # 40 §2.1 (E-W4-18): the is_bot map from the local users.json cache, so a rostered bot
    # is judged the way sync judged it and report agrees with the synced verdicts.
    config = _load_config(args, _cached_is_bot(args))
    rows = _load_rows(args)
    opted = _opted_out(config, args)
    sem = _pick_semester(config, args.semester)
    try:
        elig = eligible_snipes(
            rows, config.rules, config.roster, opted, config.semesters, config.tz, sem
        )
    except NoRuleInForceError as exc:
        raise _CliError(Exit.CONFIG_INVALID, f"no rule in force: {exc}") from exc
    table = BY_TO_TABLE[args.by]
    from snipebot.export import build_all_tables

    rows_t = build_all_tables(elig, config.roster, opted)[table]
    resolver = NameResolver(_users_cache(args), config.roster)
    names = resolver.resolve_all(collect_display_ids({table: rows_t}))
    print(render_table_text(table, rows_t, names))
    return int(Exit.OK)


def _cmd_export(args, slack_factory, detector_factory) -> int:
    config = _load_config(args, _cached_is_bot(args))  # E-W4-18, as `report`
    rows = _load_rows(args)
    opted = _opted_out(config, args)
    sem = _pick_semester(config, args.semester)
    try:
        elig = eligible_snipes(
            rows, config.rules, config.roster, opted, config.semesters, config.tz, sem
        )
    except NoRuleInForceError as exc:
        raise _CliError(Exit.CONFIG_INVALID, f"no rule in force: {exc}") from exc
    out_dir = Path(args.out)
    resolver = NameResolver(_users_cache(args), config.roster)
    export_all(elig, config.roster, opted, resolver, sem.name, out_dir)
    from snipebot.export import TABLE_ORDER

    for table in TABLE_ORDER:
        print(out_dir / f"{sem.name}_{table}.csv")
    print(out_dir / f"{sem.name}.xlsx")
    return int(Exit.OK)


def _cmd_review(args, slack_factory, detector_factory) -> int:
    """Local photo review: queue the ambiguous face reads, record the owner's yes/no.
    Never writes Slack, the ledger or the data branch; see snipebot/review.py."""
    from snipebot import review

    folder = Path(args.dir)
    try:
        if args.review_command == "label":
            if not _TS_RE.match(args.ts):
                raise _CliError(Exit.CONFIG_INVALID, f"--ts: not a message ts: {args.ts}")
            try:
                review.append_label(folder, args.ts, args.label)
            except TsFormatError as exc:
                raise _CliError(Exit.CONFIG_INVALID, f"--ts: {exc}") from exc
            print(f"{args.ts} {args.label}")
            return int(Exit.OK)

        config = _load_config(args)
        sem = _pick_semester(config, args.semester)
        clear_milli = review.threshold_milli(config.faces.score_threshold)
        min_targets = config.review.min_targets
        cache = review.load_cache(folder)

        if args.review_command == "stats":
            labels = review.load_labels(folder)
            for line in review.stats_lines(cache, labels, clear_milli, min_targets,
                                           sem.start_us, sem.end_us):
                print(line)
            return int(Exit.OK)

        from snipebot.slack_io import SlackError
        from snipebot.ts import format_ts

        slack = _get_slack(config, slack_factory)
        if detector_factory is not None:
            detector = detector_factory()
        else:
            from snipebot.faces import YuNetDetector

            # A low floor so faint faces are kept; the live threshold is applied when
            # reasons are computed, so a threshold change needs no rescan.
            detector = YuNetDetector(config.faces.model_path, "0.5")
        try:
            auth = slack.auth_identity()
            result = review.scan(
                slack, config.channel, format_ts(sem.start_us), format_ts(sem.end_us),
                detector, cache, auth.user_id,
                progress=lambda n, d: print(f"  {n} posts, {d} new photos read", file=sys.stderr),
            )
        except SlackError as exc:
            raise _CliError(Exit.SLACK_ERROR, f"slack: {type(exc).__name__}") from exc
        finally:
            review.save_cache(folder, cache)
        print(f"scanned {result.posts} tagged photo posts in {sem.name}: "
              f"{result.detected} new photos read, {result.failed} could not be read",
              file=sys.stderr)

        labels = review.load_labels(folder)
        items = review.build_queue(cache, labels, clear_milli, min_targets,
                                   sem.start_us, sem.end_us, everything=args.all)

        def local_time(ts: str) -> str:
            moment = datetime.fromtimestamp(parse_ts(ts) // US_PER_SECOND, tz=config.tz)
            return moment.strftime("%a %b %d %H:%M")

        if args.review_command == "list":
            for it in items:
                why = ", ".join(review.REASON_TEXT[r] for r in it.reasons) or "-"
                print(f"{it.ts}  {local_time(it.ts)}  {it.label or 'open':<4}  {why}  "
                      f"{review.permalink(auth.url, config.channel, it.ts)}")
            print(f"{len(items)} queued, {sum(1 for i in items if i.label is None)} open",
                  file=sys.stderr)
            return int(Exit.OK)

        payload = review.gallery_payload(items, cache, clear_milli, auth.url,
                                         config.channel, local_time)
        review.serve(folder, payload, slack.fetch_file_bytes, result.urls,
                     f"Photo review, {sem.name}", open_browser=not args.no_browser)
        return int(Exit.OK)
    except review.ReviewError as exc:
        raise _CliError(Exit.CONFIG_INVALID, f"review: {exc}") from exc


def _cmd_roster(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    slack = _get_slack(config, slack_factory)
    from snipebot.slack_io import SlackError

    try:
        member_ids = set(slack.conversations_members(config.channel))
        users = {u["id"]: u for u in slack.users_list()}
    except SlackError as exc:
        raise _CliError(Exit.SLACK_ERROR, f"slack error: {exc}") from exc
    lines: list[tuple[str, str]] = []
    for uid in member_ids:
        u = users.get(uid)
        if u is None or u.get("is_bot") or u.get("deleted"):
            continue
        profile = u.get("profile", {})
        name = profile.get("display_name") or profile.get("real_name") or uid
        lines.append((name, uid))
    # §4.2: roster "refreshes the `users.json` cache locally" — the same id->name cache
    # report/export read (`_users_cache`). Store user IDs and display names only (never
    # permalinks/URLs/file names), consistent with the logging rules. This is a local
    # cache refresh, not a ledger/commit write, so it stays within "Writes nothing".
    # Written before printing so an output failure can never leave the cache stale.
    # The cache covers every human users.list entry, channel member or not, as sync's does
    # (20 §2): a rostered player who left the channel or was deactivated still has counted
    # snipes and keeps their name in the export, and a nameless account is cached as "" so
    # it prints as the bracketed `[U...]` fallback (30 §4), exactly as the digest shows it.
    # Bots hold no standings, so they get only the is_bot entry recorded below.
    cache = {}
    for uid, u in users.items():
        if uid and not u.get("is_bot"):
            profile = u.get("profile") or {}
            cache[uid] = profile.get("display_name") or profile.get("real_name") or ""
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "users.json").write_text(
        json.dumps(cache, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    # The rewrite above drops any is_bot a sync recorded, and a fresh checkout (the export
    # workflow, 40 §7.4) has no other source: record it for every rostered user from the same
    # users.list, so report/export still judge a rostered bot the way sync did (E-W4-18).
    _record_roster_is_bot(
        args, _is_bot_to_record(
            config, {uid: bool(u.get("is_bot")) for uid, u in users.items() if uid}
        )
    )
    for name, uid in sorted(lines):
        print(f"{uid}  {name}")
    return int(Exit.OK)


def _cmd_backfill(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    slack = _get_slack(config, slack_factory)
    config = _load_config_with_bots(args, slack)
    detector = _get_detector(config, detector_factory)
    from_us = _resolve_from(config, args.from_)
    result = _run_sync(
        config, slack, detector, args, command=Command.BACKFILL,
        backfill_from_us=from_us, dry_run=args.dry_run,
        no_react=args.no_react, no_post=args.no_post,
    )
    if result.exit_code == 0:
        if args.dry_run:
            # 40 §4.2 backfill Output: "verdict counts by reason; with --dry-run also the L8
            # audit list". Print the by-reason verdict counts to stdout (the point of a dry
            # run); the L8 audit list stays on stderr (§4 preamble) and no commit is made.
            if result.counts_by_reason:
                print("verdict counts: " + " ".join(result.counts_by_reason))
            print("dry run — no writes (audit list on stderr)")
        else:
            _print_write_outcome(config, result)
    return result.exit_code


def _cmd_veto(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    by = _acting_by(config, args.by)
    rows = _load_rows(args)
    idx = next((i for i, r in enumerate(rows) if r.ts == args.ts), None)
    if idx is None:
        raise _CliError(Exit.CONFIG_INVALID, f"no ledger row at ts {args.ts}")
    slack = _get_slack(config, slack_factory)
    config = _load_config_with_bots(args, slack)
    detector = _get_detector(config, detector_factory)
    result = _run_sync(
        config, slack, detector, args, command=Command.VETO,
        veto_ts=args.ts, veto_by=by, veto_remove=False,
        no_react=args.no_react, no_post=args.no_post,
    )
    if result.exit_code == 0:
        # 40 §4.2 veto Output: "the message's new verdict, verdict flips by reason, the
        # produced commit". The new message-level verdict (VETOED, 00-data §4) is distinct
        # from the §4.3 `moved:` flips-by-reason line, so print it in its own line first.
        if result.target_verdict is not None:
            print(f"verdict: {result.target_verdict}")
        _print_write_outcome(config, result)
    return result.exit_code


def _cmd_unveto(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    # A None `--by` legitimately means "remove all CLI vetoes", so validate the
    # format only when one is given (40 §4.2 veto/unveto Exit row).
    if args.by is not None and not _USER_ID_RE.match(args.by):
        raise _CliError(Exit.CONFIG_INVALID, f"--by: not a user ID: {args.by}")
    rows = _load_rows(args)
    idx = next((i for i, r in enumerate(rows) if r.ts == args.ts), None)
    if idx is None:
        raise _CliError(Exit.CONFIG_INVALID, f"no ledger row at ts {args.ts}")
    slack = _get_slack(config, slack_factory)
    config = _load_config_with_bots(args, slack)
    detector = _get_detector(config, detector_factory)
    result = _run_sync(
        config, slack, detector, args, command=Command.UNVETO,
        veto_ts=args.ts, veto_by=args.by, veto_remove=True,
        no_react=args.no_react, no_post=args.no_post,
    )
    if result.exit_code == 0:
        # 40 §4.2 unveto Output is likewise "the message's new verdict, ...": print the
        # target row's resulting message-level verdict after CLI vetoes are removed.
        if result.target_verdict is not None:
            print(f"verdict: {result.target_verdict}")
        _print_write_outcome(config, result)
    return result.exit_code


def _cmd_selfie(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    by = _acting_by(config, args.by)
    slack = _get_slack(config, slack_factory)
    config = _load_config_with_bots(args, slack)
    detector = _get_detector(config, detector_factory)
    result = _run_sync(
        config, slack, detector, args, command=Command.SELFIE,
        selfie_ts=args.ts, selfie_value=not args.no, selfie_by=by,
        no_react=args.no_react, no_post=args.no_post,
    )
    if result.exit_code == 0:
        # 40 §4.2 selfie Output: "the message's new SelfieClass", read back from the
        # verdicts.jsonl this run just wrote (the class value only, never IDs or names).
        selfie_class = _selfie_class_at(args, args.ts)
        if selfie_class is not None:
            print(f"selfie: {selfie_class}")
        _print_write_outcome(config, result)
    return result.exit_code


def _selfie_class_at(args: argparse.Namespace, ts: str) -> str | None:
    ledger_path, _ = _data_paths(args)
    try:
        text = ledger_path.with_name("verdicts.jsonl").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("ts") == ts and isinstance(obj.get("selfie"), str):
            return obj["selfie"]
    return None


def _optout_messages_reacted(config: Config, slack, user: str) -> list[str]:
    """The configured opt-out message ts values `user` still reacts on (any emoji, 20
    §5.2), read with `reactions_get` for complete user lists. A deleted message holds no
    reaction; any other Slack failure ends the command, since the check cannot be made."""
    from snipebot.slack_io import MessageNotFound, SlackError

    found: list[str] = []
    for msg_ts in config.consent.optout_message_ts:
        try:
            raw = slack.reactions_get(config.channel, msg_ts)
        except MessageNotFound:
            continue
        except SlackError as exc:
            raise _CliError(Exit.SLACK_ERROR, f"slack error: {exc}") from exc
        if any(user in r.get("users", []) for r in raw.get("reactions", []) or []):
            found.append(msg_ts)
    return found


def _cmd_rejoin(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    if not _USER_ID_RE.match(args.user):
        raise _CliError(Exit.CONFIG_INVALID, f"not a user ID: {args.user}")
    _, state_path = _data_paths(args)
    try:
        state = load_state(state_path)
    except MalformedLedgerError as exc:
        raise _CliError(Exit.LEDGER_MALFORMED, f"state malformed: {exc}") from exc
    if args.user not in state.opted_out:
        raise _CliError(Exit.CONFIG_INVALID, f"{args.user} is not opted out")
    # E-W4-25: a config seed or a live opt-out reaction would re-add the user on the next
    # sync, so either one refuses the rejoin and names what to remove first.
    if args.user in config.consent.seed_opted_out:
        raise _CliError(
            Exit.CONFIG_INVALID,
            f"{args.user} is still in consent.opted_out; remove it from the config first",
        )
    slack = _get_slack(config, slack_factory)
    reacting = _optout_messages_reacted(config, slack, args.user)
    if reacting:
        raise _CliError(
            Exit.CONFIG_INVALID,
            f"{args.user} still reacts on opt-out message {' '.join(reacting)}; "
            "remove that reaction first",
        )
    config = _load_config_with_bots(args, slack)
    detector = _get_detector(config, detector_factory)
    result = _run_sync(
        config, slack, detector, args, command=Command.REJOIN,
        rejoin_user=args.user,
        no_react=args.no_react, no_post=args.no_post,
    )
    if result.exit_code == 0:
        print(f"rejoined {args.user}")
        _print_write_outcome(config, result)
    return result.exit_code


def _cmd_accept_deletes(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    slack = _get_slack(config, slack_factory)
    config = _load_config_with_bots(args, slack)
    detector = _get_detector(config, detector_factory)
    result = _run_sync(
        config, slack, detector, args, command=Command.ACCEPT_DELETES,
        accept_deletes=args.count, no_react=args.no_react, no_post=args.no_post,
    )
    if result.exit_code == int(Exit.ACCEPT_DELETES_MISMATCH):
        print(
            f"accept-deletes: given {args.count}, channel now shows "
            f"{result.newly_deleted} newly deleted; retry with --count {result.newly_deleted}",
            file=sys.stderr,
        )
    elif result.exit_code == 0:
        print(f"newly_deleted {result.newly_deleted} released")
        _print_write_outcome(config, result)
    return result.exit_code


def _cmd_rules_bump(args, slack_factory, detector_factory) -> int:
    import yaml

    config = _load_config(args)
    if args.effective_from == "now":
        # The config holds a minute-granular local wall clock, resolved with fold=0
        # (00-data §8). Round the clock UP to the next whole minute, then check the
        # instant the written string resolves to, so the value checked is the value
        # load_config will read (40 §4.2: strictly later than H as an exact instant).
        now = datetime.now(config.tz)
        wall = now.replace(tzinfo=None, fold=0)
        floor = wall.replace(second=0, microsecond=0)
        if floor != wall:
            floor += timedelta(minutes=1)
        eff_str = floor.strftime("%Y-%m-%d %H:%M")
        eff_us = _local_to_us(datetime.strptime(eff_str, "%Y-%m-%d %H:%M"), config.tz)
        # In a repeated (fall-back) hour the fold=0 reading of the rounded minute is up to an
        # hour BEFORE the real instant; advance until the written minute resolves after now.
        now_us = _local_to_us(now.replace(tzinfo=None), config.tz)   # keeps now.fold
        while eff_us <= now_us:
            floor += timedelta(minutes=1)
            eff_str = floor.strftime("%Y-%m-%d %H:%M")
            eff_us = _local_to_us(datetime.strptime(eff_str, "%Y-%m-%d %H:%M"), config.tz)
        naive = datetime.strptime(eff_str, "%Y-%m-%d %H:%M")
    else:
        eff_str = args.effective_from
        text = args.effective_from.replace("T", " ")
        fmt = "%Y-%m-%d %H:%M" if " " in text else "%Y-%m-%d"
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError as exc:
            raise _CliError(
                Exit.CONFIG_INVALID,
                f"--effective-from: not a date-time: {args.effective_from}",
            ) from exc
        eff_us = _local_to_us(naive, config.tz)

    rows = _load_rows(args)
    h_us = max((parse_ts(r.ts) for r in rows), default=None)
    if h_us is not None and eff_us <= h_us:
        raise _CliError(
            Exit.CONFIG_INVALID,
            "--effective-from must be strictly later than the newest ledger row; "
            "use --reevaluate to rewrite history",
        )
    # A dated list must stay strictly increasing (40 §1.5 rule 2); a single mapping's
    # entry sits at INT_MIN_TS, so it never refuses here.
    if eff_us <= config.rules.entries[-1].effective_from_us:
        raise _CliError(
            Exit.CONFIG_INVALID,
            "--effective-from must be later than the last dated rules entry",
        )

    path = Path(args.config)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = doc.get("rules")
    # Choose the first semester by its resolved instant: a raw start may be a bare YAML date
    # or a quoted string (40 §1.4), which do not compare with each other.
    first_sem = min(config.semesters, key=lambda s: s.start_us)
    first_sem_start = next(
        s["start"] for s in doc["semesters"] if s.get("name") == first_sem.name)
    before_entries = 1 if isinstance(rules, dict) else len(rules) if isinstance(rules, list) else 0

    def _prior_date(written: object, written_us: int) -> object:
        """The first entry's effective_from: kept when it is strictly before the new entry,
        else re-dated to the latest minute that resolves strictly before it. rules[0]
        resolves at INT_MIN_TS whatever its written date (40 §1.5 rule 3), so this changes
        no verdict; it only keeps the list strictly increasing and rules[0] at or before
        the first semester start (a pre-season bump)."""
        if written_us < eff_us:
            return written
        cand = naive.replace(second=0, microsecond=0)
        while True:
            cand -= timedelta(minutes=1)
            s = cand.strftime("%Y-%m-%d %H:%M")
            if _local_to_us(datetime.strptime(s, "%Y-%m-%d %H:%M"), config.tz) < eff_us:
                return s

    if isinstance(rules, dict):
        prior = dict(rules)
        prior["effective_from"] = _prior_date(first_sem_start, first_sem.start_us)
        new_entry = dict(rules)
        new_entry["effective_from"] = eff_str
        doc["rules"] = [prior, new_entry]
    elif isinstance(rules, list):
        new_rules = list(rules)
        if len(new_rules) == 1 and isinstance(new_rules[0], dict) \
                and "effective_from" in new_rules[0]:
            try:
                written_us = _parse_datetime(
                    new_rules[0]["effective_from"], config.tz, "rules[0].effective_from")
            except ConfigError as exc:
                raise _CliError(Exit.CONFIG_INVALID, f"config invalid: {exc}") from exc
            first = dict(new_rules[0])
            first["effective_from"] = _prior_date(first["effective_from"], written_us)
            new_rules[0] = first
        doc["rules"] = new_rules + [{"effective_from": eff_str}]
    else:
        raise _CliError(Exit.CONFIG_INVALID, "rules: expected a mapping or a list")

    # Never leave a config.yaml that no longer loads: validate the new text through the
    # same load path first, then swap it in atomically.
    new_text = yaml.safe_dump(doc, sort_keys=False)
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".yaml.tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)
        try:
            load_config(tmp)
        except ConfigError as exc:
            raise _CliError(Exit.CONFIG_INVALID, f"rules bump would make config invalid: {exc}") from exc
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    print(f"rules bump: added entry effective_from {eff_str}")
    # 40 §7.2: `rules bump` only ever edits config.yaml on the code branch (never the data
    # branch or its store), so there is nothing to commit or push here. The admin workflow
    # takes it from here: it commits config.yaml on the code branch under the shared
    # SNIPEBOT_GIT_AUTHOR identity and pushes, then echoes the resulting commit SHA. Print a
    # local confirmation plus a diff summary so the effect is checkable by hand even from a
    # cancelled/never-started run.
    print("config.yaml updated")
    after_entries = len(doc["rules"])
    print(f"rules: {before_entries} entr{'y' if before_entries == 1 else 'ies'} "
          f"-> {after_entries} entries")
    return int(Exit.OK)


# A top-level key line: the key at column 0, plain or quoted and with any spacing before
# its colon (every spelling the loader honours), followed by a space, a tab or the line end.
# The value keeps its surrounding spacing and a trailing ` # comment` (40 §4.2, E-W4-39).
_RECAPS_LINE_RE = re.compile(
    r"""^(?P<head>(?:recaps|"recaps"|'recaps')[ \t]*:[ \t]*)(?P<value>(?:[^#\s].*?)?)"""
    r"(?P<tail>(?:[ \t]+#.*)?[ \t]*)(?P<cr>\r?)$")
_ENABLED_LINE_RE = re.compile(r"^enabled:(?:[ \t].*)?\r?$")
# The document start marker (`---`, alone or before a comment) that ends a leading header of
# blank lines, comments and `%` directives; an inserted key goes below it, never above.
_DOC_START_RE = re.compile(r"^---(?:[ \t].*)?\r?$")


def _recaps_rewrite(text: str, on: bool) -> str:
    """config.yaml text with the top-level `recaps:` set to `on`, every other byte kept.

    An existing line has only its value replaced; a value already spelled as the wanted
    boolean (any case) is left alone. With no such line, `recaps: false` is inserted after
    the top-level `enabled:` line (else at the top, below any `---` document header); an
    absent key is already on (the code default, 40 §1), so `on` inserts nothing.
    """
    word = "true" if on else "false"
    bom = "﻿" if text.startswith("﻿") else ""
    lines = text[len(bom):].split("\n")
    for i, line in enumerate(lines):
        m = _RECAPS_LINE_RE.match(line)
        if m is None or (m["head"].endswith(":") and m["value"]):
            continue                    # not the key (`recaps:true` is a one-word scalar)
        if m["value"].lower() == word:
            return text
        head = m["head"] + " " if m["head"].endswith(":") else m["head"]
        lines[i] = f"{head}{word}{m['tail']}{m['cr']}"
        return bom + "\n".join(lines)
    if on:
        return text
    cr = "\r" if "\r\n" in text else ""
    at = next((i + 1 for i, line in enumerate(lines) if _ENABLED_LINE_RE.match(line)), None)
    if at is None:
        at = 0
        for i, line in enumerate(lines):
            if _DOC_START_RE.match(line):
                at = i + 1
                break
            if line.strip() and not line.startswith(("#", "%")):
                break                   # content first: no header to step over
    if at == len(lines):                # `enabled:` is the last line, with no line end
        lines[-1] += cr
        lines.append(f"recaps: {word}")
    else:
        lines.insert(at, f"recaps: {word}{cr}")
    return bom + "\n".join(lines)


def _cmd_recaps(args, slack_factory, detector_factory) -> int:
    """`recaps [on|off]` (40 §4.2, E-W4-39): print or set the step-9 digest switch.

    Setting it edits config.yaml as text, so comments and layout survive. The edit is
    written to a temp file beside it, validated through the same load path and swapped in
    atomically, as `rules bump` does: a result that no longer loads (or does not read back
    as the wanted state), or a write that fails part way, leaves config.yaml untouched. It
    needs no Slack token and never touches the data dir.
    """
    if args.state is None:
        config = _load_config(args)
        print(f"recaps: {'on' if config.recaps else 'off'}")
        return int(Exit.OK)
    on = args.state == "on"
    path = Path(args.config)
    if not path.exists():
        raise _CliError(Exit.CONFIG_INVALID, f"config not found: {path}")
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CliError(Exit.CONFIG_INVALID, "config invalid: not UTF-8") from exc
    new_text = _recaps_rewrite(text, on)
    target = path
    tmp: Path | None = None
    try:
        if new_text != text:
            fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".yaml.tmp",
                                            dir=path.parent)
            os.close(fd)
            tmp = Path(tmp_name)
            tmp.write_bytes(new_text.encode("utf-8"))
            target = tmp
        try:
            config = load_config(target)
            if config.recaps is not on:
                raise ConfigError("recaps: the edited line did not take effect")
        except ConfigError as exc:
            raise _CliError(Exit.CONFIG_INVALID, f"config invalid: {exc}") from exc
        if tmp is not None:
            os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
    print(f"recaps: {args.state}")
    return int(Exit.OK)


def _cmd_purge(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    if not _USER_ID_RE.match(args.user):
        raise _CliError(Exit.CONFIG_INVALID, f"not a user ID: {args.user}")
    # 40 §2.1: resolve RosterEntry.is_bot as sync did, so the guard, the re-evaluate and the
    # stored fingerprints agree with the last sync. The local users cache it recorded comes
    # first (purge needs no Slack); when it does not cover the roster, users.list is asked
    # if a client is available.
    cached = _cached_is_bot(args)
    # Under players.mode auto any bot target is off-roster, so an empty cache does not
    # cover the roster even when no one is grouped (E-W4-42).
    covered = all(uid in cached for uid in config.roster.entries) and (
        config.roster.mode is not RosterMode.AUTO or bool(cached))
    if covered:
        config = _load_config(args, cached)
    elif slack_factory is not None or os.environ.get("SLACK_BOT_TOKEN", "").strip():
        config = _load_config_with_bots(args, _get_slack(config, slack_factory))
    elif cached:
        config = _load_config(args, cached)
    if args.rewrite_history and not args.yes:
        if not sys.stdin.isatty():
            print(
                "purge --rewrite-history needs confirmation; pass --yes to proceed",
                file=sys.stderr,
            )
            return int(Exit.CONFIG_INVALID)
        answer = input(f"Rewrite history to erase {args.user}? [y/N] ").strip().lower()
        if answer != "y":
            print("aborted", file=sys.stderr)
            return int(Exit.CONFIG_INVALID)
    rows = _load_rows(args)
    ledger_path, state_path = _data_paths(args)
    try:
        state = load_state(state_path)
    except MalformedLedgerError as exc:
        raise _CliError(Exit.LEDGER_MALFORMED, f"state malformed: {exc}") from exc
    # 40 §3: a purge must not silently absorb a pending config change that would re-judge
    # rows <= H; guard against the pre-purge ledger exactly as a sync would.
    try:
        fingerprint_guard(config, [parse_ts(r.ts) for r in rows], state.fingerprints)
    except FingerprintGuardError as exc:
        raise _CliError(Exit.GUARD_REFUSED, str(exc)) from exc
    u = args.user
    kept = [
        r for r in rows
        if not _row_names_user(u, r.sender, r.targets, r.first_seen_targets,
                               [e.user for e in r.target_edited_in])
    ]
    removed = len(rows) - len(kept)
    # The pruned ledger changes the derived verdicts; rewrite verdicts.jsonl so it
    # stays byte-equal to a fresh evaluate over the durable ledger (DOC-VERDICTS-FRESH).
    opted_out = set(state.opted_out)
    try:
        verdicts = evaluate(
            kept, config.rules, config.roster, opted_out,
            config.semesters, config.tz,
        )
    except NoRuleInForceError as exc:
        raise _CliError(Exit.CONFIG_INVALID, f"no rule in force: {exc}") from exc
    save_ledger(ledger_path, kept)
    save_verdicts(ledger_path.with_name("verdicts.jsonl"), verdicts)
    if state_path.exists():
        # 40 §3.3: the stored fingerprints track the written ledger, so they move with H,
        # and `fingerprints_at` names that H (E-W4-17).
        h_row = max(kept, key=lambda r: parse_ts(r.ts), default=None)
        state.fingerprints = compute_fingerprints(
            config, parse_ts(h_row.ts) if h_row is not None else None)
        state.fingerprints_at = h_row.ts if h_row is not None else None
        save_state(state_path, state)
    if not args.rewrite_history and config.persistence == Persistence.FILES:
        print(f"rows removed {removed} (live ledger only; --rewrite-history not set)")
        print("pushed local only")
        return int(Exit.OK)
    store = store_for(config, ledger_path.parent)
    day = _local_day(config)
    rewritten = 0
    if isinstance(store, GitStore) and args.rewrite_history:
        # 40 §4.2: drop the user from every earlier snapshot, then land the pruned live
        # files as the purge movement on the rewritten tip; the push below is the lease-
        # guarded force-push (a lost lease is exit 9).
        rewritten = _rewrite_data_history(store, args.user)
    try:
        result = store.commit_and_push(
            local_day=day,
            large_movement=True,
            # 20 §8.4: counts only, never the purged user's ID.
            message=_commit_message(Command.PURGE, day, True, "admin", 0, removed),
            boundary=lambda *_a, **_k: None,
        )
    except LeaseRejected:
        raise _CliError(Exit.LEASE_FAILED, "force-push lease rejected") from None
    print(f"rows removed {removed}")
    if args.rewrite_history:
        print(f"commits rewritten {rewritten}")
    else:
        print("live ledger only; earlier snapshots keep the rows (--rewrite-history not set)")
    if result.sha:
        if result.sealed_sha:
            print(f"commit {result.sealed_sha}")
        print(f"movement {result.sha}")
        print("pushed data")
    elif config.persistence == Persistence.FILES:
        print("pushed local only")
    else:
        print("no change")
    return int(Exit.OK)


def _cmd_history(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    if config.persistence == Persistence.FILES:
        # 20 §8.6: no git history exists in files mode.
        raise _CliError(
            Exit.CONFIG_INVALID, "history needs git history; unavailable under persistence: files"
        )
    # 40 §4.2 / 30 §7: --semester NAME, default as report; an unknown name exits 2.
    sem = _pick_semester(config, getattr(args, "semester", None))
    # The semesters tile the calendar for the audit log: a commit day belongs to the latest
    # semester that started on or before it, so post-season vetoes, the final digest's sync
    # and break-time commits list under the semester they follow (the earliest semester
    # also takes anything before it). Every commit shows under exactly one semester.
    ordered = sorted(config.semesters, key=lambda s: s.start_us)
    idx = next(i for i, s in enumerate(ordered) if s is sem)
    first_day = None if idx == 0 else _us_local_day(sem.start_us, config)
    last_day = (
        None if idx + 1 >= len(ordered)
        else (datetime.strptime(_us_local_day(ordered[idx + 1].start_us, config), "%Y-%m-%d")
              - timedelta(days=1)).strftime("%Y-%m-%d")
    )
    ledger_path, _ = _data_paths(args)
    store = store_for(config, ledger_path.parent)
    entries = [
        e for e in store.history()
        if (first_day is None or first_day <= e.day) and (last_day is None or e.day <= last_day)
    ]
    print(f"history ({len(entries)} commits)")
    for entry in entries[: args.limit]:
        kind = "movement" if entry.is_movement else "daily"
        # 40 §4.2: counts by reason of what moved, diffed from the committed verdicts.jsonl
        # against the parent's (never from the message text); counts only, never IDs.
        moved = _history_reason_moves(store, entry.sha)
        print(f"{entry.sha}  {entry.day}  {kind}  moved {entry.moved_pairs}: "
              f"{' '.join(moved) if moved else 'none'}")
    return int(Exit.OK)


def _history_reason_moves(store, sha: str) -> list[str]:
    """`<reason> <+/-n>` for every non-zero per-reason delta of one data-branch commit,
    in the 20 §8.4 order."""
    verdicts_at = getattr(store, "_verdicts_at", None)
    if verdicts_at is None:
        return []
    try:
        deltas = _reason_deltas(
            _baseline_reason_counts(verdicts_at(sha)),
            _baseline_reason_counts(verdicts_at(f"{sha}^")),
        )
    except ValueError as exc:
        raise _CliError(Exit.LEDGER_MALFORMED, "verdicts malformed") from exc
    return [f"{name} {deltas[name]:+d}" for name in _delta_line_names(deltas)
            if deltas.get(name, 0) != 0]


def _live_opted_out(state_path: Path) -> dict[str, int] | None:
    """The live state.json's opted_out map, or None when there is no live file to keep it
    from. A live file that fails the full state check still yields its opted_out when that
    member alone is well formed: restore is how a damaged checkout is repaired, and the
    opt-out set must survive it."""
    try:
        text = state_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return dict(load_state(state_path).opted_out)
    except MalformedLedgerError:
        pass
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    opts = obj.get("opted_out") if isinstance(obj, dict) else None
    if isinstance(opts, dict) and all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in opts.items()
    ):
        return dict(opts)
    return None


def _cmd_restore(args, slack_factory, detector_factory) -> int:
    config = _load_config(args)
    ledger_path, state_path = _data_paths(args)
    store = store_for(config, ledger_path.parent)
    # The live ledger is never read before the restore: restoring is how a malformed live
    # ledger is repaired.
    day = _local_day(config)
    # E-W4-19: restore never changes the durable opt-out set (rejoin is the only removal
    # path, 20 §5.2), so the restored state.json keeps the CURRENT opted_out.
    live_opted_out = _live_opted_out(state_path)
    # 20 §8.4: the body's deltas are relative to the cumulative sealed baseline, read
    # BEFORE the restore touches the working tree.
    base = store.baseline_verdicts(day)
    base_text = base.decode("utf-8") if base is not None else ""
    try:
        store.restore(args.from_)
    except (NotImplementedError, ValueError) as exc:
        raise _CliError(Exit.CONFIG_INVALID, f"restore: {exc}") from exc
    except GitCommandError as exc:
        # 40 §4.2: exit 2 when the commit is not on the data branch; never echo git's
        # command line or stderr.
        raise _CliError(
            Exit.CONFIG_INVALID, "restore: commit is not on the data branch history"
        ) from exc
    if live_opted_out is not None:
        try:
            restored = load_state(state_path)
        except MalformedLedgerError:
            restored = None
        if restored is not None and restored.opted_out != live_opted_out:
            save_state(state_path, dataclasses.replace(restored, opted_out=live_opted_out))
    rows = _load_rows(args)
    print(f"rows restored {len(rows)}")
    try:
        cur_text = ledger_path.with_name("verdicts.jsonl").read_text(encoding="utf-8")
    except FileNotFoundError:
        cur_text = ""
    try:
        deltas = _reason_deltas(
            _baseline_reason_counts(cur_text), _baseline_reason_counts(base_text))
        base_ts, cur_ts = _baseline_row_ts(base_text), _baseline_row_ts(cur_text)
    except ValueError as exc:
        raise _CliError(Exit.LEDGER_MALFORMED, "verdicts malformed") from exc
    try:
        result = store.commit_and_push(
            local_day=day,
            large_movement=True,
            # 20 §8.4/§8.5: a dated movement header and counts only (no source sha).
            message=_commit_message(
                Command.RESTORE, day, True, "admin",
                len(cur_ts - base_ts), len(base_ts - cur_ts), deltas,
            ),
            boundary=lambda *_a, **_k: None,
        )
    except LeaseRejected:
        raise _CliError(Exit.LEASE_FAILED, "force-push lease rejected") from None
    # 40 §4.2 restore Output: the verdict flips by reason (counts only, never IDs).
    moved = [f"{name} {deltas[name]:+d}" for name in _delta_line_names(deltas)
             if deltas.get(name, 0) != 0]
    if result.sha:
        if result.sealed_sha:
            print(f"commit {result.sealed_sha}")
        print(f"movement {result.sha}")
        print("pushed data")
        print("moved: " + (" ".join(moved) if moved else "none"))
    else:
        # The snapshot equals the branch tip: nothing was committed or pushed.
        print("no change")
    return int(Exit.OK)


def _cmd_doctor(args, slack_factory, detector_factory) -> int:
    try:
        from snipebot import doctor as doctor_mod  # type: ignore[attr-defined]
    except ImportError:
        print("doctor: not available in this build", file=sys.stderr)
        return int(Exit.UNEXPECTED)
    # Online production `doctor` (no injected factory, not --offline) must BUILD a real
    # Slack client from SLACK_BOT_TOKEN and run the §5.2 against-Slack block — exactly as
    # every other Slack-touching command does via `_get_slack` (40 §5.2 / §6.1). Only when
    # the token is genuinely absent is the factory left None, so DOC-AUTH still surfaces a
    # clean "no Slack client available" FAIL. --offline skips the Slack block entirely.
    if (
        slack_factory is None
        and not getattr(args, "offline", False)
        and os.environ.get("SLACK_BOT_TOKEN")
    ):
        # Config is loaded lazily inside the factory: doctor only invokes it once its
        # own config-parse check has passed and it reaches the Slack block, so a broken
        # config still surfaces DOC-CONFIG-PARSE rather than aborting the whole run.
        slack_factory = lambda: _doctor_slack(args)  # noqa: E731
    # 40 §2.1 (E-W4-18): an offline doctor has no users.list, so it judges rostered bots
    # with the is_bot map from the local users.json cache, the source report/export use.
    args.is_bot_cache = _cached_is_bot(args) if getattr(args, "offline", False) else None
    return int(doctor_mod.run(args, slack_factory=slack_factory))


def _doctor_slack(args: argparse.Namespace):
    """Build the SlackIO transport doctor's §5.2 block runs against, from
    SLACK_BOT_TOKEN, exactly as every other Slack-touching command does via
    `_get_slack`. `make_client` already returns a `SlackIO`; the wrap guard only
    fires for a bare Web-API client and is a no-op for the real transport, so doctor's
    protocol calls (`auth_identity`, `channel_info`, ...) always have a client to run."""
    config = _load_config(args)
    client = _get_slack(config, None)
    from snipebot.slack_io import SlackWebClient

    if isinstance(client, SlackWebClient):
        return client
    return SlackWebClient(
        client,
        bot_token=_bot_token(),
        fetch_timeout_seconds=config.faces.fetch_timeout_seconds,
        max_image_bytes=config.faces.max_image_bytes,
    )


# --- argument parser ---------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS the defaults so the subparser copies of these global flags do not
    # overwrite a value already parsed before the subcommand. `main` applies the
    # real defaults after parsing for any attribute still missing (40 §4).
    common.add_argument("--config", default=argparse.SUPPRESS, help="config file")
    common.add_argument("--data-dir", default=argparse.SUPPRESS,
                        help="ledger/verdicts/state/users dir")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="debug logging to stderr")

    parser = argparse.ArgumentParser(prog="snipebot", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, **kw):
        p = sub.add_parser(name, parents=[common], **kw)
        p.set_defaults(_handler=handler)
        return p

    p = add("sync", _cmd_sync, help="one full pass")
    p.add_argument("--no-react", action="store_true")
    p.add_argument("--no-post", action="store_true")
    p.add_argument("--reevaluate", action="store_true")

    p = add("run", _cmd_run, help="sync in a loop")
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-react", action="store_true")
    p.add_argument("--no-post", action="store_true")
    p.add_argument("--reevaluate", action="store_true")

    p = add("report", _cmd_report, help="print one table")
    p.add_argument("--by", required=True,
                   choices=["day", "person", "group", "target", "snipes", "pairs"])
    p.add_argument("--semester", default=None)

    p = add("export", _cmd_export, help="write CSVs + one XLSX")
    p.add_argument("--semester", default=None)
    p.add_argument("--out", default="exports")

    add("roster", _cmd_roster, help="print channel members")

    p = add("backfill", _cmd_backfill, help="re-scan from an explicit lower bound")
    p.add_argument("--from", dest="from_", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-react", action="store_true", default=True)
    p.add_argument("--react", dest="no_react", action="store_false")
    p.add_argument("--no-post", action="store_true")

    p = add("veto", _cmd_veto, help="add a durable CLI veto")
    p.add_argument("--ts", required=True)
    p.add_argument("--by", default=None)
    p.add_argument("--no-react", action="store_true")
    p.add_argument("--no-post", action="store_true")

    p = add("unveto", _cmd_unveto, help="remove a CLI veto")
    p.add_argument("--ts", required=True)
    p.add_argument("--by", default=None)
    p.add_argument("--no-react", action="store_true")
    p.add_argument("--no-post", action="store_true")

    p = add("selfie", _cmd_selfie, help="set/replace a durable selfie override")
    p.add_argument("--ts", required=True)
    p.add_argument("--no", action="store_true", help="record as NOT a selfie")
    p.add_argument("--by", default=None)
    p.add_argument("--no-react", action="store_true")
    p.add_argument("--no-post", action="store_true")

    p = add("rejoin", _cmd_rejoin, help="remove a user from the opted-out set")
    p.add_argument("user")
    p.add_argument("--no-react", action="store_true")
    p.add_argument("--no-post", action="store_true")

    p = add("purge", _cmd_purge, help="erase a user (history rewrite)")
    p.add_argument("--user", required=True)
    p.add_argument("--rewrite-history", action="store_true")
    p.add_argument("--yes", action="store_true")

    p = add("accept-deletes", _cmd_accept_deletes, help="release the delete breaker once")
    p.add_argument("--count", required=True, type=int)
    p.add_argument("--no-react", action="store_true")
    p.add_argument("--no-post", action="store_true")

    rules = add("rules", None, help="rules admin")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    bump = rules_sub.add_parser("bump", parents=[common])
    bump.add_argument("--effective-from", dest="effective_from", required=True)
    bump.set_defaults(_handler=_cmd_rules_bump)

    p = add("recaps", _cmd_recaps, help="print or set the digest switch")
    p.add_argument("state", nargs="?", choices=["on", "off"], default=None)

    review_p = add("review", None, help="local photo review: ambiguous face reads, yes/no")
    review_sub = review_p.add_subparsers(dest="review_command", required=True)
    for name, text in (("open", "scan, then open the review gallery"),
                       ("list", "scan, then print the queue"),
                       ("stats", "how the labels fell, per reason and threshold"),
                       ("label", "record one label")):
        rp = review_sub.add_parser(name, parents=[common], help=text)
        rp.set_defaults(_handler=_cmd_review)
        rp.add_argument("--dir", default="review", help="local review folder")
        if name in ("open", "list", "stats"):
            rp.add_argument("--semester", default=None)
        if name in ("open", "list"):
            rp.add_argument("--all", action="store_true", help="every tagged photo post")
        if name == "open":
            rp.add_argument("--no-browser", action="store_true")
        if name == "label":
            rp.add_argument("--ts", required=True)
            rp.add_argument("label", choices=["yes", "no", "clear"])

    p = add("history", _cmd_history, help="print per-commit verdict deltas")
    p.add_argument("--semester", default=None)
    p.add_argument("--limit", type=int, default=50)

    p = add("restore", _cmd_restore, help="bring a snapshot back")
    p.add_argument("--from", dest="from_", required=True)

    p = add("doctor", _cmd_doctor, help="preflight checks")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--json", action="store_true")

    return parser


def main(argv=None, *, slack_factory=None, detector_factory=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # The global flags use default=argparse.SUPPRESS so a value given before the
    # subcommand is not clobbered by the subparser's copy; fill the real defaults
    # for any that were never supplied (40 §4).
    args.config = getattr(args, "config", "config.yaml")
    args.data_dir = getattr(args, "data_dir", "data")
    args.verbose = getattr(args, "verbose", False)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # -v raises this program's logger only. Third-party loggers stay where they
    # are: slack_sdk logs every request and response at DEBUG, headers and
    # query parameters included, and a verbose run must still carry IDs only
    # (40 §4).
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.error("no command")
    # A redirected stdout on Windows is cp1252 with strict errors; a display name outside
    # it must not crash `roster`/`report` (40 §4.2). UTF-8 matches config.yaml, where the
    # roster output is pasted.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass
    try:
        return int(handler(args, slack_factory, detector_factory))
    except _CliError as exc:
        print(" ".join(exc.message.split()), file=sys.stderr)
        return int(exc.code)
    except Exception as exc:  # noqa: BLE001 - the process boundary: never a traceback on stderr
        logger.debug("unexpected error", exc_info=True)
        # 40 §4.4: one line on stderr, whatever the exception carries (the full detail
        # reaches the -v traceback above).
        detail = " ".join(str(exc).split())
        print(f"unexpected error: {type(exc).__name__}: {detail}", file=sys.stderr)
        return int(Exit.UNEXPECTED)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
