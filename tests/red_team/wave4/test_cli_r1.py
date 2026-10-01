"""Wave 4, round 1 breaker tests for the `cli` surface (40-config-cli.md section 4).

Every test here fails against the current code for the reason its docstring names. The
Slack world is a `FakeSlack`; git runs only against a bare origin plus a clone under
`tmp_path`.
"""

from __future__ import annotations

import calendar
import io
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.config import load_config
from snipebot.faces import FakeFaceDetector
from snipebot.persistence import _parse_header
from snipebot.ts import US_PER_SECOND, parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
ADMIN = "U0AAA009"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND


def _config_dict(*, persistence: str = "files") -> dict:
    return {
        "enabled": True,
        "persistence": persistence,
        "slack": {"channel": CHANNEL},
        "timezone": "UTC",
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}],
        "rules": {
            "cooldown": {"minutes": 15, "scope": "pair", "rejected_attempts_reset": False},
            "multi_tag": "per_target",
            "max_targets_per_message": None,
            "edit_grace_minutes": 10,
            "max_snipes_per_target_per_day": None,
            "allow_self": False,
            "allow_bots": False,
            "count_thread_replies": False,
            "count_image_links": False,
            "allow_video": False,
            "selfie_bonus": False,
        },
        "players": {"count_intra_group": True,
                    "groups": {"sib": [SNIPER, TARGET]}, "extras": []},
        "consent": {
            "veto": {"emoji": "no_entry_sign", "by": ["admins"]},
            "optout_messages": [],
            "opted_out": [],
        },
        "admins": [ADMIN],
        "feedback": {
            "reactions": {
                "counted": "white_check_mark",
                "cooldown": "hourglass_flowing_sand",
                "untagged": None,
                "not_counted": "x",
                "selfie": None,
            },
            "review": {"min_targets": 5, "emoji": "question"},
        },
        "reports": [],
    }


def _write_config(path: Path, **kw) -> Path:
    path.write_text(yaml.safe_dump(_config_dict(**kw), sort_keys=False), encoding="utf-8")
    return path


def _world(msg_ts: str | None, *, target_name: str = "user-2") -> FakeSlack:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name=target_name),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    if msg_ts is not None:
        slack.post(at=msg_ts, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
                   files=[image_file("F0FILE001", b"snap", name="photo-1.png")])
    return slack


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)


def _argv(cfg: Path, data: Path) -> list[str]:
    return ["--config", str(cfg), "--data-dir", str(data)]


def _sync(cfg: Path, data: Path, slack: FakeSlack) -> int:
    return main(["sync", "--no-react", "--no-post", *_argv(cfg, data)],
                slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({}))


# --------------------------------------------------------------------------- git rig

def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _git_rig(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """(origin, repo, config) with one synced snipe of TARGET committed and pushed."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "data", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", str(origin), str(seed)])
    (seed / "data").mkdir(exist_ok=True)
    (seed / "data" / "README").write_text("data branch\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=seed", "-c", "user.email=seed@fixture.invalid",
         "commit", "-m", "init data branch")
    _git(seed, "branch", "-M", "data")
    _git(seed, "push", "-u", "origin", "data")
    repo = tmp_path / "repo"
    _run(["git", "clone", str(origin), str(repo)])
    _git(repo, "checkout", "data")
    monkeypatch.setenv("SNIPEBOT_DATA_REPO", str(repo))
    monkeypatch.setenv("SNIPEBOT_GIT_AUTHOR", "snipebot <snipebot@fixture.invalid>")
    cfg = _write_config(tmp_path / "config.yaml", persistence="git")
    rc = _sync(cfg, repo / "data", _world(_ts(2026, 9, 18, 10)))
    assert rc == 0
    assert TARGET in _git(origin, "show", "data:data/ledger.jsonl")
    return origin, repo, cfg


# --------------------------------------------------------------------------- findings

def test_purge_commit_message_never_names_the_purged_user(tmp_path, monkeypatch):
    """20 §8.4 "Commit message format (counts by reason; never IDs or names)": only counts
    ever reach a commit message, never a user ID. `purge --rewrite-history --yes` commits
    with the message `purge <UserID>`, so the erasure command itself writes the erased
    person's ID into the data branch history that everyone with repo access can read."""
    origin, repo, cfg = _git_rig(tmp_path, monkeypatch)
    rc = main(["purge", "--user", TARGET, "--rewrite-history", "--yes",
               *_argv(cfg, repo / "data")])
    assert rc == 0
    messages = _git(origin, "log", "data", "--format=%B")
    assert TARGET not in messages


def test_purge_rewrite_history_drops_user_from_earlier_snapshots(tmp_path, monkeypatch):
    """40 §4.2 `purge` Effect: removes every row where the user is sender or target from
    the live ledger AND rewrites the `data` branch history to drop them from earlier daily
    snapshots, then force-pushes (Output: rows removed, commits rewritten, the new tip SHA).
    The CLI only appends one ordinary commit on top, so every earlier commit on the pushed
    `data` branch still carries the purged user's rows in ledger/verdicts."""
    origin, repo, cfg = _git_rig(tmp_path, monkeypatch)
    rc = main(["purge", "--user", TARGET, "--rewrite-history", "--yes",
               *_argv(cfg, repo / "data")])
    assert rc == 0
    leaks = []
    for sha in _git(origin, "rev-list", "data").split():
        for path in ("data/ledger.jsonl", "data/verdicts.jsonl", "data/state.json"):
            proc = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=str(origin),
                                  capture_output=True, text=True)
            if proc.returncode == 0 and TARGET in proc.stdout:
                leaks.append((sha[:8], path))
    assert leaks == []


def test_roster_redirected_to_cp1252_stdout_does_not_crash(tmp_path, monkeypatch):
    """40 §4.2 `roster`: prints `id  display name` lines "for pasting into `players`" (so
    it is routinely redirected to a file) and refreshes the `users.json` cache; Exit 0.
    On Windows a redirected stdout is cp1252 with strict errors: a display name holding an
    emoji raises UnicodeEncodeError inside `print`, the run exits 1 ("unexpected error")
    and, because the print precedes the cache write, `users.json` is never refreshed.
    The same crash hits `report`, which prints display names too."""
    cfg = _write_config(tmp_path / "config.yaml")
    data = tmp_path / "data"
    slack = _world(None, target_name="user-2 \U0001F4F8")
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252", errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stdout", out)
    rc = main(["roster", *_argv(cfg, data)], slack_factory=lambda: slack)
    out.flush()
    monkeypatch.undo()
    assert rc == int(Exit.OK)
    assert TARGET in buf.getvalue().decode("cp1252", errors="replace")
    assert (data / "users.json").exists()


def test_rules_bump_now_is_not_dated_before_the_newest_row(tmp_path, monkeypatch):
    """40 §4.2 `rules bump`: the new entry must fall strictly after `H` (the newest ledger
    row) as an exact instant, else exit 2 — a new entry dated before `H` re-judges history.
    With `--effective-from now` the CLI checks the exact current instant against `H` but
    writes `now` truncated to the minute ("%Y-%m-%d %H:%M"), so a row posted earlier in the
    current minute lands AFTER the written effective_from: the check passes, exit 0, and
    config.yaml now carries an entry in force before `H`."""
    cfg = _write_config(tmp_path / "config.yaml")
    data = tmp_path / "data"
    data.mkdir()
    row_ts = _ts(2026, 9, 18, 10, 0, 30)
    assert _sync(cfg, data, _world(row_ts)) == 0
    h_us = parse_ts(row_ts)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 18, 10, 0, 45, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", _FrozenDateTime)
    rc = main(["rules", "bump", "--effective-from", "now", *_argv(cfg, data)])
    newest_entry_us = load_config(cfg).rules.entries[-1].effective_from_us
    assert rc == int(Exit.CONFIG_INVALID) or newest_entry_us > h_us


def test_restore_unknown_commit_exits_config_invalid(tmp_path, monkeypatch):
    """40 §4.2 `restore`: "Exit `0`; `2` if the commit is not on the `data` branch
    history". A commit id that does not exist surfaces as a GitCommandError from
    `git checkout` and is mapped to exit 1 ("unexpected error") instead of 2."""
    origin, repo, cfg = _git_rig(tmp_path, monkeypatch)
    rc = main(["restore", "--from", "0123456789abcdef0123456789abcdef01234567",
               *_argv(cfg, repo / "data")])
    assert rc == int(Exit.CONFIG_INVALID)


def test_restore_commit_carries_the_dated_movement_header(tmp_path, monkeypatch):
    """20 §8.4/§8.5: every commit's first line is `<command> <YYYY-MM-DD>[ [movement:...]]`
    and `restore` is a large movement with "its own dated commit line". The CLI commits
    `restore <commit>` (no day, no movement marker), so the commit parser does not
    recognise it: `history` silently skips the restore and it is not treated as sealed."""
    origin, repo, cfg = _git_rig(tmp_path, monkeypatch)
    older = _git(origin, "rev-parse", "data").strip()
    # Restoring the tip itself is `no change` (E-W4-20f): land a newer, different commit
    # first (on the next local day, so it is not an amend of the older one) and restore
    # the older one.
    monkeypatch.setattr(cli, "_now_us", lambda: _secs(2026, 9, 19, 12) * US_PER_SECOND)
    assert _sync(cfg, repo / "data", _world(_ts(2026, 9, 18, 11))) == 0
    assert _git(origin, "rev-parse", "data").strip() != older
    rc = main(["restore", "--from", older, *_argv(cfg, repo / "data")])
    assert rc == 0
    subject = _git(origin, "log", "-1", "data", "--format=%B")
    header = _parse_header(subject)
    assert header is not None and header[1] is True


def test_history_under_files_persistence_exits_config_invalid(tmp_path, capsys):
    """20 §8.6: "`snipebot history` and `restore` need git history that does not exist
    here and exit 2 with a message naming `persistence: files`". `history` under
    `persistence: files` prints `history (0 commits)` and exits 0 instead."""
    cfg = _write_config(tmp_path / "config.yaml")
    data = tmp_path / "data"
    data.mkdir()
    rc = main(["history", *_argv(cfg, data)])
    assert rc == int(Exit.CONFIG_INVALID)
    assert "persistence: files" in capsys.readouterr().err


def test_unexpected_error_is_a_single_stderr_line(tmp_path, monkeypatch, capsys):
    """40 §4.4: `main()` maps any uncaught exception to exit 1 "with a single one-line
    message on stderr ... a crash reads as one line like every other failure". The CLI
    prints `str(exc)` verbatim, so an exception with a multi-line message (a git
    subprocess error carrying git's stderr, an SDK error carrying a response dump)
    spills several lines onto stderr."""
    cfg = _write_config(tmp_path / "config.yaml")
    data = tmp_path / "data"
    slack = _world(_ts(2026, 9, 18, 10))

    def _boom(*_a, **_k):
        raise RuntimeError("first line of detail\nsecond line of detail")

    monkeypatch.setattr(slack, "history", _boom)
    rc = _sync(cfg, data, slack)
    assert rc == int(Exit.UNEXPECTED)
    err_lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(err_lines) == 1, err_lines
