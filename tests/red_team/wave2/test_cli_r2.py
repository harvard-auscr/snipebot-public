"""Round-2 red team against snipebot/cli.py (40-config-cli.md section 4): hostile input,
failure injection, and the printed-confirmation / fail-closed contract.

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


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config_dict(), sort_keys=False), encoding="utf-8")
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
# Finding 1 — the stdout `moved:` line is reaction/delete/digest counts, not the
# spec's "verdict flips by reason"
# --------------------------------------------------------------------------- #

def test_moved_line_reports_verdict_flips_by_reason(tmp_path, capsys):
    """40 §4.3: the confirming block's last line is
    "`moved: counted +3 cooldown -1 late_tag +2   # verdict flips by reason; never IDs or names`".

    A sync that counts one snipe must print that flip on stdout keyed by its verdict reason
    (here `counted`). `_print_write_outcome` instead prints
    `moved: reactions +N -M deleted K digests P/R` — Slack side-effect counts, never the
    verdict-flip-by-reason line the section mandates — so `counted` never reaches stdout.
    """
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()  # one countable snipe (Alex -> Bailey, both rostered, with an image)
    rc = _sync_once(cfg, data, slack)
    assert rc == int(Exit.OK)
    out = capsys.readouterr().out
    assert "counted" in out


# --------------------------------------------------------------------------- #
# Finding 3 — a syntactically malformed config.yaml escapes as a traceback instead
# of the controlled CONFIG_INVALID (exit 2)
# --------------------------------------------------------------------------- #

def test_malformed_yaml_config_exits_config_invalid(tmp_path):
    """40 §4.4: "`CONFIG_INVALID = 2  # ConfigError (§2.2); "unknown semester"/"malformed
    arg"`" and "Any exit `!= 0` fails the Actions job". A malformed `config.yaml` is the
    canonical bad config (§5.1 `DOC-CONFIG-PARSE`: "`config.yaml` parses and validates").

    `_load_config` catches only `ConfigError`, but `yaml.safe_load` on a broken document
    raises `yaml.YAMLError`, which escapes `main()` as an uncaught traceback (an
    out-of-contract exit `1`) instead of the controlled exit `2`.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text("slack: {channel: 'C1'\n  timezone: [unterminated\n", encoding="utf-8")
    data = _data_dir(tmp_path)
    slack = _world()
    try:
        rc = main(["sync", "--no-react", "--no-post", *_base_argv(cfg, data)],
                  slack_factory=lambda: slack,
                  detector_factory=lambda: FakeFaceDetector({}))
    except Exception as exc:  # noqa: BLE001 - the break is that an exception escapes at all
        pytest.fail(f"expected controlled exit 2, but main() raised "
                    f"{type(exc).__name__}: {exc}")
    assert rc == int(Exit.CONFIG_INVALID)


# --------------------------------------------------------------------------- #
# Finding 4 — purge prunes the ledger but never rewrites verdicts.jsonl, leaving the
# derived output stale relative to the durable ledger
# --------------------------------------------------------------------------- #

def test_purge_rewrites_verdicts_to_match_pruned_ledger(tmp_path, capsys):
    """40 §5.1 `DOC-VERDICTS-FRESH`: "`verdicts.jsonl` on disk is byte-equal to a fresh
    `dumps_verdicts` of `evaluate` re-run from the durable ledger ... any difference means a
    bug". 40 §4.2 (`restore`): the data files "are restored as one set so the restored
    `verdicts.jsonl` matches the restored ledger" — purge (also a writing command) owes the
    same invariant.

    `_cmd_purge` calls `save_ledger(kept)` and returns without recomputing verdicts, so after
    purging the only snipe's sender the ledger is empty while `verdicts.jsonl` still holds the
    purged message's verdict row — a stale derived output a subsequent `DOC-VERDICTS-FRESH`
    check would fail.
    """
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    slack = _world()
    assert _sync_once(cfg, data, slack) == int(Exit.OK)
    capsys.readouterr()
    rc = main(["purge", "--user", "U0AAAA1", "--rewrite-history", "--yes",
               *_base_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.OK)
    ledger_text = (data / "ledger.jsonl").read_text(encoding="utf-8").strip()
    assert ledger_text == ""  # the only row (sender U0AAAA1) is gone
    verdicts_text = (data / "verdicts.jsonl").read_text(encoding="utf-8")
    assert MSG_TS not in verdicts_text
