"""Round-3 red team, surface rules-scoring: the TARGET_IS_BOT gate across the offline
commands.

`sync` resolves `RosterEntry.is_bot` from `users.list` (40 section 2: "`doctor` and
`sync` pass a real map"), so a rostered bot target is judged TARGET_IS_BOT and the stored
`players` fingerprint (00 section 7) carries `"is_bot": true`. Every test below syncs such
a world once, then runs a second command that loads the config WITHOUT the map, so the
same roster is judged with `is_bot == False`: the second command disagrees with sync about
the verdicts and the players fingerprint.

Offline: FakeSlack only, persistence: files, tmp_path data dirs.
"""

from __future__ import annotations

import calendar
import json
import types
from pathlib import Path

import pytest
import yaml

from snipebot import cli, doctor
from snipebot.cli import Exit, main
from snipebot.faces import FakeFaceDetector
from snipebot.ts import US_PER_SECOND

from tests.controls import slack_fixtures
from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
ADMIN = "U0AAA009"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 10)


class _World(FakeSlack):
    """FakeSlack plus the two duck-typed extras doctor's Slack block calls."""

    def auth_scopes(self):
        return frozenset(slack_fixtures.FULL_BOT_SCOPES)

    def emoji_list(self):
        return {}


def _config_dict() -> dict:
    return {
        "enabled": True,
        "persistence": "files",
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
        # The bot is rostered (an extra), so the TARGET_IS_BOT gate is reachable.
        "players": {"count_intra_group": True,
                    "groups": {"fam": [SNIPER, TARGET]}, "extras": [BOT]},
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


def _setup(tmp_path: Path) -> tuple[Path, Path, _World]:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(_config_dict(), sort_keys=False), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2"),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9"),
    }
    slack = _World(now=NOW_TS, bot_user_id=BOT, users=users)
    # One photo tagging a sib and the rostered bot: sib pair COUNTED, bot pair TARGET_IS_BOT.
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}> <@{BOT}>", files=[image_file("F0FILE001", b"photo-1")])
    return cfg, data, slack


def _argv(cfg: Path, data: Path) -> list[str]:
    return ["--config", str(cfg), "--data-dir", str(data)]


def _sync(cfg: Path, data: Path, slack) -> None:
    rc = main(["sync", "--no-react", "--no-post", *_argv(cfg, data)],
              slack_factory=lambda: slack,
              detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.OK)
    # Precondition: sync judged the bot pair with the real is_bot map.
    verdicts = (data / "verdicts.jsonl").read_text(encoding="utf-8")
    assert "target_is_bot" in verdicts


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)


def test_purge_not_refused_by_guard_when_a_bot_is_rostered(tmp_path, capsys):
    """Claim: with a rostered bot, `purge` can never run. `_cmd_purge` loads the config with
    `_load_config(args)` (no is_bot map), so every roster entry has `is_bot == False`, and
    its `fingerprint_guard` compares that against the players fingerprint the last sync
    stored (computed with the real map, `"is_bot": true`). The fingerprints differ although
    the config never changed, so purge exits GUARD_REFUSED instead of erasing the user.

    Violates 40 section 2 (`is_bot` "from the users cache (users.json / users.list)";
    sync passes a real map) together with 00 section 7 (the `players` fingerprint includes
    `is_bot`; the guard fires only on a config change that re-judges rows <= H) and
    40 section 4.2 (`purge --user U` erases the user and exits 0).
    """
    cfg, data, slack = _setup(tmp_path)
    _sync(cfg, data, slack)
    capsys.readouterr()
    rc = main(["purge", "--user", TARGET, *_argv(cfg, data)],
              slack_factory=lambda: slack,
              detector_factory=lambda: FakeFaceDetector({}))
    err = capsys.readouterr().err
    assert rc == int(Exit.OK), f"purge refused (rc={rc}): {err.strip()}"


def test_doctor_verdicts_fresh_after_sync_with_rostered_bot(tmp_path, capsys):
    """Claim: right after a clean sync, online `doctor` FAILs DOC-VERDICTS-FRESH (exit 10)
    and WARNs DOC-FINGERPRINT-PLAYERS "a sync would refuse" whenever a rostered bot was
    tagged. `doctor._check_config_parse` calls `load_config(config_path)` with no is_bot
    map, so its fresh `evaluate` scores the bot pair COUNTED where sync wrote TARGET_IS_BOT,
    and its players fingerprint has `"is_bot": false` against the stored `true`.

    Violates 40 section 2 (`load_config`: "`doctor` and `sync` pass a real map") and
    40 section 5.1 DOC-VERDICTS-FRESH (a difference "means a bug": here the data is
    healthy and the difference comes from doctor's own config load).
    """
    cfg, data, slack = _setup(tmp_path)
    _sync(cfg, data, slack)
    capsys.readouterr()
    args = types.SimpleNamespace(config=str(cfg), data_dir=str(data), offline=False,
                                 json=True)
    doctor.run(args, slack_factory=lambda: slack, now_us=NOW_US)
    records = {}
    for line in capsys.readouterr().out.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and "id" in rec:
            records[rec["id"]] = rec
    fresh = records.get("DOC-VERDICTS-FRESH")
    players = records.get("DOC-FINGERPRINT-PLAYERS")
    assert fresh is not None and players is not None, sorted(records)
    assert fresh["ok"] is True, fresh
    assert players["ok"] is True, players
