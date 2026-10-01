"""Wave 4, round 2: the sync lifecycle over time (20 §3-§6), driven through `run_sync`
against `FakeSlack` with files-mode persistence under `tmp_path`. Offline and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snipebot.config import Cadence, ReportSpec, Section
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger
from snipebot.parse import VetoSource
from snipebot.sync import Command, SyncResult, run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import SEMESTER, make_config, mkts, roster_of, us

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
ADMIN = "U0AAA009"
VETO = "no_entry_sign"


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
    for uid in (SNIPER, TARGET, ADMIN):
        users[uid] = FakeUser(id=uid, display_name=uid)
    return FakeSlack(now=now, bot_user_id=BOT, users=users)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack: FakeSlack, cfg, tmp_path: Path, now: str, **kw) -> SyncResult:
    slack.as_of(now)
    led, st = _paths(tmp_path)
    kw.setdefault("no_post", True)
    detector = kw.pop("detector", None) or FakeFaceDetector({})
    return run_sync(
        slack, cfg, detector=detector, ledger_path=led, state_path=st,
        now_us=parse_ts(now), **kw,
    )


def _row(tmp_path: Path, ts: str):
    led, _ = _paths(tmp_path)
    return next(r for r in load_ledger(led) if r.ts == ts)


def _verdict_status(tmp_path: Path, ts: str) -> str:
    led, _ = _paths(tmp_path)
    for line in led.with_name("verdicts.jsonl").read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("ts") == ts:
            return obj["status"]
    raise AssertionError("no verdict for the row")


def _digest_posts(slack: FakeSlack) -> list[dict]:
    return [
        e for e in slack._events
        if e["kind"] == "post"
        and (e["data"].get("metadata") or {}).get("event_type") == "snipe_digest"
    ]


def test_go_live_backfill_ignores_admin_veto_on_message_first_seen_below_scan_floor(tmp_path):
    """Claim: the documented go-live step (PLAN §11 phase 6: a real `backfill --from
    <semester start> --no-react --no-post` on a fresh data dir) creates every row older than
    `scan_days` for the first time, and step 5 (`_observe_vetoes`) skips every row below the
    scan floor, so an admin's veto reaction placed on a snipe in the first weeks (day 0, well
    inside the message's own `scan_days`) is never recorded and the vetoed photo is scored
    COUNTED forever. The same happens to messages first seen after an outage longer than
    `scan_days` (watermark gap recovery, 20 §3). Violates PLAN §6 Veto (an admin veto within
    `scan_days` voids the photo), PLAN "What is guaranteed" (facts track Slack for
    `scan_days` after posting) and 20 §4.1 (the REACTION half of `vetoes` is built from the
    latest observation, which here carries the veto)."""
    roster = roster_of({SNIPER: None, TARGET: None, ADMIN: None})
    cfg = make_config(roster=roster, admins=(ADMIN,))
    slack = _world(mkts(2026, 9, 2, 12))
    ts = slack.post(at=mkts(2026, 9, 2, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(1)])
    slack.react(at=mkts(2026, 9, 2, 11), ts=ts, channel=CHANNEL, user=ADMIN, name=VETO)

    # Go-live 23 days later on a fresh data dir: the first ever run is the real backfill.
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 12), command=Command.BACKFILL,
             backfill_from_us=SEMESTER.start_us, no_react=True)
    assert r.exit_code == 0
    assert [(v.by, v.source) for v in _row(tmp_path, ts).vetoes] == \
        [(ADMIN, VetoSource.REACTION)]
    assert _verdict_status(tmp_path, ts) != "counted"


def test_backfill_from_recent_point_reposts_digest_already_in_channel(tmp_path):
    """Claim: digest dedup reads only the `snipe_digest` messages returned by this run's
    watched-channel fetch, and `backfill --from <ts|date>` replaces the whole fetch range
    with `[from, now]`. A backfill from a point after the last digest but within its 24 h
    window (e.g. `backfill --from 2026-09-19` at 08:00 the next morning) no longer sees the
    21:00 digest, so Pass A posts the same period a second time into the channel. Violates
    20 §6 / §6.2 Pass A ("already posted" is derived from the channel posted to, keyed on
    `(channel, period_key)`; a period is posted once) and 20 §3 ("the watched channel
    already covers reports with no `post_to`")."""
    roster = roster_of({SNIPER: None, TARGET: None, ADMIN: None})
    reports = (ReportSpec(
        name="daily", cadence=Cadence.DAILY, at_hour=21, at_minute=0,
        weekday=None, post_to=None, sections=(Section.DAY,), top_n=5,
    ),)
    cfg = make_config(roster=roster, admins=(ADMIN,), reports=reports)
    slack = _world(mkts(2026, 9, 18, 21, 30))
    slack.post(at=mkts(2026, 9, 18, 12), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(3)])

    r1 = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 21, 30), no_react=True,
              no_post=False)
    assert r1.exit_code == 0 and r1.digests_posted == 1

    r2 = _run(slack, cfg, tmp_path, mkts(2026, 9, 19, 8), command=Command.BACKFILL,
              backfill_from_us=us(2026, 9, 19), no_react=True, no_post=False)
    assert r2.exit_code == 0
    assert len(_digest_posts(slack)) == 1


def test_files_mode_quiet_sync_reports_counted_flip_that_did_not_happen(tmp_path):
    """Claim: under `persistence: files` the baseline is always empty, and the watermark
    makes `state.json` differ on every run, so a sync ten minutes after the last one, with
    nothing new in the channel, still returns `moved_lines == ("counted +1",)` and the CLI
    prints `moved: counted +1`: it reports a verdict flip that did not happen, on every
    scheduled run. Violates 20 §1.3 (`moved_lines` is empty when nothing moved, and empty
    under `persistence: files`, where nothing is committed) and 40 §4.3 (the `moved:` line is
    this run's verdict flips by reason; a run that changed nothing prints `no change`)."""
    roster = roster_of({SNIPER: None, TARGET: None, ADMIN: None})
    cfg = make_config(roster=roster, admins=(ADMIN,))
    slack = _world(mkts(2026, 9, 18, 12))
    slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(4)])
    r1 = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12), no_react=True)
    assert r1.exit_code == 0

    r2 = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12, 10), no_react=True)
    assert r2.exit_code == 0
    assert r2.moved_lines == ()
