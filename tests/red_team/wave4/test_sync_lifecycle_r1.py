"""Wave 4, round 1: the sync lifecycle over time (20 §3-§5), driven through `run_sync`
against `FakeSlack` with files-mode persistence under `tmp_path`. Offline and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger, load_state
from snipebot.parse import VetoSource
from snipebot.sync import Command, SyncResult, run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import SEMESTER, make_config, mkts, roster_of

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
ADMIN = "U0AAA009"
THIRD = "U0AAA010"
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
    for uid in (SNIPER, TARGET, ADMIN, THIRD):
        users[uid] = FakeUser(id=uid)
    return FakeSlack(now=now, bot_user_id=BOT, users=users)


def _config(**kw):
    roster = roster_of({SNIPER: None, TARGET: None, ADMIN: None, THIRD: None})
    return make_config(roster=roster, admins=(ADMIN,), **kw)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack: FakeSlack, cfg, tmp_path: Path, now: str, **kw) -> SyncResult:
    slack.as_of(now)
    led, st = _paths(tmp_path)
    return run_sync(
        slack, cfg, detector=FakeFaceDetector({}), ledger_path=led, state_path=st,
        now_us=parse_ts(now), no_post=True, **kw,
    )


def _verdict_status(tmp_path: Path, ts: str) -> str:
    import json
    led, _ = _paths(tmp_path)
    for line in led.with_name("verdicts.jsonl").read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("ts") == ts:
            return obj["status"]
    raise AssertionError("no verdict for the row")


def _row(tmp_path: Path, ts: str):
    led, _ = _paths(tmp_path)
    return next(r for r in load_ledger(led) if r.ts == ts)


def test_backfill_drops_reaction_veto_on_rows_older_than_scan_window(tmp_path):
    """Claim: a `backfill --from <semester start>` re-fetches rows older than the scan window,
    `_replace_facts` strips their REACTION vetoes, and step 5 never rebuilds them (it only
    observes scan-window rows) -- so every admin-vetoed snipe older than `scan_days` counts
    again, although the veto emoji is still on the message. Violates PLAN "What is
    guaranteed" (facts are final after `scan_days` except through the admin CLI) and PLAN §6
    Veto (an admin veto within `scan_days` voids the photo); 20 §4.1 says the REACTION half
    is rebuilt from the latest observation, and that observation still carries the veto."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 2, 12))
    ts = slack.post(at=mkts(2026, 9, 2, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(1)])
    slack.react(at=mkts(2026, 9, 2, 11), ts=ts, channel=CHANNEL, user=ADMIN, name=VETO)
    r1 = _run(slack, cfg, tmp_path, mkts(2026, 9, 2, 12))
    assert r1.exit_code == 0
    assert [(v.by, v.source) for v in _row(tmp_path, ts).vetoes] == [(ADMIN, VetoSource.REACTION)]

    r2 = _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 12), command=Command.BACKFILL,
              backfill_from_us=SEMESTER.start_us, no_react=True)
    assert r2.exit_code == 0
    assert [(v.by, v.source) for v in _row(tmp_path, ts).vetoes] == [(ADMIN, VetoSource.REACTION)]
    assert _verdict_status(tmp_path, ts) != "counted"


def test_optout_reaction_after_scan_window_never_observed(tmp_path):
    """Claim: step 5 reads the opt-out message only if it happens to fall inside this run's
    fetch range, so once the pinned opt-out message is older than `scan_days` (14) -- i.e.
    two weeks into the semester -- a person who reacts to leave is never recorded and keeps
    being scored and named in digests. Violates PLAN §2 step 5 ("each run reads the reactors
    on the opt-out message(s)"), PLAN §6 self-serve exit, the config contract that an opt-out
    message stays in use until it ages past Slack's 90-day horizon ("Re-post and add a ts
    when one ages past 90 days"), and 20 §5.2 step 1, whose only stated cause of absence is
    that 90-day horizon."""
    slack = _world(mkts(2026, 9, 1, 9))
    optout_ts = slack.post(at=mkts(2026, 9, 1, 9), user=ADMIN, channel=CHANNEL,
                           text="react to this message to leave the game")
    cfg = _config(optout_message_ts=(optout_ts,))
    snipe = slack.post(at=mkts(2026, 9, 20, 10), user=SNIPER, channel=CHANNEL,
                       text=f"<@{TARGET}>", files=[_photo(2)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 20, 11)).exit_code == 0

    slack.react(at=mkts(2026, 9, 20, 11, 30), ts=optout_ts, channel=CHANNEL, user=TARGET,
                name="wave")
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 20, 12)).exit_code == 0

    _, st = _paths(tmp_path)
    assert TARGET in load_state(st).opted_out
    assert _verdict_status(tmp_path, snipe) != "counted"


def test_pending_miss_run_drops_reaction_veto_and_counts_vetoed_photo(tmp_path):
    """Claim: on a run where a vetoed in-window row is absent from history (one pending
    miss: a transient absence, or the first run after its delete), `_observe_vetoes` finds no
    raw message and rebuilds `vetoes` from the CLI half alone, so the REACTION veto is
    erased and the photo is judged COUNTED for that run (and would be counted in a digest
    posted by it). Violates 20 §4.2 (an unreturned row only increments `missing_runs`; its
    facts are left unchanged) and 20 §4.1 (the REACTION half is replaced from the latest
    observation -- there is none this run)."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(3)])
    # A text-only survivor keeps the next fetch non-empty (zero returned infers no miss, E-W4-16).
    slack.post(at=mkts(2026, 9, 18, 10, 30), user=SNIPER, channel=CHANNEL, text="hello")
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN, name=VETO)
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12)).exit_code == 0
    assert _verdict_status(tmp_path, ts) != "counted"

    slack.faults.vanish(ts=ts, for_fetches=1)
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 13), no_react=True).exit_code == 0
    row = _row(tmp_path, ts)
    assert row.missing_runs == 1
    assert [(v.by, v.source) for v in row.vetoes] == [(ADMIN, VetoSource.REACTION)]
    assert _verdict_status(tmp_path, ts) != "counted"


def test_deleted_parent_with_replies_tombstone_keeps_counting(tmp_path):
    """Claim: when a snipe that has thread replies is deleted, Slack keeps its ts in
    `conversations.history` as a `subtype: "tombstone"` placeholder (10 §4 step 3 lists
    `tombstone` as a subtype `parse` returns None for). The ts is in `returned_ts`, so the
    row is never missed, and merge's `fresh is None` branch keeps every stale fact
    (targets, live images) instead of replacing them -- the deleted photo stays COUNTED
    forever. Violates 20 §4.1 (facts wholesale-replaced from the latest observation) and
    PLAN §2 step 4 (a deleted message stops counting)."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(4)])
    slack.reply(at=mkts(2026, 9, 18, 10, 5), user=THIRD, channel=CHANNEL, parent_ts=ts,
                text="nice")
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12), no_react=True).exit_code == 0
    assert _verdict_status(tmp_path, ts) == "counted"

    real_history = slack.history

    def history_with_tombstone(channel, oldest, latest=None):
        out = []
        for m in real_history(channel, oldest, latest):
            if m.get("ts") == ts:
                m = {
                    "type": "message", "subtype": "tombstone", "hidden": True,
                    "text": "This message was deleted.", "ts": ts, "thread_ts": ts,
                    "reply_count": 1, "reply_users": [THIRD], "reply_users_count": 1,
                    "latest_reply": mkts(2026, 9, 18, 10, 5),
                }
            out.append(m)
        return out

    slack.history = history_with_tombstone  # type: ignore[method-assign]
    for hour in (13, 14, 15):
        assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, hour),
                    no_react=True).exit_code == 0
    assert _verdict_status(tmp_path, ts) != "counted"
