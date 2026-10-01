"""Privacy of what reaches the git-backed `data` branch (commit messages, committed blobs,
and the rewritten history `purge` promises).

Each test builds a bare `origin` whose `data` branch has one seed commit, clones it as the
data checkout (`SNIPEBOT_DATA_REPO`), and drives the CLI end to end against a `FakeSlack`
world under `persistence: git`. Real git runs only inside `tmp_path`.
"""

from __future__ import annotations

import calendar
import subprocess
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.faces import FakeFaceDetector
from snipebot.ts import US_PER_SECOND

from tests.fake_slack import FakeSlack, FakeUser

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
ADMIN = "U0AAA009"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
SNIPER_NAME = "user-1"
TARGET_NAME = "user-2"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.000000"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 10)


# --------------------------------------------------------------------------- helpers

def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """(origin, checkout): a bare origin with a seeded `data` branch and its clone."""
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
    return origin, repo


def _config(tmp_path: Path) -> Path:
    doc = {
        "enabled": True,
        "persistence": "git",
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
                    "groups": {"fam": [SNIPER, TARGET]}, "extras": []},
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
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _photo(file_id: str, data: bytes) -> dict:
    return {
        "id": file_id,
        "mimetype": "image/jpeg",
        "name": "photo-1.jpg",
        "title": "photo-1",
        "size": len(data),
        "original_w": 100,
        "original_h": 100,
        "thumb_1024": f"https://fixture.invalid/{file_id}/thumb_1024",
        "url_private": f"https://fixture.invalid/{file_id}/photo-1.jpg",
        "url_private_download": f"https://fixture.invalid/{file_id}/download/photo-1.jpg",
        "permalink": f"https://fixture.invalid/archives/{file_id}",
        "_bytes": data,
    }


def _world() -> FakeSlack:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name=SNIPER_NAME, real_name=SNIPER_NAME),
        TARGET: FakeUser(id=TARGET, display_name=TARGET_NAME, real_name=TARGET_NAME),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9", real_name="user-9"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[_photo("F0FILE001", b"snap-bytes")])
    return slack


@pytest.fixture()
def git_world(tmp_path, monkeypatch):
    origin, repo = _make_repo(tmp_path)
    monkeypatch.setenv("SNIPEBOT_DATA_REPO", str(repo))
    monkeypatch.delenv("SNIPEBOT_CRASH_AT", raising=False)
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _config(tmp_path)
    data = repo / "data"
    slack = _world()
    argv = ["--config", str(cfg), "--data-dir", str(data)]

    def cli_run(*args: str) -> int:
        return main([*args, *argv], slack_factory=lambda: slack,
                    detector_factory=lambda: FakeFaceDetector({}))

    rc = cli_run("sync", "--no-react", "--no-post")
    assert rc == int(Exit.OK)
    # sanity: the synced snipe (sender SNIPER) is on the pushed data branch
    assert SNIPER in _git(origin, "show", "data:data/ledger.jsonl")
    cli_run.slack = slack  # lets a test author a later snipe before a second sync
    return origin, repo, cli_run


def _all_commits(origin: Path) -> list[str]:
    return _git(origin, "rev-list", "data").split()


# --------------------------------------------------------------------------- findings

def test_purge_commit_message_carries_the_purged_user_id(git_world, capsys):
    """20-sync-ledger.md §8.4: a data-branch commit message carries "**Only counts** -- never
    a user ID, display name, message ts ..." (history is readable by everyone with repo
    access); persistence.py's own contract says the same. `_cmd_purge` commits with
    `message=f"purge {args.user}\\n\\nrows -{removed}\\n"`, so the erased user's ID is written
    into the permanent, force-pushed data-branch history by the very command whose job is
    genuine erasure (40 §4.2 purge)."""
    origin, _repo, cli_run = git_world
    rc = cli_run("purge", "--user", SNIPER, "--rewrite-history", "--yes")
    assert rc == int(Exit.OK)
    messages = _git(origin, "log", "data", "--format=%B")
    assert SNIPER not in messages


def test_purge_rewrite_history_leaves_the_user_in_earlier_snapshots(git_world, capsys):
    """40-config-cli.md §4.2 `purge` Effect: "removes every row where the user is sender or
    target from the live ledger **and rewrites the `data` branch history to drop them from
    earlier daily snapshots**, then force-pushes" (plan §6 genuine erasure). `_cmd_purge
    --rewrite-history` only appends one new commit on top of the tip; every earlier commit on
    the pushed `data` branch still holds the purged sender's rows in `data/ledger.jsonl` and
    `data/verdicts.jsonl`, so the user is not erased."""
    origin, _repo, cli_run = git_world
    rc = cli_run("purge", "--user", SNIPER, "--rewrite-history", "--yes")
    assert rc == int(Exit.OK)
    leaked = []
    for sha in _all_commits(origin):
        for path in ("data/ledger.jsonl", "data/verdicts.jsonl"):
            proc = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=str(origin),
                                  capture_output=True, text=True)
            if proc.returncode == 0 and SNIPER in proc.stdout:
                leaked.append((sha[:8], path))
    assert leaked == []


def test_restore_commits_the_local_users_json_name_cache(git_world, capsys):
    """PLAN §2 storage: "`users.json` -- id to display name cache. Local only, never
    committed"; 20 §8.4 / plan §6: no display name is ever written to git. With the default
    layout (`--data-dir ./data` inside `SNIPEBOT_DATA_REPO`), `roster` writes
    `data/users.json` (display names) into the data checkout, and `restore` then commits via
    `GitStore.commit_and_push`, whose `git add -A` stages every untracked file -- so the
    display-name cache is committed and pushed to the `data` branch."""
    origin, _repo, cli_run = git_world
    synced = _git(origin, "rev-parse", "data").strip()
    assert cli_run("roster") == int(Exit.OK)
    capsys.readouterr()
    rc = cli_run("restore", "--from", synced)
    assert rc == int(Exit.OK)
    tracked = _git(origin, "ls-tree", "-r", "--name-only", "data").split()
    assert "data/users.json" not in tracked
    grep = subprocess.run(["git", "grep", "-l", TARGET_NAME, "data"], cwd=str(origin),
                          capture_output=True, text=True)
    assert grep.stdout.strip() == ""


def test_restore_commit_message_breaks_the_8_4_format_and_vanishes_from_history(
        git_world, capsys, monkeypatch):
    """20-sync-ledger.md §8.4: every data-branch commit message is
    `<command> <YYYY-MM-DD>[ [movement:<trigger>]]` + count lines, and `restore` is a large
    movement (§8.5) so it is marked `[movement:admin]`; 40 §4.2 `history` prints one line per
    daily/movement commit. `_cmd_restore` commits with `f"restore {args.from_}..."` (a SHA
    where the day belongs, no movement marker, no delta lines), so `_parse_header` rejects it
    and `snipebot history` silently omits the restore from the audit trail."""
    origin, _repo, cli_run = git_world
    synced = _git(origin, "rev-parse", "data").strip()
    # Restoring the tip itself is `no change` (E-W4-20f): land a newer, different sync
    # commit first (on the next local day, so it is not an amend of the older one) and
    # restore the older one.
    monkeypatch.setattr(cli, "_now_us", lambda: _secs(2026, 9, 19, 12) * US_PER_SECOND)
    cli_run.slack.post(at=_ts(2026, 9, 18, 11), user=SNIPER, channel=CHANNEL,
                       text=f"<@{TARGET}>", files=[_photo("F0FILE002", b"snap-bytes-2")])
    assert cli_run("sync", "--no-react", "--no-post") == int(Exit.OK)
    assert _git(origin, "rev-parse", "data").strip() != synced
    assert cli_run("restore", "--from", synced) == int(Exit.OK)
    capsys.readouterr()
    first_line = _git(origin, "log", "-1", "data", "--format=%s").strip()
    assert cli_run("history") == int(Exit.OK)
    out = capsys.readouterr().out
    assert first_line.startswith("restore 2026-09-19")
    assert "[movement:" in first_line
    assert "history (3 commits)" in out
