"""Wave 4 red-team, round 3, surface "doctor" (snipebot/doctor.py and its CLI
wiring).

Each test drives doctor against a tmp_path config and data dir, with an
offline FakeSlack world where the Slack block is needed. Each test fails on
the current code for the reason in its docstring. No network.
"""

from __future__ import annotations

import calendar
import types
from pathlib import Path

import pytest
import yaml

import snipebot.cli as cli
import snipebot.doctor as doctor
from snipebot.faces import FakeFaceDetector
from snipebot.ts import US_PER_SECOND
from tests._helpers_sync import image_file
from tests.controls import slack_fixtures
from tests.fake_slack import FakeSlack, FakeUser

_CHANNEL = "C0MAIN01"
_BOT = "U0BOT01"
_ADMIN = "U0AAA009"
_SNIPER = "U0AAA001"
_TARGET = "U0AAA002"
_BOT_TARGET = "U0AAA003"  # a rostered integration user: users.list says is_bot true


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.000000"


_NOW_TS = _ts(2026, 9, 18, 12)
_NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND


def _config_dict(*, groups=None, extras=None) -> dict:
    return {
        "enabled": True,
        "persistence": "files",
        "slack": {"channel": _CHANNEL},
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
        "players": {
            "count_intra_group": True,
            "groups": groups if groups is not None else {},
            "extras": extras if extras is not None else [],
        },
        "consent": {
            "veto": {"emoji": "no_entry_sign", "by": ["admins"]},
            "optout_messages": [],
            "opted_out": [],
        },
        "admins": [_ADMIN],
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


def _write_config(tmp_path: Path, cfg: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


class _Slack(FakeSlack):
    """FakeSlack plus doctor's two duck-typed extras (auth_scopes, emoji_list)."""

    def auth_scopes(self):
        return frozenset(slack_fixtures.full_granted_scopes())

    def emoji_list(self):
        return {"white_check_mark", "hourglass_flowing_sand", "x", "question",
                "no_entry_sign"}


def _find(out: str, check_id: str) -> str:
    for line in out.splitlines():
        if line.startswith(check_id + " "):
            return line
    raise AssertionError(f"{check_id} not printed; got:\n{out}")


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now_us", lambda: _NOW_US)


# --------------------------------------------------------------------------- #
# 1. doctor re-evaluates with is_bot=False for every rostered user, so it
#    contradicts the verdicts and fingerprints a real sync just wrote.
# --------------------------------------------------------------------------- #

def test_doctor_verdicts_fresh_fails_after_sync_with_a_rostered_bot(
    tmp_path: Path, capsys,
) -> None:
    """40-config-cli.md section 2 (load_config docstring): '`doctor` and `sync`
    pass a real [is_bot] map'; 00-data.md section 5 DECISION: RosterEntry.is_bot
    'resolves bot-ness at config/`doctor` time'. Since the round-2 repair, sync
    loads the config through cli._load_config_with_bots, so a rostered user
    whom users.list reports as is_bot true gets TARGET_IS_BOT (allow_bots
    false) in verdicts.jsonl, and the players fingerprint in state.json carries
    is_bot true. doctor._check_config_parse calls load_config(path) with no
    is_bot map even when the Slack block runs, so its re-evaluation scores
    the same snipe COUNTED: DOC-VERDICTS-FRESH FAILs (exit 10) on a data dir
    that sync itself just verified byte-equal, and DOC-FINGERPRINT-PLAYERS
    warns 'a sync would refuse' although the next sync does not refuse."""
    cfg = _config_dict(extras=[_SNIPER, _TARGET, _BOT_TARGET])
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    users = {
        _BOT: FakeUser(id=_BOT, is_bot=True),
        _SNIPER: FakeUser(id=_SNIPER, display_name="user-1"),
        _TARGET: FakeUser(id=_TARGET, display_name="user-2"),
        _BOT_TARGET: FakeUser(id=_BOT_TARGET, is_bot=True, display_name="user-3"),
        _ADMIN: FakeUser(id=_ADMIN, display_name="user-9"),
    }
    slack = _Slack(
        now=_NOW_TS, bot_user_id=_BOT, users=users,
        channels=[_CHANNEL], bot_member_of=[_CHANNEL],
        channel_members={_CHANNEL: [_SNIPER, _TARGET, _BOT_TARGET, _ADMIN, _BOT]},
    )
    slack.post(at=_ts(2026, 9, 18, 10), user=_SNIPER, channel=_CHANNEL,
               text=f"<@{_BOT_TARGET}>", files=[image_file("F0FILE001", b"snap")])

    argv = ["--config", str(config_path), "--data-dir", str(data_dir)]
    rc = cli.main(["sync", "--no-post", "--no-react", *argv],
                  slack_factory=lambda: slack,
                  detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    verdicts_text = (data_dir / "verdicts.jsonl").read_text(encoding="utf-8")
    assert "target_is_bot" in verdicts_text.lower(), verdicts_text  # sync saw the bot
    capsys.readouterr()

    args = types.SimpleNamespace(
        config=str(config_path), data_dir=str(data_dir), offline=False, json=False,
    )
    doctor.run(args, slack_factory=lambda: slack, now_us=_NOW_US)
    out = capsys.readouterr().out

    assert _find(out, "DOC-VERDICTS-FRESH") == "DOC-VERDICTS-FRESH PASS", out
    assert _find(out, "DOC-FINGERPRINT-PLAYERS") == "DOC-FINGERPRINT-PLAYERS PASS", out


# --------------------------------------------------------------------------- #
# 2. DOC-FINGERPRINT-GROUPS tells the operator a sync would refuse; it never does.
# --------------------------------------------------------------------------- #

def test_doctor_groups_fingerprint_warn_claims_a_refusal_sync_never_makes(
    tmp_path: Path, capsys,
) -> None:
    """40-config-cli.md section 5.1: DOC-FINGERPRINT-GROUPS is a WARN for a
    'mid-semester group change'; config.fingerprint_guard (40 section 3) never
    refuses on a groups-only difference ('A groups-only difference never
    refuses (doctor warns)'). _check_fingerprints nevertheless gives every
    mismatch, the groups one included, the detail 'fingerprint mismatch (a sync
    would refuse)'. After a regroup the operator is told the scheduled sync
    will stop (and may run `sync --reevaluate` to push through a refusal that
    was never coming), while the real sync in this test runs and exits 0."""
    old_cfg = _config_dict(groups={"g1": [_SNIPER, _TARGET]}, extras=[_BOT_TARGET])
    config_path = _write_config(tmp_path, old_cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    users = {
        _BOT: FakeUser(id=_BOT, is_bot=True),
        _SNIPER: FakeUser(id=_SNIPER, display_name="user-1"),
        _TARGET: FakeUser(id=_TARGET, display_name="user-2"),
        _BOT_TARGET: FakeUser(id=_BOT_TARGET, display_name="user-3"),
        _ADMIN: FakeUser(id=_ADMIN, display_name="user-9"),
        "U0AAA004": FakeUser(id="U0AAA004", display_name="user-4"),
    }
    slack = _Slack(
        now=_NOW_TS, bot_user_id=_BOT, users=users,
        channels=[_CHANNEL], bot_member_of=[_CHANNEL],
        channel_members={_CHANNEL: [_SNIPER, _TARGET, _BOT_TARGET, _ADMIN, _BOT]},
    )
    slack.post(at=_ts(2026, 9, 18, 10), user=_SNIPER, channel=_CHANNEL,
               text=f"<@{_BOT_TARGET}>", files=[image_file("F0FILE001", b"snap")])
    argv = ["--config", str(config_path), "--data-dir", str(data_dir)]
    factory = dict(slack_factory=lambda: slack,
                   detector_factory=lambda: FakeFaceDetector({}))
    assert cli.main(["sync", "--no-post", "--no-react", *argv], **factory) == 0

    # Mid-semester group change: a late joiner (from: after the newest row, so
    # outside the players fingerprint) is added to g1 -- groups-only.
    late = {"id": "U0AAA004", "from": "2026-09-18 11:00"}
    new_cfg = _config_dict(groups={"g1": [_SNIPER, _TARGET, late]}, extras=[_BOT_TARGET])
    _write_config(tmp_path, new_cfg)
    capsys.readouterr()

    args = types.SimpleNamespace(
        config=str(config_path), data_dir=str(data_dir), offline=True, json=False,
    )
    doctor.run(args, now_us=_NOW_US)
    out = capsys.readouterr().out
    groups_line = _find(out, "DOC-FINGERPRINT-GROUPS")
    assert groups_line.startswith("DOC-FINGERPRINT-GROUPS WARN"), out

    # The sync the WARN forecasts as a refusal is not refused.
    assert cli.main(["sync", "--no-post", "--no-react", *argv], **factory) == 0
    assert "refuse" not in groups_line, groups_line
