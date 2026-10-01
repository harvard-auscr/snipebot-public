"""Red-team wave 4, round 2: config surface (snipebot/config.py, config.example.yaml).

Each test states one defect in its docstring with the spec section it violates.
Offline and deterministic: config files are written under tmp_path, Slack is a FakeSlack.
"""

from __future__ import annotations

import calendar
import json
import textwrap
from pathlib import Path

import pytest

from snipebot import cli
from snipebot.cli import main
from snipebot.config import InvalidValueError, load_config
from snipebot.faces import FakeFaceDetector
from snipebot.ts import US_PER_SECOND

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


NOW_TS = f"{_secs(2026, 9, 18, 12)}.000000"
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = f"{_secs(2026, 9, 18, 10)}.000000"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _minimal(timezone: str = "UTC", extra: str = "") -> str:
    return (
        "slack: {channel: C0MAIN01}\n"
        f"timezone: {timezone}\n"
        "semesters:\n"
        "  - {name: fall-2026, start: 2026-09-01, end: 2026-12-20}\n"
        "rules: {}\n"
        "players: {extras: [U0AAA001]}\n"
        "consent: {veto: {emoji: x}}\n"
        "feedback: {}\n"
        + extra
    )


def test_sync_never_passes_is_bot_so_rostered_bot_target_counts(tmp_path: Path, monkeypatch) -> None:
    """A snipe tagging a ROSTERED bot counts under allow_bots: false. cli._load_config calls
    load_config(path) with no is_bot map, so every RosterEntry.is_bot is False and
    evaluate's TARGET_IS_BOT gate (rules.py, roster.is_bot) can never fire on the real
    sync path. Violates 40 section 2.1 ("doctor and sync pass a real map"), 40 section 1.6
    (is_bot comes from the users map passed to load_config) and 00 section 6 DECISION
    (RosterEntry.is_bot resolved at config time from the users cache): the pair must be
    NOT_COUNTED with reason target_is_bot, not COUNTED.
    """
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write(tmp_path, _minimal(extra="persistence: files\n").replace(
        "players: {extras: [U0AAA001]}",
        "players: {extras: [U0AAA001, U0BOT01]}",
    ))
    data = tmp_path / "data"
    data.mkdir()
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{BOT}>",
               files=[image_file("F0FILE001", b"snap")])

    rc = main(["sync", "--no-react", "--no-post", "--config", str(cfg), "--data-dir", str(data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    rows = [json.loads(line) for line in (data / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert len(rows) == 1
    text = json.dumps(rows[0])
    assert "target_is_bot" in text, f"rostered bot target was not gated: {text}"


def test_lowercase_timezone_loads_on_case_insensitive_filesystem(tmp_path: Path) -> None:
    """timezone: utc (or est, cet, zulu, US/eastern) is not an IANA name, yet load_config
    accepts it on Windows: ZoneInfo reads top-level tzdata resources through the
    case-insensitive filesystem, so the config validates on the owner's machine and then
    fails with 'unknown zone' on the Linux Actions runner (case-sensitive zoneinfo), where
    every scheduled sync exits 2. Violates 40 section 1 scalar format Tz ("an IANA name")
    and the deploy requirement that a config that loads locally loads on the runner.
    """
    with pytest.raises(InvalidValueError, match=r"^timezone"):
        load_config(_write(tmp_path, _minimal(timezone="utc")))


def test_enabled_error_message_names_a_dotted_key(tmp_path: Path) -> None:
    """enabled: "false" (a string) raises with the key path '.enabled' (a stray leading dot,
    from _get_bool(root, 'enabled', True, '')), not 'enabled'. Violates 40 section 2.2:
    the message names the offending key path (e.g. 'reports[1].weekday').
    """
    with pytest.raises(InvalidValueError) as exc:
        load_config(_write(tmp_path, _minimal(extra='enabled: "false"\n')))
    assert str(exc.value).startswith("enabled:"), str(exc.value)
