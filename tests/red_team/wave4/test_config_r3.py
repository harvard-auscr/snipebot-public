"""Red-team wave 4, round 3: config surface (snipebot/config.py and its callers).

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
from snipebot.config import ConfigError, load_config
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


def _minimal(rules: str = "{}", semesters: str | None = None, extra: str = "") -> str:
    sem = semesters or "  - {name: fall-2026, start: 2026-09-01, end: 2026-12-20}\n"
    return (
        "slack: {channel: C0MAIN01}\n"
        "timezone: UTC\n"
        "semesters:\n"
        + sem
        + f"rules: {rules}\n"
        "players: {extras: [U0AAA001]}\n"
        "consent: {veto: {emoji: x}}\n"
        "feedback: {}\n"
        + extra
    )


def _synced_with_rostered_bot(tmp_path: Path, monkeypatch) -> tuple[Path, Path, FakeSlack]:
    """A clean sync (persistence: files) where the roster lists a real bot user and a
    snipe tags it. Returns (config path, data dir, slack)."""
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write(tmp_path, _minimal(extra="persistence: files\n").replace(
        "players: {extras: [U0AAA001]}",
        "players: {extras: [U0AAA001, U0AAA002, U0BOT01]}",
    ))
    data = tmp_path / "data"
    data.mkdir()
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{BOT}>",
               files=[image_file("F0FILE001", b"snap")])
    rc = main(["sync", "--no-react", "--no-post", "--config", str(cfg), "--data-dir", str(data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    verdicts = (data / "verdicts.jsonl").read_text(encoding="utf-8")
    assert "target_is_bot" in verdicts, "precondition: sync gates the rostered bot target"
    state = json.loads((data / "state.json").read_text(encoding="utf-8"))
    assert state.get("fingerprints", {}).get("players"), "precondition: fingerprints stored"
    return cfg, data, slack


def test_purge_refused_by_guard_after_clean_sync_with_rostered_bot(tmp_path: Path, monkeypatch) -> None:
    """Incomplete repair of the is_bot fix. sync/backfill/veto/... now reload the config with
    the users.list is_bot map, so state.json's `players` fingerprint (00 section 7: it covers
    {"user","join_us","group","is_bot"}) is computed with is_bot=true for a rostered bot.
    _cmd_purge still calls _load_config(args) with no map, so its fingerprint_guard sees
    is_bot=false, reports a `players` change that never happened, and refuses the erasure
    with exit 3. Nothing in config.yaml changed between the sync and the purge. Violates 40
    section 3 (the guard refuses only on a config change that would re-judge rows) and 40
    section 2.1 / 00 section 6 DECISION (is_bot resolved from the users map, not defaulted
    False on a path that compares against stored fingerprints).
    """
    cfg, data, slack = _synced_with_rostered_bot(tmp_path, monkeypatch)
    rc = main(["purge", "--user", SNIPER, "--config", str(cfg), "--data-dir", str(data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0, f"purge refused (exit {rc}) with no config change"


def test_doctor_reports_fresh_verdicts_stale_with_rostered_bot(tmp_path: Path, monkeypatch, capsys) -> None:
    """doctor's _check_config_parse calls load_config(config_path) with no is_bot map, even
    online where it holds a Slack client. After a clean sync whose snipe tagged a rostered
    bot (verdict target_is_bot), doctor re-evaluates with is_bot=false, gets COUNTED, and
    reports DOC-VERDICTS-FRESH FAIL 'verdicts.jsonl is stale' (and DOC-FINGERPRINT-PLAYERS
    'a sync would refuse'), so doctor exits DOCTOR_FAILED on a healthy data dir. Violates 40
    section 2.1 ("`doctor` and `sync` pass a real map").
    """
    cfg, data, slack = _synced_with_rostered_bot(tmp_path, monkeypatch)
    capsys.readouterr()
    main(["doctor", "--json", "--config", str(cfg), "--data-dir", str(data)],
         slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    out = capsys.readouterr().out
    checks = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            obj = json.loads(line)
            checks[obj["id"]] = obj
    assert "DOC-VERDICTS-FRESH" in checks, out
    assert checks["DOC-VERDICTS-FRESH"]["ok"], checks["DOC-VERDICTS-FRESH"]
    assert checks["DOC-FINGERPRINT-PLAYERS"]["ok"], checks["DOC-FINGERPRINT-PLAYERS"]


def test_impossible_bare_date_error_names_no_key(tmp_path: Path) -> None:
    """Incomplete repair: an impossible bare YAML date (start: 2026-02-30) is now mapped to
    InvalidValueError, but the message is the generic 'config: invalid date or date-time
    value' and never names semesters[0].start. With several semesters, members' `from:` and
    dated rules, the owner cannot tell which value is wrong. Violates 40 section 2.2: the
    message names the offending key path (e.g. 'reports[1].weekday'); 40 section 2.2 also
    says the first violation is reported with its key path.
    """
    sem = "  - {name: fall-2026, start: 2026-02-30, end: 2026-12-20}\n"
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, _minimal(semesters=sem)))
    assert "semesters[0].start" in str(exc.value), str(exc.value)
