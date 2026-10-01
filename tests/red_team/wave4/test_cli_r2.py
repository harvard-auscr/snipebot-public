"""Wave 4, round 2 breaker tests for the `cli` surface (40-config-cli.md section 4).

Every test here fails against the current code for the reason its docstring names. The
Slack world is a `FakeSlack`; git runs only against a bare origin plus a clone under
`tmp_path`.
"""

from __future__ import annotations

import calendar
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.config import load_config
from snipebot.faces import FakeFaceDetector
from snipebot.ts import US_PER_SECOND, parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
TARGET3 = "U0AAA003"
TARGET4 = "U0AAA004"
ADMIN = "U0AAA009"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND


def _config_dict(*, persistence: str = "files", timezone: str = "UTC",
                 semesters: list | None = None, extras: list | None = None) -> dict:
    return {
        "enabled": True,
        "persistence": persistence,
        "slack": {"channel": CHANNEL},
        "timezone": timezone,
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": semesters if semesters is not None else [
            {"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}],
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
                    "groups": {"sib": [SNIPER, TARGET]},
                    "extras": extras if extras is not None else []},
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


def _users() -> dict:
    return {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2"),
        TARGET3: FakeUser(id=TARGET3, display_name="user-3"),
        TARGET4: FakeUser(id=TARGET4, display_name="user-4"),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9"),
    }


def _world(posts: list[tuple[str, str]], *, now: str = NOW_TS) -> FakeSlack:
    """A channel holding one photo snipe per (ts, target) pair, all sent by SNIPER."""
    slack = FakeSlack(now=now, bot_user_id=BOT, users=_users())
    for i, (ts, target) in enumerate(posts, start=1):
        slack.post(at=ts, user=SNIPER, channel=CHANNEL, text=f"<@{target}>",
                   files=[image_file(f"F0FILE{i:03d}", b"snap%d" % i,
                                     name=f"photo-{i}.png")])
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


def _git_rig(tmp_path: Path, monkeypatch, **cfg_kw) -> tuple[Path, Path, Path]:
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
    cfg = _write_config(tmp_path / "config.yaml", persistence="git", **cfg_kw)
    rc = _sync(cfg, repo / "data", _world([(_ts(2026, 9, 18, 10), TARGET)]))
    assert rc == 0
    assert TARGET in _git(origin, "show", "data:data/ledger.jsonl")
    return origin, repo, cfg


# --------------------------------------------------------------------------- findings

def test_purge_without_rewrite_history_under_git_is_a_silent_no_op(tmp_path, monkeypatch):
    """40 §4.1: `purge` Writes = yes, and a write under `persistence: git` means a commit
    (20 §8), a large movement that prints its commit; 40 §7.2 also says a CLI input must
    never end in a silent no-op. Under git, `purge --user U` without `--rewrite-history`
    prunes the working-tree ledger, prints `rows removed 1`, exits 0 and commits nothing.
    The pushed data branch still holds the user's rows, and the next sync's refresh
    (`checkout -f`) silently throws the pruned file away. It should either commit the
    pruned live ledger or refuse with a non-zero exit."""
    origin, repo, cfg = _git_rig(tmp_path, monkeypatch)
    rc = main(["purge", "--user", TARGET, *_argv(cfg, repo / "data")])
    pushed = _git(origin, "show", "data:data/ledger.jsonl")
    assert rc != int(Exit.OK) or TARGET not in pushed


def test_purge_leaves_stale_fingerprints_so_the_next_sync_is_refused(tmp_path, capsys):
    """40 §3 / §3.3: the guard refuses only on a config change that would re-judge rows
    <= H, and every write keeps `state.json` fingerprints equal to the values computed
    over the ledger it writes. `purge` rewrites ledger.jsonl and verdicts.jsonl but
    leaves the stored fingerprints computed at the OLD H. When the purged row was the
    newest one, H moves back past a dated roster addition (`from:` between the new and
    the old H), so the next plain `sync` with an unchanged config exits 3
    (GUARD_REFUSED) and keeps failing until an admin runs `--reevaluate`."""
    cfg = _write_config(tmp_path / "config.yaml",
                        extras=[TARGET3, {"id": TARGET4, "from": "2026-09-18 10:30"}])
    data = tmp_path / "data"
    data.mkdir()
    keep_ts = _ts(2026, 9, 18, 10)
    purge_ts = _ts(2026, 9, 18, 11)
    slack = _world([(keep_ts, TARGET3), (purge_ts, TARGET)])
    assert _sync(cfg, data, slack) == 0

    rc = main(["purge", "--user", TARGET, *_argv(cfg, data)])
    assert rc == int(Exit.OK)
    # The erasure request also removed the message from the channel.
    slack.delete_message(at=_ts(2026, 9, 18, 11, 30), ts=purge_ts, channel=CHANNEL)
    capsys.readouterr()

    rc = _sync(cfg, data, slack)
    err = capsys.readouterr().err
    assert rc == int(Exit.OK), f"sync after purge exited {rc}: {err}"


def test_history_semester_flag_is_ignored(tmp_path, monkeypatch, capsys):
    """40 §4.2 `history`: `--semester NAME (default as report)`, the shared semester
    default of `report`, `export` and `history` (30 §7), and 40 §4.4 maps an unknown
    semester to exit 2. `_cmd_history` never reads `args.semester`: `history --semester
    spring-2026` lists the fall commit, and a semester name that does not exist is
    accepted with exit 0."""
    semesters = [
        {"name": "spring-2026", "start": "2026-01-10", "end": "2026-05-20"},
        {"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"},
    ]
    origin, repo, cfg = _git_rig(tmp_path, monkeypatch, semesters=semesters)
    fall_sha = _git(origin, "rev-parse", "data").strip()
    capsys.readouterr()

    rc = main(["history", "--semester", "spring-2026", *_argv(cfg, repo / "data")])
    out = capsys.readouterr().out
    assert rc == int(Exit.OK)
    assert fall_sha not in out, "a fall-2026 commit is listed under --semester spring-2026"

    rc = main(["history", "--semester", "no-such-semester", *_argv(cfg, repo / "data")])
    assert rc == int(Exit.CONFIG_INVALID)


def test_restore_never_prints_the_sealed_restore_point(tmp_path, monkeypatch, capsys):
    """40 §4.3: every writing command prints a final block, and "a large movement prints
    both `commit` (the restore point sealed before it) and `movement`"; `restore` is a
    large movement (40 §4.1, 20 §8.5). `_cmd_restore` gets `CommitResult.sealed_sha` back
    from `commit_and_push` but prints only `rows restored`, `movement <sha>` and `pushed
    data`, so an admin without repo access cannot see the restore point that undoes it."""
    origin, repo, cfg = _git_rig(tmp_path, monkeypatch)
    older = _git(origin, "rev-parse", "data").strip()
    # Restoring the tip itself is `no change` (E-W4-20f): land a newer, different commit
    # first (on the next local day, so it is not an amend of the older one) and restore
    # the older one; the sealed restore point is that newer tip.
    monkeypatch.setattr(cli, "_now_us", lambda: _secs(2026, 9, 19, 12) * US_PER_SECOND)
    assert _sync(cfg, repo / "data", _world([(_ts(2026, 9, 18, 11), TARGET)])) == 0
    tip = _git(origin, "rev-parse", "data").strip()
    assert tip != older
    capsys.readouterr()
    rc = main(["restore", "--from", older, *_argv(cfg, repo / "data")])
    out = capsys.readouterr().out
    assert rc == int(Exit.OK)
    assert f"commit {tip}" in out, out


def test_rules_bump_now_in_the_repeated_hour_is_refused(tmp_path, monkeypatch, capsys):
    """40 §4.2 `rules bump`: exit 2 only if `--effective-from` is not strictly later than
    H as an exact instant; `now` always is. In the repeated fall-back hour
    (America/New_York, 2026-11-01), `now` is the second 01:30 (EST), but the CLI rounds
    the wall clock and resolves it with fold=0, i.e. one hour EARLIER (EDT). A row posted
    during the first 01:xx hour is therefore "later" than the value checked, and the
    bump is refused with 'must be strictly later than the newest ledger row' although
    the admin asked for now, which is after every row. A correct bump writes a minute
    that resolves after now (for example 02:00)."""
    semesters = [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}]
    cfg = _write_config(tmp_path / "config.yaml", timezone="America/New_York",
                        semesters=semesters)
    data = tmp_path / "data"
    data.mkdir()
    # 01:40 EDT (first pass) = 05:40 UTC; "now" = 01:30:20 EST (second pass) = 06:30:20 UTC.
    row_ts = _ts(2026, 11, 1, 5, 40)
    now_secs = _secs(2026, 11, 1, 6, 30, 20)
    monkeypatch.setattr(cli, "_now_us", lambda: now_secs * US_PER_SECOND)
    slack = _world([(row_ts, TARGET)], now=f"{now_secs}.000000")
    assert _sync(cfg, data, slack) == 0
    h_us = parse_ts(row_ts)

    from zoneinfo import ZoneInfo

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(now_secs, ZoneInfo("America/New_York"))

    monkeypatch.setattr(cli, "datetime", _FrozenDateTime)
    capsys.readouterr()
    rc = main(["rules", "bump", "--effective-from", "now", *_argv(cfg, data)])
    err = capsys.readouterr().err
    assert rc == int(Exit.OK), err
    assert load_config(cfg).rules.entries[-1].effective_from_us > h_us


