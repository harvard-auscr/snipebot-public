"""Round-1 spec-conformance red team against snipebot/cli.py (40-config-cli.md section 4).

Every test drives cli.main(argv, slack_factory=..., detector_factory=...) against a FakeSlack
world under persistence: files, and asserts a rule from the named spec section. Each test is a
break: it FAILS on the current code and would pass once the code conforms.

Self-contained builders (config-on-disk + FakeSlack world) mirror tests/test_cli.py so this
file imports no other red-team module.
"""

from __future__ import annotations

import calendar
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
BOT = "U0BOT"
ADMIN = "U0ADMIN"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 10)


def _config_dict(*, persistence="files"):
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
                    "groups": {"fam": ["U0AAAA1", "U0BBBB1"]}, "extras": []},
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


def _write_config_at(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_config_dict(), sort_keys=False), encoding="utf-8")
    return path


def _write_config(tmp_path: Path) -> Path:
    return _write_config_at(tmp_path / "config.yaml")


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


def _base_argv(cfg: Path, data: Path):
    return ["--config", str(cfg), "--data-dir", str(data)]


def _sync_once(cfg, data, slack):
    return main(["sync", "--no-react", "--no-post", *_base_argv(cfg, data)],
                slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({}))


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)


# --------------------------------------------------------------------------- #
# Finding 1 — global flags before the subcommand are silently dropped
# --------------------------------------------------------------------------- #

def test_global_config_flag_before_subcommand_is_honored(tmp_path, monkeypatch):
    """40 §4: "Two flags are accepted by **every** command: `--config PATH` ... `--data-dir DIR`".

    A global flag placed BEFORE the subcommand must be honored. Because `--config`/`--data-dir`
    are declared on both the top parser and each subparser (`parents=[common]`), argparse
    re-applies the subparser's default and clobbers the value parsed before the subcommand, so
    `snipebot --config prod.yaml sync` silently runs the default `./config.yaml` instead.
    """
    monkeypatch.chdir(tmp_path)  # a clean cwd with no ./config.yaml
    proj = tmp_path / "proj"
    cfg = _write_config_at(proj / "config.yaml")
    data = proj / "data"
    data.mkdir(parents=True, exist_ok=True)
    slack = _world()
    # Populate the ledger with the flags AFTER the subcommand (works today).
    assert _sync_once(cfg, data, slack) == 0
    # Same paths, now as GLOBAL flags placed BEFORE the subcommand.
    rc = main(["--config", str(cfg), "--data-dir", str(data), "report", "--by", "person"])
    assert rc == int(Exit.OK)


# --------------------------------------------------------------------------- #
# Finding 2 — unveto does not validate --by as a UserID
# --------------------------------------------------------------------------- #

def test_unveto_bad_by_exits_config_invalid(tmp_path):
    """40 §4.2 (veto / unveto, shared Exit row): "`0`; `2` if no ledger row has that ts, or
    `--by` fails `UserID`".

    `unveto --by <garbage>` must exit `2`; the handler removes CLI vetoes without validating
    `--by`, so a malformed actor id is accepted and the command exits `0`.
    """
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    assert _sync_once(cfg, data, slack) == 0
    rc = main(["unveto", "--ts", MSG_TS, "--by", "not-a-user", "--no-react", "--no-post",
               *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.CONFIG_INVALID)


# --------------------------------------------------------------------------- #
# Finding 3 — an unexpected exception inside run_sync escapes as a traceback
# --------------------------------------------------------------------------- #

def test_unexpected_exception_maps_to_exit_unexpected(tmp_path, monkeypatch):
    """40 §4.4: "`UNEXPECTED = 1  # uncaught error`" and "Any exit `!= 0` fails the Actions job
    and triggers the owner email".

    An unexpected (non-`SlackError`, non-`_CliError`) exception raised while a sync pass runs
    must be mapped to exit `1`, not escape `main()` as a traceback (which would crash the
    Actions job outside the exit-code contract and can print a name/permalink to stderr).
    """
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()

    def _boom(*_a, **_k):
        raise RuntimeError("unexpected failure inside the fetch step")

    monkeypatch.setattr(slack, "history", _boom)
    rc = main(["sync", "--no-react", "--no-post", *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.UNEXPECTED)
