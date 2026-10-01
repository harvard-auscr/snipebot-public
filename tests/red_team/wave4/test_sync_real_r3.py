"""Wave 4, round 3, surface "sync-real": run_sync and the commands that share its data files,
fed with FakeSlack and the real captured fixture shapes. Offline and deterministic; files-mode
persistence under tmp_path, no git, no network.

Each test states one defect in its docstring, with the spec section it violates.
"""

from __future__ import annotations

import calendar
import json
import textwrap
from pathlib import Path

from snipebot import cli
from snipebot.cli import main
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
MSG2_TS = f"{_secs(2026, 9, 18, 10, 30)}.000000"


def _config_text() -> str:
    return textwrap.dedent(
        """\
        slack: {channel: C0MAIN01}
        timezone: UTC
        persistence: files
        semesters:
          - {name: fall-2026, start: 2026-09-01, end: 2026-12-20}
        rules: {}
        players: {extras: [U0AAA001, U0AAA002, U0BOT01]}
        consent: {veto: {emoji: x}}
        admins: [U0AAA009]
        feedback: {}
        """
    )


def _rostered_bot_world(tmp_path: Path, monkeypatch):
    """A roster that lists the bot user (allow_bots false by default), one photo that tags the
    bot and one that tags a person, and a real sync (which loads the users.list is_bot map)."""
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config_text(), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"got <@{BOT}>",
               files=[image_file("F0FILE001", b"snap-1")])
    slack.post(at=MSG2_TS, user=TARGET, channel=CHANNEL, text=f"got <@{SNIPER}>",
               files=[image_file("F0FILE002", b"snap-2")])
    rc = main(["sync", "--no-react", "--no-post", "--config", str(cfg), "--data-dir", str(data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    verdicts = (data / "verdicts.jsonl").read_text(encoding="utf-8")
    assert "target_is_bot" in verdicts          # the round-2 repair is in effect for sync
    return cfg, data, slack


def test_purge_is_refused_by_the_guard_after_a_sync_with_a_rostered_bot(tmp_path, monkeypatch,
                                                                        capsys):
    """Claim: once sync writes state.json with the users.list is_bot map (the round-2 repair
    `_load_config_with_bots`), `purge` can never run while a bot is on the roster. `_cmd_purge`
    loads the config with no is_bot map, so its players fingerprint (which includes `is_bot`,
    00-data section 11) differs from the one sync stored, and `fingerprint_guard` refuses with
    exit 3 although no config change is pending. The one privacy command that erases a person
    is blocked. Violates 40 section 2.1 (`is_bot` from the users map; sync and doctor pass a
    real map, so every command that compares against sync's fingerprints must resolve it the
    same way) and 40 section 3 (the guard trips only on a real config change)."""
    cfg, data, _slack = _rostered_bot_world(tmp_path, monkeypatch)
    capsys.readouterr()

    rc = main(["purge", "--user", TARGET, "--config", str(cfg), "--data-dir", str(data)])
    err = capsys.readouterr().err
    assert rc == int(cli.Exit.OK), (
        f"purge refused (exit {rc}) with no config change pending: {err.strip()}"
    )


def test_doctor_reports_stale_verdicts_after_a_sync_with_a_rostered_bot(tmp_path, monkeypatch,
                                                                       capsys):
    """Claim: after a normal sync with a bot on the roster, an online `doctor` reports
    DOC-VERDICTS-FRESH as FAIL (and the players fingerprint as a mismatch). doctor loads the
    config with no is_bot map, so its fresh evaluate scores the bot-tagged photo COUNTED where
    sync (which now passes the users.list map) wrote NOT_COUNTED target_is_bot. doctor exits
    DOCTOR_FAILED on a healthy data dir. Violates 40 section 2.1 load_config docstring
    ("`doctor` and `sync` pass a real map") and 40 section 5 DOC-VERDICTS-FRESH (verdicts.jsonl
    equals a fresh evaluate over the ledger with the resolved config)."""
    cfg, data, slack = _rostered_bot_world(tmp_path, monkeypatch)
    capsys.readouterr()

    main(["doctor", "--config", str(cfg), "--data-dir", str(data)],
         slack_factory=lambda: slack)
    out = capsys.readouterr().out
    fresh = next((ln for ln in out.splitlines() if ln.startswith("DOC-VERDICTS-FRESH")), "")
    players = next((ln for ln in out.splitlines() if ln.startswith("DOC-FINGERPRINT-PLAYERS")),
                   "")
    assert fresh.startswith("DOC-VERDICTS-FRESH PASS"), fresh or out
    assert players.startswith("DOC-FINGERPRINT-PLAYERS PASS"), players or out
