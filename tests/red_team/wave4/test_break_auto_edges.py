"""Breaker tests for the automatic roster (E-W4-42) at its edges: sync end to end over a
FakeSlack world and the offline doctor. Each test pins one defect. (Probed and found
sound: the listed -> auto switch on a synced ledger converging after `sync --reevaluate`,
a player deactivated after their snipes, sync's logs and output holding no display
names, and a grouped entry dated after H, which under auto dates only the group and so
leaves every row <= H as it was.)

Offline only: config files and a FakeSlack world under tmp_path, files persistence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snipebot import cli
from snipebot.cli import main

from tests.fake_slack import FakeSlack
from tests.red_team.wave4.test_rulings_auto_roster import (
    BOT, LOOSE, NOW_TS, ROBOT, S2, SIB1,
    _argv, _cli, _Clock, _photo, _users, _verdicts, _write,
)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(NOW_TS)
    monkeypatch.setattr(cli, "_now_us", lambda: c.us)
    return c


def _sync(slack: FakeSlack, cfg: Path, data: Path, *extra: str) -> int:
    return _cli(slack, "sync", "--no-react", "--no-post", *extra, *_argv(cfg, data))


# --------------------------------------------------------------------------- doctor

def test_offline_doctor_under_listed_still_judges_a_rostered_bot_from_the_cache(
        tmp_path, clock, capsys):
    """listed is unchanged byte for byte, and 40 section 2 has an offline doctor judge a
    rostered bot with the users.json is_bot map (E-W4-18). A freshly synced listed ledger
    with a TARGET_IS_BOT pair must read fresh offline, with a matching players
    fingerprint; doctor now consults the cache only under players.mode auto."""
    cfg = _write(tmp_path / "config.yaml", mode="listed", extras=[LOOSE, ROBOT])
    data = tmp_path / "data"
    data.mkdir()
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=_users(newbie=False))
    _photo(slack, S2, SIB1, ROBOT, LOOSE)
    assert _sync(slack, cfg, data) == 0
    pairs = {p["target"]: p["reason"] for p in _verdicts(data)[S2]["pairs"]}
    assert pairs[ROBOT] == "target_is_bot"
    cache = json.loads((data / "users.json").read_text(encoding="utf-8"))
    assert cache[ROBOT]["is_bot"] is True
    capsys.readouterr()
    main(["doctor", "--offline", "--json", *_argv(cfg, data)])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()
            if line.startswith("{")]
    checks = {r["id"]: r for r in rows}
    assert checks["DOC-FINGERPRINT-PLAYERS"]["ok"], checks["DOC-FINGERPRINT-PLAYERS"]
    assert checks["DOC-VERDICTS-FRESH"]["ok"], checks["DOC-VERDICTS-FRESH"]
