"""Wave 4, surface "time-periods", round 3.

Day and semester boundaries at the CLI after the round-2 repairs. Offline: FakeSlack for
the Slack world; git only against a bare origin plus a clone under tmp_path.
"""

from __future__ import annotations

import calendar
import subprocess
from pathlib import Path

import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.faces import FakeFaceDetector
from snipebot.ts import US_PER_SECOND

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


def _config_dict(*, persistence: str = "files", tz: str = "UTC",
                 semesters: list[dict] | None = None) -> dict:
    return {
        "enabled": True,
        "persistence": persistence,
        "slack": {"channel": CHANNEL},
        "timezone": tz,
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": semesters or [
            {"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"},
        ],
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


def _world(msg_ts: str | None) -> FakeSlack:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2"),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    if msg_ts is not None:
        slack.post(at=msg_ts, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
                   files=[image_file("F0FILE001", b"snap", name="photo-1.png")])
    return slack


def _argv(cfg: Path, data: Path) -> list[str]:
    return ["--config", str(cfg), "--data-dir", str(data)]


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _git_rig(tmp_path: Path, monkeypatch, msg_ts: str, **cfg_kw):
    """(repo, config, slack) with one synced snipe committed and pushed to `data`."""
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
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    slack = _world(msg_ts)
    rc = main(["sync", "--no-react", "--no-post", *_argv(cfg, repo / "data")],
              slack_factory=lambda: slack,
              detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    return repo, cfg, slack


# --------------------------------------------------------------------------- findings

def test_history_lists_a_post_season_veto_movement(tmp_path, monkeypatch, capsys):
    """The round-2 repair of `history --semester` keeps only commits whose local commit day
    lies inside the chosen semester's [start, end] dates, so every commit made after the
    semester's last day (or before its first) is hidden from `history` under every
    `--semester`. PLAN section 8 / 40 section 4.2: `snipebot veto --ts` "has no deadline",
    every admin command is a large movement with "a dated line in the log", and "`snipebot
    history` prints the per-commit deltas" (PLAN: "`snipebot history` totals vetoes by
    actor"). Here the semester ends 2026-09-17, a snipe from 2026-09-15 is synced on
    2026-09-18 and an admin vetoes it the same day: `history` (default semester = the one
    with the latest end, 30 section 7) prints "history (0 commits)" although the data
    branch holds the veto movement that moved a pair of that semester."""
    sems = [{"name": "summer-2026", "start": "2026-09-01", "end": "2026-09-17"}]
    msg = _ts(2026, 9, 15, 10)
    repo, cfg, slack = _git_rig(tmp_path, monkeypatch, msg, semesters=sems)
    rc = main(["veto", "--ts", msg, "--by", ADMIN, "--no-react", "--no-post",
               *_argv(cfg, repo / "data")],
              slack_factory=lambda: slack,
              detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    log = _git(repo, "log", "--format=%s", "HEAD", "--")
    assert "2026-09-18" in log and "movement" in log, log   # control: the veto movement is on the branch
    capsys.readouterr()
    rc = main(["history", *_argv(cfg, repo / "data")])
    out = capsys.readouterr().out
    assert rc == int(Exit.OK)
    assert "movement" in out, out


def test_rules_bump_with_one_quoted_and_one_bare_semester_date(tmp_path):
    """40 section 1.4 accepts a semester `start` either as a bare YAML date (loads as a
    date) or as a quoted "YYYY-MM-DD" string, and this config loads cleanly. `rules bump`
    then picks the first semester start with `min(s["start"] for s in doc["semesters"])` on
    the raw YAML values, comparing a str with a datetime.date: TypeError, so a valid,
    future-dated bump exits 1 ("unexpected error") instead of 0. 40 section 4.2 `rules
    bump`: exit 0 when --effective-from is strictly later than H."""
    import datetime as _dt

    from snipebot.config import load_config

    d = _config_dict(semesters=[
        {"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"},
        {"name": "spring-2027", "start": _dt.date(2027, 1, 20), "end": _dt.date(2027, 5, 10)},
    ])
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    load_config(cfg)                               # control: the config is valid
    data = tmp_path / "data"
    data.mkdir()
    rc = main(["rules", "bump", "--effective-from", "2026-10-01 12:00", *_argv(cfg, data)])
    assert rc == int(Exit.OK)
    assert len(load_config(cfg).rules.entries) == 2
