"""CLI tests (40-config-cli.md section 4): flag parsing, exit codes, and every command
driven end to end against a `FakeSlack` world under `persistence: files`.

Self-contained: the config is written as YAML on disk (so `rules bump` can rewrite it and
the next `load_config` sees the change) and the Slack world is a `FakeSlack` injected
through `slack_factory`. `_now_us` and `_sleep` are the CLI's injection seams; both are
monkeypatched here so nothing depends on the wall clock.
"""

from __future__ import annotations

import calendar
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import State, load_ledger, save_state
from snipebot.ts import US_PER_SECOND

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file

CHANNEL = "C0MAIN01"
BOT = "U0BOT"
ADMIN = "U0ADMIN"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 10)


def _config_dict(*, persistence="files", selfie_bonus=False):
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
            "selfie_bonus": selfie_bonus,
        },
        "players": {"count_intra_group": True, "groups": {"fam": ["U0AAAA1", "U0BBBB1"]}, "extras": []},
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


def _write_config(tmp_path: Path, **kw) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config_dict(**kw), sort_keys=False), encoding="utf-8")
    return path


def _data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d


def _world(with_snipe=True) -> FakeSlack:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        "U0AAAA1": FakeUser(id="U0AAAA1", display_name="Alex"),
        "U0BBBB1": FakeUser(id="U0BBBB1", display_name="Bailey"),
        ADMIN: FakeUser(id=ADMIN, display_name="Admin"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    if with_snipe:
        slack.post(at=MSG_TS, user="U0AAAA1", channel=CHANNEL, text="<@U0BBBB1>",
                   files=[image_file("F01", b"snap")])
    return slack


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)


def _base_argv(cfg: Path, data: Path):
    return ["--config", str(cfg), "--data-dir", str(data)]


# --------------------------------------------------------------------------- #
# Flag parsing
# --------------------------------------------------------------------------- #

def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_report_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["report", "--help"])
    assert exc.value.code == 0


def test_parser_covers_every_command():
    parser = cli._build_parser()
    for argv in (
        ["sync", "--no-react", "--no-post", "--reevaluate"],
        ["run", "--once", "--no-post"],
        ["report", "--by", "person"],
        ["export", "--semester", "fall-2026", "--out", "x"],
        ["roster"],
        ["backfill", "--from", "2026-09-01", "--dry-run"],
        ["veto", "--ts", "1.0", "--by", ADMIN],
        ["unveto", "--ts", "1.0"],
        ["selfie", "--ts", "1.0", "--no"],
        ["rejoin", "U0AAAA1"],
        ["purge", "--user", "U0AAAA1", "--rewrite-history", "--yes"],
        ["accept-deletes", "--count", "3"],
        ["rules", "bump", "--effective-from", "now"],
        ["history", "--limit", "10"],
        ["restore", "--from", "abc123"],
        ["doctor", "--offline"],
    ):
        ns = parser.parse_args(argv)
        assert hasattr(ns, "_handler")


def test_sync_flags_parsed():
    ns = cli._build_parser().parse_args(["sync", "--no-react", "--no-post", "--reevaluate"])
    assert ns.no_react and ns.no_post and ns.reevaluate


def test_backfill_no_react_default_on():
    ns = cli._build_parser().parse_args(["backfill", "--from", "2026-09-01"])
    assert ns.no_react is True


# --------------------------------------------------------------------------- #
# Exit codes
# --------------------------------------------------------------------------- #

def test_missing_config_exit_config_invalid(tmp_path):
    data = _data_dir(tmp_path)
    rc = main(["sync", "--no-post", "--config", str(tmp_path / "nope.yaml"),
               "--data-dir", str(data)], slack_factory=_world, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.CONFIG_INVALID)


def test_bad_config_exit_config_invalid(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("slack: {}\n", encoding="utf-8")  # missing required keys
    data = _data_dir(tmp_path)
    rc = main(["sync", "--no-post", *_base_argv(cfg, data)],
              slack_factory=_world, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.CONFIG_INVALID)


def test_missing_token_exit_slack_error(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    rc = main(["roster", *_base_argv(cfg, data)])  # no slack_factory -> needs the token
    assert rc == int(Exit.SLACK_ERROR)


def test_default_slack_factory_threads_configured_fetch_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    cfg_dict = _config_dict()
    cfg_dict["faces"] = {"max_image_bytes": 123456}
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(cfg_dict, sort_keys=False), encoding="utf-8")
    data = _data_dir(tmp_path)

    captured: dict = {}

    def fake_make_client(token, **kwargs):
        captured["token"] = token
        captured.update(kwargs)
        return _world()

    monkeypatch.setattr("snipebot.slack_io.make_client", fake_make_client)

    rc = main(["roster", *_base_argv(cfg, data)])  # no slack_factory -> default path
    assert rc == int(Exit.OK)
    assert captured["token"] == "xoxb-test-token"
    assert captured["max_image_bytes"] == 123456
    assert captured["fetch_timeout_seconds"] == 10


# --------------------------------------------------------------------------- #
# sync / report / export end to end
# --------------------------------------------------------------------------- #

def test_sync_writes_ledger(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    rc = main(["sync", "--no-react", "--no-post", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    out = capsys.readouterr().out
    assert "pushed local only" in out
    ledger = load_ledger(data / "ledger.jsonl")
    assert [r.ts for r in ledger] == [MSG_TS]
    assert (data / "verdicts.jsonl").exists()


def test_sync_moved_line_counts_by_reason(tmp_path, capsys):
    # R5 / 40 §4.3: a sync that counts one snipe prints the flip on stdout keyed by reason.
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()  # one countable snipe (Alex -> Bailey, both rostered, with an image)
    rc = main(["sync", "--no-react", "--no-post", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    out = capsys.readouterr().out
    assert "moved: counted +1" in out


def test_second_noop_sync_prints_no_change(tmp_path, capsys):
    # R5: a second sync at the same `now` over a stable channel changes no bytes, so it prints
    # a single 'no change' (no persistence line, no `moved:` line) under files mode.
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    assert main(["sync", "--no-react", "--no-post", *_base_argv(cfg, data)],
                slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({})) == 0
    capsys.readouterr()
    rc = main(["sync", "--no-react", "--no-post", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no change" in out
    assert "moved:" not in out


def test_report_by_person_prints_table(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    main(["sync", "--no-react", "--no-post", *_base_argv(cfg, data)],
         slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    capsys.readouterr()
    rc = main(["report", "--by", "person", *_base_argv(cfg, data)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "person" in out and "points" in out


def test_export_writes_six_csv_and_one_xlsx(tmp_path):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    main(["sync", "--no-react", "--no-post", *_base_argv(cfg, data)],
         slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    out_dir = tmp_path / "exports"
    rc = main(["export", "--out", str(out_dir), *_base_argv(cfg, data)])
    assert rc == 0
    csvs = sorted(out_dir.glob("*.csv"))
    xlsx = sorted(out_dir.glob("*.xlsx"))
    assert len(csvs) == 6
    assert len(xlsx) == 1


# --------------------------------------------------------------------------- #
# run loop with an injected sleep
# --------------------------------------------------------------------------- #

def test_run_executes_n_ticks(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    ticks: list[float] = []

    def fake_sleep(seconds):
        ticks.append(seconds)
        if len(ticks) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_sleep", fake_sleep)
    rc = main(["run", "--no-react", "--no-post", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    assert len(ticks) == 3  # three full passes, each followed by the injected sleep


def test_run_once_single_pass(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    monkeypatch.setattr(cli, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    rc = main(["run", "--once", "--no-react", "--no-post", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0


# --------------------------------------------------------------------------- #
# veto / selfie / rejoin / accept-deletes / backfill
# --------------------------------------------------------------------------- #

def _sync_once(cfg, data, slack):
    return main(["sync", "--no-react", "--no-post", "--config", str(cfg),
                 "--data-dir", str(data)], slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({}))


def test_veto_marks_message_vetoed(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    _sync_once(cfg, data, slack)
    capsys.readouterr()
    rc = main(["veto", "--ts", MSG_TS, "--by", ADMIN, "--no-react",
               *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    assert "pushed local only" in capsys.readouterr().out
    row = next(json.loads(line) for line in (data / "ledger.jsonl").read_text().splitlines())
    assert any(v["source"] == "cli" and v["by"] == ADMIN for v in row["vetoes"])


def test_veto_unknown_ts_exit_2(tmp_path):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    _sync_once(cfg, data, slack)
    rc = main(["veto", "--ts", _ts(2000, 1, 1), "--by", ADMIN, *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.CONFIG_INVALID)


def test_selfie_sets_override(tmp_path):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    _sync_once(cfg, data, slack)
    rc = main(["selfie", "--ts", MSG_TS, "--by", ADMIN, "--no-react",
               *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    row = next(json.loads(line) for line in (data / "ledger.jsonl").read_text().splitlines())
    assert row["selfie_override"] is not None
    assert row["selfie_override"]["source"] == "cli"


def test_selfie_unknown_ts_exit_2(tmp_path):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    _sync_once(cfg, data, slack)
    rc = main(["selfie", "--ts", _ts(2000, 1, 1), "--by", ADMIN, *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.CONFIG_INVALID)


def test_rejoin_removes_from_opted_out(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    save_state(data / "state.json", State(opted_out={"U0BBBB1": NOW_US}))
    slack = _world(with_snipe=False)
    rc = main(["rejoin", "U0BBBB1", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    assert "rejoined U0B" in capsys.readouterr().out
    state = json.loads((data / "state.json").read_text())
    assert "U0BBBB1" not in state["opted_out"]


def test_rejoin_not_opted_out_exit_2(tmp_path):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world(with_snipe=False)
    rc = main(["rejoin", "U0BBBB1", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.CONFIG_INVALID)


def test_backfill_dry_run_writes_nothing(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    rc = main(["backfill", "--from", "2026-09-01", "--dry-run", "--no-post",
               *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    assert "dry run" in capsys.readouterr().out
    assert not (data / "ledger.jsonl").exists()


def test_backfill_writes_ledger(tmp_path):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    rc = main(["backfill", "--from", "2026-09-01", "--no-post", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    assert [r.ts for r in load_ledger(data / "ledger.jsonl")] == [MSG_TS]


def test_accept_deletes_mismatch_exit_7(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    # Six messages, then delete all of them so newly_deleted (6) > max_deletes_per_run (5).
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        "U0AAAA1": FakeUser(id="U0AAAA1"), "U0BBBB1": FakeUser(id="U0BBBB1"), ADMIN: FakeUser(id=ADMIN),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    tss = []
    for i in range(6):
        t = _ts(2026, 9, 18, 10, i)
        slack.post(at=t, user="U0AAAA1", channel=CHANNEL, text="<@U0BBBB1>",
                   files=[image_file(f"F{i}", f"snap{i}".encode())])
        tss.append(t)
    # A text-only survivor keeps later fetches non-empty (zero returned infers no miss, E-W4-16).
    slack.post(at=_ts(2026, 9, 18, 10, 30), user="U0AAAA1", channel=CHANNEL, text="hello")
    _sync_once(cfg, data, slack)
    for i, t in enumerate(tss):
        slack.delete_message(at=_ts(2026, 9, 18, 11, i), ts=t, channel=CHANNEL)
    _sync_once(cfg, data, slack)  # first miss: rows go pending (missing_runs=1), no breaker yet
    rc = main(["accept-deletes", "--count", "3", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.ACCEPT_DELETES_MISMATCH)
    assert "retry with --count 6" in capsys.readouterr().err


def test_accept_deletes_release(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        "U0AAAA1": FakeUser(id="U0AAAA1"), "U0BBBB1": FakeUser(id="U0BBBB1"), ADMIN: FakeUser(id=ADMIN),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    tss = []
    for i in range(6):
        t = _ts(2026, 9, 18, 10, i)
        slack.post(at=t, user="U0AAAA1", channel=CHANNEL, text="<@U0BBBB1>",
                   files=[image_file(f"F{i}", f"snap{i}".encode())])
        tss.append(t)
    _sync_once(cfg, data, slack)
    for i, t in enumerate(tss):
        slack.delete_message(at=_ts(2026, 9, 18, 11, i), ts=t, channel=CHANNEL)
    _sync_once(cfg, data, slack)  # first miss: rows go pending (missing_runs=1), no breaker yet
    rc = main(["accept-deletes", "--count", "6", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    assert "released" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# roster / rules bump / history / restore / purge / doctor
# --------------------------------------------------------------------------- #

def test_roster_lists_members(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world(with_snipe=False)
    rc = main(["roster", *_base_argv(cfg, data)], slack_factory=lambda: slack)
    assert rc == 0
    out = capsys.readouterr().out
    assert "U0AAAA1  Alex" in out
    assert BOT not in out  # the bot is omitted


def test_rules_bump_rewrites_yaml(tmp_path):
    from snipebot.config import load_config

    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    assert len(load_config(cfg).rules.entries) == 1
    rc = main(["rules", "bump", "--effective-from", "2026-10-01", *_base_argv(cfg, data)])
    assert rc == 0
    doc = yaml.safe_load(cfg.read_text())
    assert isinstance(doc["rules"], list) and len(doc["rules"]) == 2
    reloaded = load_config(cfg)
    assert len(reloaded.rules.entries) == 2


def test_history_files_mode_empty(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    rc = main(["history", *_base_argv(cfg, data)])
    # 20 §8.6: history needs git history; files mode exits 2 naming persistence: files.
    assert rc == int(Exit.CONFIG_INVALID)
    assert "persistence: files" in capsys.readouterr().err


def test_restore_files_mode_reports_unavailable(tmp_path):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    rc = main(["restore", "--from", "deadbeef", *_base_argv(cfg, data)])
    assert rc == int(Exit.CONFIG_INVALID)


def test_purge_without_yes_needs_confirmation(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    _sync_once(cfg, data, slack)
    rc = main(["purge", "--user", "U0AAAA1", "--rewrite-history", *_base_argv(cfg, data)],
              slack_factory=lambda: slack)
    assert rc == int(Exit.CONFIG_INVALID)
    assert "confirmation" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Lazy detector: no selfie_bonus in force -> cv2 is never imported (E8)
# --------------------------------------------------------------------------- #

def test_sync_without_selfie_bonus_imports_no_cv2():
    """The default detector is a LazyDetector that builds YuNetDetector (and imports cv2)
    only on the first count_faces call. A sync whose rules never put selfie_bonus in force
    never counts a face, so it imports no OpenCV. Verified in a fresh subprocess so no
    earlier test in this interpreter has already imported cv2."""
    repo_root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import pathlib
        import sys
        import tempfile

        import yaml

        assert "cv2" not in sys.modules, "cv2 imported before the run started"
        from snipebot import cli
        from tests.test_cli import NOW_US, _config_dict, _world

        work = pathlib.Path(tempfile.mkdtemp())
        cfg = work / "config.yaml"
        cfg.write_text(
            yaml.safe_dump(_config_dict(selfie_bonus=False), sort_keys=False),
            encoding="utf-8",
        )
        data = work / "data"
        data.mkdir()
        cli._now_us = lambda: NOW_US
        rc = cli.main(
            ["sync", "--no-react", "--no-post",
             "--config", str(cfg), "--data-dir", str(data)],
            slack_factory=_world,
            detector_factory=None,
        )
        assert rc == 0, "sync exited %r" % rc
        assert "cv2" not in sys.modules, "cv2 was imported despite selfie_bonus off"
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OK" in proc.stdout


