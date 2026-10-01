"""Privacy and consent of what is stored (round 3): the durable opt-out set across an admin
`restore`.

Git tests build a bare `origin` whose `data` branch has one seed commit, clone it as the
data checkout (`SNIPEBOT_DATA_REPO`), and drive the CLI end to end against a `FakeSlack`
world under `persistence: git`. Real git runs only inside `tmp_path`.
"""

from __future__ import annotations

import calendar
import json
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
OTHER = "U0AAA003"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.000000"


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


def _config(tmp_path: Path, optout_ts: str) -> Path:
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
                    "groups": {"fam": [SNIPER, TARGET, OTHER]}, "extras": []},
        "consent": {
            "veto": {"emoji": "no_entry_sign", "by": ["admins"]},
            "optout_messages": [optout_ts],
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
        "url_private_download": f"https://fixture.invalid/{file_id}/download/photo-1.jpg",
        "_bytes": data,
    }


def _world(now: str) -> FakeSlack:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1", real_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2", real_name="user-2"),
        OTHER: FakeUser(id=OTHER, display_name="user-3", real_name="user-3"),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9", real_name="user-9"),
    }
    return FakeSlack(now=now, bot_user_id=BOT, users=users)


def _opted_out(repo: Path) -> set[str]:
    state = json.loads((repo / "data" / "state.json").read_text(encoding="utf-8"))
    return set(state["opted_out"])


# --------------------------------------------------------------------------- findings

def test_restore_rolls_back_the_durable_optout_set(tmp_path, monkeypatch, capsys):
    """20-sync-ledger.md §5.2: the opted-out set is durable and monotonic; "The **only**
    removal path is the admin command `snipebot rejoin U…`". PLAN.md §11 invariants:
    "Opt-out is monotonic: once observed, ... never brings the person back", and "Opt-out
    closure: no opted-out ID appears in any table, export, digest". `_cmd_restore` checks
    out the snapshot's `data/state.json` wholesale (GitStore.restore over `_DATA_FILES`), so
    restoring any commit sealed before a person's opt-out silently removes them from
    `state.opted_out`. The next sync cannot re-observe the opt-out: `_observe_optouts`
    reads the pinned opt-out message only when it falls in the run's fetch range, and a
    pinned message is normally older than the scan window. The person is back in every
    standings digest, report and export with no `rejoin` ever run."""
    _origin, repo = _make_repo(tmp_path)
    monkeypatch.setenv("SNIPEBOT_DATA_REPO", str(repo))
    monkeypatch.delenv("SNIPEBOT_CRASH_AT", raising=False)
    clock = {"now": 0}
    monkeypatch.setattr(cli, "_now_us", lambda: clock["now"])

    optout_ts = _ts(2026, 9, 2, 9)
    cfg = _config(tmp_path, optout_ts)
    slack = _world(_ts(2026, 9, 3, 12))
    slack.post(at=optout_ts, user=ADMIN, channel=CHANNEL, text="react here to opt out")
    slack.post(at=_ts(2026, 9, 3, 9), user=SNIPER, channel=CHANNEL, text=f"<@{OTHER}>",
               files=[_photo("F0FILE001", b"snap-1")])
    argv = ["--config", str(cfg), "--data-dir", str(repo / "data")]

    def at(y, mo, d, h) -> None:
        slack.as_of(_ts(y, mo, d, h))
        clock["now"] = _secs(y, mo, d, h) * US_PER_SECOND

    def cli_run(*args: str) -> int:
        return main([*args, *argv], slack_factory=lambda: slack,
                    detector_factory=lambda: FakeFaceDetector({}))

    at(2026, 9, 3, 12)
    assert cli_run("sync", "--no-react", "--no-post") == int(Exit.OK)
    before_optout = _git(repo, "rev-parse", "HEAD").strip()
    assert OTHER not in _opted_out(repo)

    # OTHER opts out by reacting to the pinned opt-out message.
    slack.react(at=_ts(2026, 9, 4, 10), ts=optout_ts, channel=CHANNEL, user=OTHER,
                name="wave")
    at(2026, 9, 4, 12)
    assert cli_run("sync", "--no-react", "--no-post") == int(Exit.OK)
    assert OTHER in _opted_out(repo)                 # sanity: the opt-out was observed

    # An admin restores the earlier snapshot (e.g. to undo an unrelated bad movement).
    at(2026, 9, 5, 12)
    assert cli_run("restore", "--from", before_optout) == int(Exit.OK)
    capsys.readouterr()
    assert OTHER in _opted_out(repo), (
        "restore removed an observed opt-out; only `rejoin` may do that"
    )
