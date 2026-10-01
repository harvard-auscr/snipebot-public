"""Breaker tests for the weekly `export.yml` workflow (40 section 7.4, E-W4-40).

Each test replays the workflow's two commands (`roster` then `export`) offline, in a fresh
runner layout: the committed data files under `_data/data`, no users.json until `roster`
writes one. A FakeSlack world stands in for Slack; nothing runs git or the network.
"""

from __future__ import annotations

import calendar
import shutil
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.faces import FakeFaceDetector
from snipebot.ts import US_PER_SECOND

from tests.fake_slack import FakeSlack, FakeUser
from tests.red_team.wave4.test_rulings_cli import (
    BOT, CHANNEL, SNIPER, TARGET, TARGET3, _config_dict, _ts, _users, _world,
    fixed_clock,  # noqa: F401
)


def _us(y, mo, d, h=0) -> int:
    return calendar.timegm((y, mo, d, h, 0, 0, 0, 0, 0)) * US_PER_SECOND


def _synced_runner_layout(tmp_path: Path, cfg_dict: dict) -> tuple[Path, Path]:
    """Sync one counted snipe of TARGET3 (a rostered extra) into a files-mode data dir,
    then copy the committed files into a fresh `_data/data` (no users.json)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(cfg_dict, sort_keys=False), encoding="utf-8")
    synced = tmp_path / "synced"
    synced.mkdir()
    slack = _world([(_ts(2026, 9, 18, 9), TARGET3)])
    assert main(["sync", "--no-react", "--no-post", "--config", str(cfg),
                 "--data-dir", str(synced)],
                slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({})) == 0
    data = tmp_path / "work" / "_data" / "data"
    data.mkdir(parents=True)
    for f in synced.iterdir():
        if f.is_file() and f.name != "users.json":
            shutil.copy2(f, data / f.name)
    return cfg, data


def _roster_then_export(cfg: Path, data: Path, out_dir: Path, slack: FakeSlack) -> list[str]:
    argv = ["--config", str(cfg), "--data-dir", str(data)]
    assert main(["roster", *argv], slack_factory=lambda: slack) == int(Exit.OK)
    assert main(["export", "--out", str(out_dir), *argv]) == int(Exit.OK)
    return sorted(p.name for p in out_dir.iterdir())


@pytest.mark.parametrize("how", ["left_channel", "deactivated"])
def test_export_names_a_rostered_player_who_is_no_longer_a_channel_member(
        tmp_path, capsys, fixed_clock, how):
    """The workflow's only name source is `roster`, which caches names for current,
    non-deleted channel members. A rostered player with counted snipes who has since left
    the channel (or whose account was deactivated) is still in the standings, and the
    digest path (sync's users.list cache) names them; the weekly export shows the raw
    `[U...]` fallback instead."""
    cfg, data = _synced_runner_layout(tmp_path, _config_dict(extras=[TARGET3]))
    users = _users()
    if how == "left_channel":
        members = {CHANNEL: [SNIPER, TARGET, BOT]}
    else:
        users[TARGET3] = FakeUser(id=TARGET3, display_name="user-3", deleted=True)
        members = None
    slack = FakeSlack(now=_ts(2026, 9, 20, 12), bot_user_id=BOT, users=users,
                      channel_members=members)
    out_dir = tmp_path / "work" / "exports"
    _roster_then_export(cfg, data, out_dir, slack)

    people = (out_dir / "fall-2026_people.csv").read_text(encoding="utf-8-sig")
    assert f"[{TARGET3}]" not in people, people
    assert "user-3" in people, people


def test_export_between_semesters_exports_the_one_that_just_ended(
        tmp_path, capsys, monkeypatch, fixed_clock):
    """With the next semester already configured, a scheduled run over the break picks the
    latest-ENDING semester, one that has not started: the artifact is six header-only CSVs
    and an empty workbook, the job is green, and the ended semester's final standings (its
    last days after the final in-semester Sunday) are never produced by the workflow, whose
    dispatch takes no semester input."""
    cfg_dict = _config_dict(extras=[TARGET3])
    cfg_dict["semesters"] = [
        {"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"},
        {"name": "spring-2027", "start": "2027-01-25", "end": "2027-05-15"},
    ]
    cfg, data = _synced_runner_layout(tmp_path, cfg_dict)
    monkeypatch.setattr(cli, "_now_us", lambda: _us(2026, 12, 28, 0))   # a break-week Monday
    slack = FakeSlack(now=_ts(2026, 12, 28), bot_user_id=BOT, users=_users())
    out_dir = tmp_path / "work" / "exports"
    files = _roster_then_export(cfg, data, out_dir, slack)

    assert "fall-2026.xlsx" in files, files
    people = (out_dir / "fall-2026_people.csv").read_text(encoding="utf-8-sig")
    assert "user-3" in people, people


def test_export_falls_back_to_the_bracketed_id_for_a_member_without_a_name(
        tmp_path, capsys, fixed_clock):
    """30 section 4: an unresolved ID prints as `[U...]`, and sync's users.list cache
    stores "" for a nameless user so the digest does exactly that. `roster` instead caches
    the raw ID itself as the "name", so the export prints a bare `U0AAA003` that reads as
    a display name and disagrees with the digest."""
    cfg, data = _synced_runner_layout(tmp_path, _config_dict(extras=[TARGET3]))
    users = _users()
    users[TARGET3] = FakeUser(id=TARGET3)                  # no display or real name
    slack = FakeSlack(now=_ts(2026, 9, 20, 12), bot_user_id=BOT, users=users)
    out_dir = tmp_path / "work" / "exports"
    _roster_then_export(cfg, data, out_dir, slack)

    people = (out_dir / "fall-2026_people.csv").read_text(encoding="utf-8-sig")
    rows = [ln.split(",") for ln in people.splitlines()[1:]]
    assert [r[0] for r in rows if TARGET3 in r[0]] == [f"[{TARGET3}]"], people
