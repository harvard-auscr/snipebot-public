"""Wave 4, round 3 breaker tests for the `cli` surface (40-config-cli.md section 4).

Every test here fails against the current code for the reason its docstring names. The
Slack world is a `FakeSlack`; a test that builds the real transport replaces the socket
layer with a stub that refuses every connection, so nothing leaves the process. Git runs
only against a bare origin plus a clone under `tmp_path`.
"""

from __future__ import annotations

import calendar
import http.client
import socket
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
TARGET3 = "U0AAA003"
TARGET4 = "U0AAA004"
ADMIN = "U0AAA009"

FAKE_TOKEN_A = "xoxb-0000-fixture"
FAKE_TOKEN_B = "xoxp-0000-fixture"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.000000"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND


def _config_dict(*, persistence: str = "files", extras: list | None = None) -> dict:
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


def _users(bot_target: bool = False) -> dict:
    return {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2"),
        TARGET3: FakeUser(id=TARGET3, display_name="user-3"),
        TARGET4: FakeUser(id=TARGET4, display_name="user-4", is_bot=bot_target),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9"),
    }


def _world(posts: list[tuple[str, str]], *, bot_target: bool = False) -> FakeSlack:
    """A channel holding one photo snipe per (ts, target) pair, all sent by SNIPER."""
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=_users(bot_target))
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


def _refuse_all_connections(monkeypatch) -> None:
    """Every outbound connection fails in-process with a recognisable error."""
    def _refuse(*_a, **_k):
        raise ConnectionRefusedError("offline-sentinel")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", _refuse)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


# --------------------------------------------------------------------------- git rig

def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _git_rig(tmp_path: Path, monkeypatch, slack: FakeSlack) -> tuple[Path, Path, Path]:
    """(origin, repo, config) with `slack`'s snipes synced, committed and pushed."""
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
    assert _sync(cfg, repo / "data", slack) == 0
    return origin, repo, cfg


# --------------------------------------------------------------------------- findings

def test_malformed_token_is_never_printed(tmp_path, monkeypatch, capsys, caplog):
    """40 §4 / plan §6: logs and command output carry IDs only, never a token; a bad
    token must end in a clear error that never prints it. A SLACK_BOT_TOKEN holding a
    line break (a secret pasted across two lines, or `$(cat .env)` of a two-line file)
    is passed to the transport unchecked. http.client refuses the header with
    `ValueError: Invalid header value b'Bearer <token>'`; slack_sdk logs that text at
    ERROR through the root logger (left at INFO), `roster`/`sync` print it again as
    'slack error: ...', and `doctor` prints it in every DOC-* detail. Both halves of
    the secret reach stderr/stdout, which is the Actions log on the runner."""
    cfg = _write_config(tmp_path / "config.yaml")
    data = tmp_path / "data"
    data.mkdir()
    _refuse_all_connections(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", f"{FAKE_TOKEN_A}\n{FAKE_TOKEN_B}")
    rc_roster = main(["roster", *_argv(cfg, data)])
    rc_doctor = main(["doctor", *_argv(cfg, data)])
    captured = capsys.readouterr()
    text = captured.out + captured.err + caplog.text
    assert rc_roster != int(Exit.OK) and rc_doctor != int(Exit.OK)
    assert FAKE_TOKEN_A not in text and FAKE_TOKEN_B not in text, (
        "the bot token was printed")


def test_report_counts_a_snipe_the_sync_rejected_as_target_is_bot(tmp_path, capsys):
    """30 §2.4 DECISION: a standings export must never disagree with the digest; sync
    and the digest judge with the real is_bot map (40 §2.1). `report` (and `export`)
    load the config with no is_bot map, so a snipe on a rostered bot account that sync
    stored as not_counted/target_is_bot is re-judged COUNTED: `report --by person`
    gives the sniper 2 points while verdicts.jsonl and the digest give 1."""
    cfg = _write_config(tmp_path / "config.yaml", extras=[TARGET3, TARGET4])
    data = tmp_path / "data"
    data.mkdir()
    slack = _world([(_ts(2026, 9, 18, 9), TARGET3), (_ts(2026, 9, 18, 10), TARGET4)],
                   bot_target=True)
    assert _sync(cfg, data, slack) == 0
    verdicts = (data / "verdicts.jsonl").read_text(encoding="utf-8")
    assert '"target_is_bot"' in verdicts
    capsys.readouterr()
    assert main(["report", "--by", "person", *_argv(cfg, data)]) == int(Exit.OK)
    out = capsys.readouterr().out
    sniper_row = next(line for line in out.splitlines() if SNIPER in line)
    assert sniper_row.split()[-1] == "1", sniper_row


def test_history_prints_no_counts_by_reason(tmp_path, monkeypatch, capsys):
    """40 §4.2 `history`: 'For each daily/movement commit: SHA, local date, and counts by
    reason of what moved (never IDs/names)'. `_cmd_history` prints only a total
    ('moved 1'), so an admin reading the log after a veto cannot tell a counted snipe
    that became vetoed from any other flip."""
    ts = _ts(2026, 9, 18, 10)
    slack = _world([(ts, TARGET3)])
    origin, repo, cfg = _git_rig(tmp_path, monkeypatch, slack)
    rc = main(["veto", "--ts", ts, *_argv(cfg, repo / "data")],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.OK)
    veto_sha = _git(origin, "rev-parse", "data").strip()
    capsys.readouterr()
    assert main(["history", *_argv(cfg, repo / "data")]) == int(Exit.OK)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith(veto_sha))
    assert "vetoed" in line, line
