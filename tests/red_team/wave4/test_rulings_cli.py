"""Regression tests for the wave-4 driver rulings the `cli` module carries: E-W4-18 (report,
export and an offline doctor judge rostered bots from the local users.json cache), E-W4-19
(restore keeps the CURRENT opted_out) and E-W4-25 (rejoin refuses while a config seed or a
live opt-out reaction would re-add the user).

Offline only: a FakeSlack world, files persistence under tmp_path, or git against a bare
origin plus a clone under tmp_path.
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
from snipebot.ledger import State, save_state
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


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.000000"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
OPTOUT_TS = _ts(2026, 9, 2, 9)


def _config_dict(*, persistence: str = "files", extras: list | None = None,
                 optout_messages: list | None = None, seeds: list | None = None) -> dict:
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
            "optout_messages": optout_messages or [],
            "opted_out": seeds or [],
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


def _world(posts: list[tuple[str, str]], *, bot_target: bool = False,
           now: str = NOW_TS) -> FakeSlack:
    """A channel holding one photo snipe per (ts, target) pair, all sent by SNIPER."""
    slack = FakeSlack(now=now, bot_user_id=BOT, users=_users(bot_target))
    for i, (ts, target) in enumerate(posts, start=1):
        slack.post(at=ts, user=SNIPER, channel=CHANNEL, text=f"<@{target}>",
                   files=[image_file(f"F0FILE{i:03d}", b"snap%d" % i,
                                     name=f"photo-{i}.png")])
    return slack


def _argv(cfg: Path, data: Path) -> list[str]:
    return ["--config", str(cfg), "--data-dir", str(data)]


def _cli(slack: FakeSlack, *argv: str) -> int:
    return main(list(argv), slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({}))


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)


# --------------------------------------------------------------------------- E-W4-18

def _bot_target_synced(tmp_path: Path) -> tuple[Path, Path]:
    """A files-mode data dir synced over one counted snipe (TARGET3) and one snipe of the
    rostered bot account TARGET4 (stored not_counted/target_is_bot)."""
    cfg = _write_config(tmp_path / "config.yaml", extras=[TARGET3, TARGET4])
    data = tmp_path / "data"
    data.mkdir()
    slack = _world([(_ts(2026, 9, 18, 9), TARGET3), (_ts(2026, 9, 18, 10), TARGET4)],
                   bot_target=True)
    assert _cli(slack, "sync", "--no-react", "--no-post", *_argv(cfg, data)) == 0
    assert '"target_is_bot"' in (data / "verdicts.jsonl").read_text(encoding="utf-8")
    return cfg, data


def test_report_judges_a_rostered_bot_from_the_users_cache(tmp_path, capsys, fixed_clock):
    """E-W4-18: `report` loads the config with the is_bot map from users.json, so a snipe
    sync stored as target_is_bot is not counted by `report` either."""
    cfg, data = _bot_target_synced(tmp_path)
    capsys.readouterr()
    assert main(["report", "--by", "snipes", *_argv(cfg, data)]) == int(Exit.OK)
    out = capsys.readouterr().out
    assert TARGET4 not in out and "user-4" not in out, out


def test_report_reads_mixed_users_cache_entries(tmp_path, capsys, fixed_clock):
    """E-W4-18: users.json entries may be a bare display name or {display_name, is_bot};
    a bare-name entry never breaks the load and the dict entry still marks the bot."""
    cfg, data = _bot_target_synced(tmp_path)
    cache = {SNIPER: "user-1", TARGET3: "user-3",
             TARGET4: {"display_name": "user-4", "is_bot": True}}
    (data / "users.json").write_text(json.dumps(cache), encoding="utf-8")
    capsys.readouterr()
    assert main(["report", "--by", "person", *_argv(cfg, data)]) == int(Exit.OK)
    out = capsys.readouterr().out
    sniper_row = next(line for line in out.splitlines() if "user-1" in line)
    assert sniper_row.split()[-1] == "1", sniper_row


def test_export_judges_a_rostered_bot_from_the_users_cache(
        tmp_path, capsys, monkeypatch, fixed_clock):
    """E-W4-18: `export` resolves RosterEntry.is_bot from users.json exactly as `report`."""
    cfg, data = _bot_target_synced(tmp_path)
    seen = {}
    real_export_all = cli.export_all

    def _spy(elig, roster, *rest, **kw):
        seen["is_bot"] = roster.entries[TARGET4].is_bot
        return real_export_all(elig, roster, *rest, **kw)

    monkeypatch.setattr(cli, "export_all", _spy)
    out_dir = tmp_path / "out"
    assert main(["export", "--out", str(out_dir), *_argv(cfg, data)]) == int(Exit.OK)
    assert seen["is_bot"] is True


def test_offline_doctor_is_handed_the_users_cache_is_bot_map(tmp_path, monkeypatch):
    """E-W4-18: an offline `doctor` gets the users.json is_bot map (`args.is_bot_cache`);
    an online doctor gets none from the cache (it reads users.list)."""
    from snipebot import doctor as doctor_mod

    cfg = _write_config(tmp_path / "config.yaml", extras=[TARGET3, TARGET4])
    data = tmp_path / "data"
    data.mkdir()
    (data / "users.json").write_text(json.dumps(
        {SNIPER: "user-1", TARGET3: {"display_name": "user-3", "is_bot": False},
         TARGET4: {"display_name": "user-4", "is_bot": True}}), encoding="utf-8")
    seen = []
    monkeypatch.setattr(doctor_mod, "run",
                        lambda args, slack_factory=None: seen.append(args.is_bot_cache) or 0)
    assert main(["doctor", "--offline", *_argv(cfg, data)]) == 0
    assert seen[-1] == {TARGET3: False, TARGET4: True}
    slack = _world([])
    assert main(["doctor", *_argv(cfg, data)], slack_factory=lambda: slack) == 0
    assert seen[-1] is None


# --------------------------------------------------------------------------- E-W4-19

def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _make_repo(tmp_path: Path) -> Path:
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
    return repo


def _opted_out(repo: Path) -> set[str]:
    state = json.loads((repo / "data" / "state.json").read_text(encoding="utf-8"))
    return set(state["opted_out"])


def test_restore_keeps_the_current_optout_set_in_both_directions(tmp_path, monkeypatch):
    """E-W4-19: the restored state.json takes the CURRENT opted_out, not the snapshot's:
    restoring a pre-opt-out snapshot keeps the opt-out, and restoring a snapshot taken
    before a `rejoin` does not bring the rejoined user back into the set."""
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("SNIPEBOT_DATA_REPO", str(repo))
    monkeypatch.setenv("SNIPEBOT_GIT_AUTHOR", "snipebot <snipebot@fixture.invalid>")
    monkeypatch.delenv("SNIPEBOT_CRASH_AT", raising=False)
    clock = {"now": 0}
    monkeypatch.setattr(cli, "_now_us", lambda: clock["now"])
    cfg = _write_config(tmp_path / "config.yaml", persistence="git",
                        extras=[TARGET3], optout_messages=[OPTOUT_TS])
    slack = _world([(_ts(2026, 9, 3, 9), TARGET)], now=_ts(2026, 9, 3, 12))
    slack.post(at=OPTOUT_TS, user=ADMIN, channel=CHANNEL, text="react here to opt out")
    argv = _argv(cfg, repo / "data")

    def at(y, mo, d, h) -> None:
        slack.as_of(_ts(y, mo, d, h))
        clock["now"] = _secs(y, mo, d, h) * US_PER_SECOND

    at(2026, 9, 3, 12)
    assert _cli(slack, "sync", "--no-react", "--no-post", *argv) == 0
    before_optout = _git(repo, "rev-parse", "HEAD").strip()

    slack.react(at=_ts(2026, 9, 4, 10), ts=OPTOUT_TS, channel=CHANNEL, user=TARGET3,
                name="wave")
    at(2026, 9, 4, 12)
    assert _cli(slack, "sync", "--no-react", "--no-post", *argv) == 0
    assert TARGET3 in _opted_out(repo)
    after_optout = _git(repo, "rev-parse", "HEAD").strip()

    at(2026, 9, 5, 12)
    assert _cli(slack, "restore", "--from", before_optout, *argv) == 0
    assert TARGET3 in _opted_out(repo), "restore dropped an observed opt-out"

    # TARGET3 removes the reaction and is rejoined; restoring the opted-out snapshot must
    # not re-add them.
    slack.unreact(at=_ts(2026, 9, 6, 10), ts=OPTOUT_TS, channel=CHANNEL, user=TARGET3,
                  name="wave")
    at(2026, 9, 6, 12)
    assert _cli(slack, "rejoin", TARGET3, "--no-react", "--no-post", *argv) == 0
    assert TARGET3 not in _opted_out(repo)
    at(2026, 9, 7, 12)
    assert _cli(slack, "restore", "--from", after_optout, *argv) == 0
    assert TARGET3 not in _opted_out(repo), "restore re-added a rejoined user"


# --------------------------------------------------------------------------- E-W4-25

def _opted_out_files(data: Path) -> dict:
    return json.loads((data / "state.json").read_text(encoding="utf-8"))["opted_out"]


def test_rejoin_refuses_while_user_is_a_config_seed(tmp_path, capsys, fixed_clock):
    """E-W4-25: `rejoin U` exits 2 while U is in consent.opted_out and names it; the durable
    set is unchanged."""
    cfg = _write_config(tmp_path / "config.yaml", extras=[TARGET3], seeds=[TARGET3])
    data = tmp_path / "data"
    data.mkdir()
    save_state(data / "state.json", State(opted_out={TARGET3: NOW_US}))
    slack = _world([])
    capsys.readouterr()
    rc = _cli(slack, "rejoin", TARGET3, "--no-react", "--no-post", *_argv(cfg, data))
    err = capsys.readouterr().err
    assert rc == int(Exit.CONFIG_INVALID)
    assert "consent.opted_out" in err
    assert TARGET3 in _opted_out_files(data)


def test_rejoin_refuses_while_user_still_reacts_on_optout_message(
        tmp_path, capsys, fixed_clock):
    """E-W4-25: `rejoin U` exits 2 while U still reacts on a configured opt-out message
    (reactions_get), naming the message; once the reaction is gone the rejoin succeeds."""
    cfg = _write_config(tmp_path / "config.yaml", extras=[TARGET3],
                        optout_messages=[OPTOUT_TS])
    data = tmp_path / "data"
    data.mkdir()
    save_state(data / "state.json", State(opted_out={TARGET3: NOW_US}))
    slack = _world([])
    slack.post(at=OPTOUT_TS, user=ADMIN, channel=CHANNEL, text="react here to opt out")
    slack.react(at=_ts(2026, 9, 4, 10), ts=OPTOUT_TS, channel=CHANNEL, user=TARGET3,
                name="wave::skin-tone-2")
    capsys.readouterr()
    rc = _cli(slack, "rejoin", TARGET3, "--no-react", "--no-post", *_argv(cfg, data))
    err = capsys.readouterr().err
    assert rc == int(Exit.CONFIG_INVALID)
    assert OPTOUT_TS in err
    assert TARGET3 in _opted_out_files(data)

    slack.unreact(at=_ts(2026, 9, 18, 11), ts=OPTOUT_TS, channel=CHANNEL, user=TARGET3,
                  name="wave::skin-tone-2")
    rc = _cli(slack, "rejoin", TARGET3, "--no-react", "--no-post", *_argv(cfg, data))
    assert rc == int(Exit.OK)
    assert TARGET3 not in _opted_out_files(data)


def test_rejoin_proceeds_when_optout_message_was_deleted(tmp_path, capsys, fixed_clock):
    """E-W4-25: a deleted opt-out message holds no reaction, so it never blocks rejoin."""
    cfg = _write_config(tmp_path / "config.yaml", extras=[TARGET3],
                        optout_messages=[OPTOUT_TS])
    data = tmp_path / "data"
    data.mkdir()
    save_state(data / "state.json", State(opted_out={TARGET3: NOW_US}))
    slack = _world([])
    slack.post(at=OPTOUT_TS, user=ADMIN, channel=CHANNEL, text="react here to opt out")
    slack.react(at=_ts(2026, 9, 4, 10), ts=OPTOUT_TS, channel=CHANNEL, user=TARGET3,
                name="wave")
    slack.delete_message(at=_ts(2026, 9, 5, 10), ts=OPTOUT_TS, channel=CHANNEL)
    rc = _cli(slack, "rejoin", TARGET3, "--no-react", "--no-post", *_argv(cfg, data))
    assert rc == int(Exit.OK), capsys.readouterr().err
    assert TARGET3 not in _opted_out_files(data)
