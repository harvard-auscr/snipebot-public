"""Wave 4, round 3: the sync lifecycle over time (20 §3-§8, 40 §4.3), driven through
`run_sync` (and the CLI entry point) against `FakeSlack` with files-mode persistence under
`tmp_path`. Offline and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger, load_state
from snipebot.sync import Command, SyncResult, run_sync
from snipebot.ts import US_PER_SECOND, parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import make_config, mkts, roster_of, secs, us

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
TARGET2 = "U0AAA003"
ADMIN = "U0AAA009"


def _photo(n: int) -> dict:
    data = f"photo-bytes-{n}".encode()
    return {
        "id": f"F0FILE{n:03d}",
        "mimetype": "image/jpeg",
        "name": f"photo-{n}.jpg",
        "size": len(data),
        "original_w": 100,
        "original_h": 100,
        "thumb_1024": f"https://fixture.invalid/thumb/{n}",
        "url_private_download": f"https://fixture.invalid/dl/{n}",
        "_bytes": data,
    }


def _world(now: str) -> FakeSlack:
    users = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in (SNIPER, TARGET, TARGET2, ADMIN):
        users[uid] = FakeUser(id=uid, display_name=uid)
    return FakeSlack(now=now, bot_user_id=BOT, users=users)


def _config(**kw):
    roster = roster_of({SNIPER: None, TARGET: None, TARGET2: None, ADMIN: None})
    return make_config(roster=roster, admins=(ADMIN,), **kw)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack: FakeSlack, cfg, tmp_path: Path, now: str, **kw) -> SyncResult:
    slack.as_of(now)
    led, st = _paths(tmp_path)
    kw.setdefault("no_post", True)
    kw.setdefault("no_react", True)
    return run_sync(
        slack, cfg, detector=FakeFaceDetector({}), ledger_path=led, state_path=st,
        now_us=parse_ts(now), **kw,
    )


def _ledger_ts(tmp_path: Path) -> list[str]:
    led, _ = _paths(tmp_path)
    return [r.ts for r in load_ledger(led)]


# --------------------------------------------------------------------------- #
# 1. A narrow backfill abandons the watermark's gap recovery
# --------------------------------------------------------------------------- #

def test_narrow_backfill_after_outage_abandons_watermark_gap(tmp_path):
    """Claim: `backfill --from X` fetches only [X, now] yet still sets
    `state.watermark = format_ts(now_us)`. When X is later than the stored watermark (a
    backfill of the last few days run after an outage longer than `scan_days`), the stretch
    [old watermark, X) was never observed, but the watermark now says it was. The next sync
    fetches only `scan_days` back, so a snipe posted early in the outage is never recorded
    and never scored. Without the backfill, the next sync would have recovered it from the
    old watermark. Violates 20 §3: the watermark is "the instant up to which the channel has
    been fully observed", the term that "pulls `oldest` further back to the last complete
    observation for recovery" after a gap > `scan_days`."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 2, 12))
    # Normal operation, then the scheduler stops on 2026-09-02.
    slack.post(at=mkts(2026, 9, 2, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(1)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 2, 12)).exit_code == 0

    # During the 23-day outage: a snipe on 09-04 (more than scan_days before recovery).
    lost = slack.post(at=mkts(2026, 9, 4, 10), user=SNIPER, channel=CHANNEL,
                      text=f"<@{TARGET2}>", files=[_photo(2)])

    # Recovery on 09-25: an admin backfills the last few days first ...
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 12), command=Command.BACKFILL,
             backfill_from_us=us(2026, 9, 22))
    assert r.exit_code == 0
    # ... then the schedule resumes.
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 13)).exit_code == 0

    assert lost in _ledger_ts(tmp_path)


# --------------------------------------------------------------------------- #
# 2. A quiet files-mode sync prints a bare `moved:` block, not `no change`
# --------------------------------------------------------------------------- #

def _cli_config(tmp_path: Path) -> Path:
    doc = {
        "enabled": True,
        "persistence": "files",
        "slack": {"channel": CHANNEL},
        "timezone": "UTC",
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 80,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-18"}],
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
        "players": {"count_intra_group": True, "groups": {},
                    "extras": [SNIPER, TARGET, TARGET2, ADMIN]},
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
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def test_files_mode_quiet_sync_later_prints_bare_moved_instead_of_no_change(
        tmp_path, monkeypatch, capsys):
    """Claim: under `persistence: files`, a scheduled sync ten minutes after the last one,
    with nothing new in the channel, rewrites state.json (the watermark moves) and returns
    `ledger_written=True` with empty `moved_lines`. `_print_write_outcome` then prints
    `pushed local only` and a bare `moved: ` line with nothing after it, instead of
    `no change`. (tests/test_cli.py only covers a rerun at the SAME instant, where no byte
    changes.) Violates 40 §4.3: "When `moved_lines` is empty (nothing moved) the block prints
    `no change` ... under both persistence modes" and "a run that changed nothing prints
    `no change` ... exactly as under git"."""
    cfg = _cli_config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    now = [secs(2026, 9, 18, 12) * US_PER_SECOND]
    monkeypatch.setattr(cli, "_now_us", lambda: now[0])
    slack = _world(mkts(2026, 9, 18, 12))
    slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(3)])
    argv = ["sync", "--no-react", "--no-post", "--config", str(cfg), "--data-dir", str(data)]

    assert cli.main(argv, slack_factory=lambda: slack,
                    detector_factory=lambda: FakeFaceDetector({})) == 0
    capsys.readouterr()

    now[0] = secs(2026, 9, 18, 12, 10) * US_PER_SECOND
    slack.as_of(mkts(2026, 9, 18, 12, 10))
    assert cli.main(argv, slack_factory=lambda: slack,
                    detector_factory=lambda: FakeFaceDetector({})) == 0
    out = capsys.readouterr().out
    lines = [ln.rstrip() for ln in out.splitlines()]
    assert "moved:" not in lines          # no bare, empty moved line
    assert "no change" in lines


# --------------------------------------------------------------------------- #
# 3. A crash between the ledger and state renames wedges every rerun on the guard
# --------------------------------------------------------------------------- #

class _Killed(Exception):
    """Stands in for SIGKILL between two os.replace calls of step 8."""


def test_files_mode_crash_between_ledger_and_state_writes_trips_guard_forever(
        tmp_path, monkeypatch):
    """Claim: under `persistence: files` step 8 writes ledger.jsonl, verdicts.jsonl and
    state.json with three separate os.replace calls. A kill after the ledger rename but
    before the state rename leaves the new ledger (newest row H2) beside the old
    fingerprints (computed at H1). When a dated roster addition (`from:`) lies between H1
    and H2, every rerun recomputes the players fingerprint at H2, finds it differs from the
    stored H1 value, and refuses with exit 3 ("use --reevaluate"), although no config
    changed. The run never converges without a manual large movement. Violates 20 §2.2
    (killing at any point and re-running yields the uninterrupted run's ledger; "persistence
    is a single atomic rename ... never partially applied") and 20 §2.4 (the guard refuses
    only a config change that would re-judge existing rows)."""
    from snipebot.config import Roster, RosterEntry
    import snipebot.sync as sync_mod

    join_us = us(2026, 9, 10)          # TARGET2 joins on 09-10 (`from:` in config)
    roster = Roster(entries={
        SNIPER: RosterEntry(user=SNIPER, join_us=0, group=None, is_bot=False),
        TARGET: RosterEntry(user=TARGET, join_us=0, group=None, is_bot=False),
        TARGET2: RosterEntry(user=TARGET2, join_us=join_us, group=None, is_bot=False),
        ADMIN: RosterEntry(user=ADMIN, join_us=0, group=None, is_bot=False),
    }, count_intra_group=True)
    cfg = make_config(roster=roster, admins=(ADMIN,))

    slack = _world(mkts(2026, 9, 8, 12))
    slack.post(at=mkts(2026, 9, 8, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(6)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 8, 12)).exit_code == 0

    slack.post(at=mkts(2026, 9, 11, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET2}>", files=[_photo(7)])

    def _killed(*_a, **_k):
        raise _Killed()

    monkeypatch.setattr(sync_mod, "save_state", _killed)
    with pytest.raises(_Killed):
        _run(slack, cfg, tmp_path, mkts(2026, 9, 11, 12))
    monkeypatch.undo()

    # The rerun after the crash, with an unchanged config, must converge.
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 11, 12, 10))
    assert r.exit_code == 0


# --------------------------------------------------------------------------- #
# 4. Two zero-message fetches delete every in-range row
# --------------------------------------------------------------------------- #

def _verdict(tmp_path: Path, ts: str) -> dict:
    led, _ = _paths(tmp_path)
    for line in led.with_name("verdicts.jsonl").read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("ts") == ts:
            return obj
    raise AssertionError("no verdict for the row")


def test_zero_message_fetches_delete_rows(tmp_path):
    """Claim: delete inference has no zero-message guard. When `conversations.history`
    comes back complete but empty (an ok:true page with `messages: []` and no `has_more`)
    on two consecutive runs, every stored row inside the fetch range takes a miss on each
    run, crosses `missing_runs` 1 -> 2 and is judged DELETED. With no more rows than
    `max_deletes_per_run`, the breaker does not trip. The snipes stop counting, and a row
    that then ages below the scan floor is dropped from the ledger for good. Violates the
    50 §2 property `L2-PR-delete-safety` ("zero-message fetch deletes nothing") and PLAN
    "Delete safety" ("a run that returns zero messages deletes nothing")."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 18, 12))
    a = slack.post(at=mkts(2026, 9, 17, 10), user=SNIPER, channel=CHANNEL,
                   text=f"<@{TARGET}>", files=[_photo(8)])
    b = slack.post(at=mkts(2026, 9, 17, 11), user=SNIPER, channel=CHANNEL,
                   text=f"<@{TARGET2}>", files=[_photo(9)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12)).exit_code == 0
    assert _verdict(tmp_path, a)["status"] == "counted"

    # Two runs in which history returns no message at all.
    for ts in (a, b):
        slack.faults.vanish(ts=ts, for_fetches=3)
    # (this probe consumes one of the three empty fetches)
    assert slack.history(CHANNEL, mkts(2026, 9, 1), mkts(2026, 9, 18, 12, 5)) == []
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12, 10)).exit_code == 0
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12, 20)).exit_code == 0

    for ts in (a, b):
        v = _verdict(tmp_path, ts)
        assert (v["status"], v.get("reason")) == ("counted", "counted"), v.get("reason")
