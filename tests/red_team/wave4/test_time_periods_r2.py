"""Wave 4, surface "time-periods", round 2.

Semester selection and day/instant boundaries at the CLI. Offline: FakeSlack for the
Slack world; git only against a bare origin plus a clone under tmp_path.
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


def _sync(cfg: Path, data: Path, slack: FakeSlack) -> int:
    return main(["sync", "--no-react", "--no-post", *_argv(cfg, data)],
                slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({}))


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _git_rig(tmp_path: Path, monkeypatch, **cfg_kw) -> tuple[Path, Path, Path]:
    """(origin, repo, config) with one synced snipe committed and pushed to `data`."""
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
    rc = _sync(cfg, repo / "data", _world(_ts(2026, 9, 18, 10)))
    assert rc == 0
    return origin, repo, cfg


# --------------------------------------------------------------------------- findings

def test_history_unknown_semester_exits_config_invalid(tmp_path, monkeypatch):
    """`history --semester NAME` is parsed but never read: `_cmd_history` ignores
    `args.semester`, so a name that matches no configured semester prints the whole commit
    list and exits 0. 40 section 4.2 `history` ("--semester NAME (default as report)") and
    30 section 7 (the default "shared with export and history"; "A --semester that names no
    configured semester exits 2", 40 section 4.4 CONFIG_INVALID)."""
    _origin, repo, cfg = _git_rig(tmp_path, monkeypatch)
    rc = main(["history", "--semester", "no-such-term", *_argv(cfg, repo / "data")])
    assert rc == int(Exit.CONFIG_INVALID)


def test_rules_bump_before_the_first_semester_start_is_accepted(tmp_path):
    """A pre-season `rules bump --effective-from 2026-08-20 12:00` (H = a pre-season row at
    2026-08-15 10:00, first semester start 2026-09-01) is strictly later than H, so 40
    section 4.2 says exit 0. The mapping form is rewritten with the prior entry dated at the
    first semester start (2026-09-01) and the new entry appended after it at 2026-08-20, an
    out-of-order list; the load check then refuses and the command exits 2. The documented
    unblock (40 section 3: "`snipebot rules bump --effective-from now` (apply forward only)")
    is unusable for any rule change made before the season opens, although a valid list
    exists (prior entry dated at or before both the new entry and the first semester start,
    40 section 1.5 rules 2 and 3)."""
    from snipebot.config import load_config
    from snipebot.ledger import save_ledger
    from tests._helpers_export import cand

    cfg = _write_config(tmp_path / "config.yaml")
    data = tmp_path / "data"
    data.mkdir()
    row_ts = _ts(2026, 8, 15, 10)
    save_ledger(data / "ledger.jsonl", [cand(row_ts, SNIPER, (TARGET,))])
    rc = main(["rules", "bump", "--effective-from", "2026-08-20 12:00", *_argv(cfg, data)])
    assert rc == int(Exit.OK)
    entries = load_config(cfg).rules.entries
    assert entries[-1].effective_from_us == _secs(2026, 8, 20, 12) * US_PER_SECOND


def test_post_in_repeated_last_hour_of_semester_end_date_is_in_season(tmp_path):
    """The semester is a local-date range: 40 section 1.4 `end` is inclusive, the default
    semester is "the one whose [start, end] contains today's local date" (30 section 7), and
    00 section 8 says a message's local calendar date is unambiguous even on a 25-hour DST
    day. config resolves `end` 23:59:59.999999 with fold=0, the FIRST occurrence, so in a
    zone whose clocks fall back at midnight (America/Santiago, 2026-04-05 00:00 -03 ->
    2026-04-04 23:00 -04) a snipe at the second 23:30 on the end date (local date
    2026-04-04, inside [start, end]) scores OUT_OF_SEASON while the digest label says the
    semester covers that date."""
    from snipebot.config import load_config
    from snipebot.rules import Reason, evaluate
    from tests._helpers_export import cand

    sems = [{"name": "autumn-2026", "start": "2026-03-01", "end": "2026-04-04"}]
    cfg = _write_config(tmp_path / "config.yaml", tz="America/Santiago", semesters=sems)
    config = load_config(cfg)
    first = _ts(2026, 4, 5, 2, 30)       # 2026-04-04 23:30 -03, the first occurrence
    second = _ts(2026, 4, 5, 3, 30)      # 2026-04-04 23:30 -04, the repeated hour
    rows = [cand(first, SNIPER, (TARGET,)), cand(second, SNIPER, (TARGET,))]
    mv1, mv2 = evaluate(rows, config.rules, config.roster, set(), config.semesters, config.tz)
    assert mv1.reason is Reason.COUNTED          # control: same local date, first 23:30
    assert mv2.reason is not Reason.OUT_OF_SEASON
