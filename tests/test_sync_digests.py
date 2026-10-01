"""Digest posting tests (20 §6, step 9): `run_sync` end to end against `FakeSlack` and
the files-mode backend, exercising `post_digests` through the real state machine with
`no_react=True`. Covers Pass A (post the due period, dedup, out-of-window/not-due skip,
`post_to` routing) and Pass B (revise on a numbers change, no-op when unchanged), plus
the untolerated `DigestTooLargeError` -> exit 1 and the `digest:before`/`digest:after`
boundaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snipebot import sync
from snipebot.config import Cadence, ReportSpec, Section
from snipebot.faces import FakeFaceDetector
from snipebot.report import DigestTooLargeError
from snipebot.sync import SyncResult, run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import (
    BOT,
    CHANNEL,
    data_paths,
    image_file,
    make_config,
    mkts,
    roster_of,
)

OFFICERS = "C0OFFICER"
ADMIN = "U0ADMIN"


def _users(*ids: str) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in ids:
        out[uid] = FakeUser(id=uid, display_name=uid)
    return out


def _run(slack, config, tmp_path, *, now, **kw) -> SyncResult:
    led, st = data_paths(tmp_path)
    return run_sync(
        slack, config, detector=FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now), no_react=True, **kw,
    )


def _digest_posts(slack) -> list[dict]:
    """Every `post_message` that carried snipe_digest metadata, in order."""
    return [
        e for e in slack._events
        if e["kind"] == "post"
        and (e["data"].get("metadata") or {}).get("event_type") == "snipe_digest"
    ]


def _digest_edits(slack) -> list[dict]:
    """Every `update_message` (chat.update) that carried snipe_digest metadata."""
    return [
        e for e in slack._events
        if e["kind"] == "edit"
        and (e["data"].get("metadata") or {}).get("event_type") == "snipe_digest"
    ]


def _payload(event: dict) -> dict:
    return event["data"]["metadata"]["event_payload"]


def _world(now, *, reports=None, channels=(CHANNEL,), member_of=(CHANNEL,)):
    """A one-snipe world (U0A tags U0B with an image on 2026-09-18) that produces a
    single counted DAY snipe, plus the resolved config and slack client."""
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, reports=reports)
    slack = FakeSlack(now=now, bot_user_id=BOT, users=_users("U0A", "U0B", ADMIN),
                      channels=channels, bot_member_of=member_of)
    slack.post(at=mkts(2026, 9, 18, 12), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"snapA")])
    return cfg, slack


# --- Pass A: post the due period --------------------------------------------

def test_due_daily_posts_once_with_metadata_and_text(tmp_path):
    now = mkts(2026, 9, 18, 21, 30)          # 30 min past the 21:00 daily anchor
    cfg, slack = _world(now)

    result = _run(slack, cfg, tmp_path, now=now)

    assert result.digests_posted == 1
    assert result.digests_revised == 0
    posts = _digest_posts(slack)
    assert len(posts) == 1
    post = posts[0]
    assert post["channel"] == CHANNEL
    assert post["data"]["text"] == "Snipes: 2026-09-18"
    payload = _payload(post)
    assert payload["report"] == "daily"
    assert payload["period_key"] == "daily:2026-09-18"
    assert payload["channel"] == CHANNEL
    assert payload["semester"] == "fall-2026"
    assert payload["revision"] == 0
    assert len(payload["numbers_hash"]) == 64      # sha256 hex
    # revision 0 carries no _revised_ marker.
    assert "_revised_" not in str(post["data"]["blocks"])


def test_second_run_unchanged_numbers_posts_nothing(tmp_path):
    now1 = mkts(2026, 9, 18, 21, 30)
    cfg, slack = _world(now1)
    _run(slack, cfg, tmp_path, now=now1)

    now2 = mkts(2026, 9, 18, 21, 40)
    slack.as_of(now2)
    result = _run(slack, cfg, tmp_path, now=now2)

    assert result.digests_posted == 0          # already present in the channel -> Pass A skips
    assert result.digests_revised == 0         # numbers unchanged -> Pass B no-op
    assert len(_digest_posts(slack)) == 1       # still just the one from run 1
    assert _digest_edits(slack) == []


def test_changed_numbers_revises_with_incremented_revision_and_marker(tmp_path):
    now1 = mkts(2026, 9, 18, 21, 30)
    cfg, slack = _world(now1)
    _run(slack, cfg, tmp_path, now=now1)

    # A second counted snipe changes the standings, so numbers_hash differs.
    slack.post(at=mkts(2026, 9, 18, 13), user="U0B", channel=CHANNEL,
               text="<@U0A>", files=[image_file("F02", b"snapB")])
    now2 = mkts(2026, 9, 18, 21, 50)
    slack.as_of(now2)
    result = _run(slack, cfg, tmp_path, now=now2)

    assert result.digests_posted == 0          # not re-posted (Pass A dedup)
    assert result.digests_revised == 1
    edits = _digest_edits(slack)
    assert len(edits) == 1
    edit = edits[0]
    assert edit["channel"] == CHANNEL
    assert edit["ts"] == _digest_posts(slack)[0]["ts"]   # updates the same message
    assert _payload(edit)["revision"] == 1      # 0 -> +1 on the numbers-changing update
    assert "_revised_" in str(edit["data"]["blocks"])


def test_post_to_routes_to_officer_channel_and_dedups_there(tmp_path):
    now1 = mkts(2026, 9, 18, 21, 30)
    report = ReportSpec(
        name="daily", cadence=Cadence.DAILY, at_hour=21, at_minute=0,
        weekday=None, post_to=OFFICERS, sections=(Section.DAY,), top_n=5,
    )
    cfg, slack = _world(now1, reports=(report,),
                        channels=(CHANNEL, OFFICERS), member_of=(CHANNEL, OFFICERS))

    r1 = _run(slack, cfg, tmp_path, now=now1)
    assert r1.digests_posted == 1
    posts = _digest_posts(slack)
    assert len(posts) == 1
    assert posts[0]["channel"] == OFFICERS      # routed to post_to, not the watched channel
    assert _payload(posts[0])["channel"] == OFFICERS

    now2 = mkts(2026, 9, 18, 21, 40)
    slack.as_of(now2)
    r2 = _run(slack, cfg, tmp_path, now=now2)
    assert r2.digests_posted == 0               # dedups on the officer channel it lives in
    assert r2.digests_revised == 0
    assert len(_digest_posts(slack)) == 1


def test_period_not_yet_due_posts_nothing(tmp_path):
    # Before the semester's first daily anchor (2026-09-01 21:00): nothing is due.
    now = mkts(2026, 9, 1, 8, 0)
    cfg, slack = _world(now)
    result = _run(slack, cfg, tmp_path, now=now)

    assert result.digests_posted == 0
    assert result.digests_revised == 0
    assert _digest_posts(slack) == []


# --- suppression flags -------------------------------------------------------

def test_no_post_posts_nothing(tmp_path):
    now = mkts(2026, 9, 18, 21, 30)
    cfg, slack = _world(now)
    result = _run(slack, cfg, tmp_path, now=now, no_post=True)

    assert result.digests_posted == 0
    assert result.digests_revised == 0
    assert _digest_posts(slack) == []


def test_dry_run_posts_nothing(tmp_path):
    now = mkts(2026, 9, 18, 21, 30)
    cfg, slack = _world(now)
    result = _run(slack, cfg, tmp_path, now=now, dry_run=True)

    assert result.exit_code == 0
    assert result.digests_posted == 0
    assert result.digests_revised == 0
    assert _digest_posts(slack) == []


# --- untolerated failure -----------------------------------------------------

def test_digest_too_large_maps_to_exit_1(tmp_path, monkeypatch):
    now = mkts(2026, 9, 18, 21, 30)
    cfg, slack = _world(now)

    def _boom(*args, **kwargs):
        raise DigestTooLargeError("digest exceeds Block Kit limits")

    monkeypatch.setattr(sync, "render_digest", _boom)
    result = _run(slack, cfg, tmp_path, now=now)

    assert result.exit_code == 1
    assert result.ledger_written is True        # the ledger persisted before step 9
    assert result.digests_posted == 0
    assert _digest_posts(slack) == []


# --- boundaries --------------------------------------------------------------

def test_digest_boundaries_fire_around_each_post(tmp_path, monkeypatch):
    now = mkts(2026, 9, 18, 21, 30)
    cfg, slack = _world(now)

    seen: list[tuple[str, str | None]] = []
    real_boundary = sync._boundary

    def _spy(name: str, key: str | None = None) -> None:
        seen.append((name, key))
        return real_boundary(name, key)

    monkeypatch.setattr(sync, "_boundary", _spy)
    _run(slack, cfg, tmp_path, now=now)

    digest_boundaries = [b for b in seen
                         if b[0] in ("digest:before", "digest:after", "after_digests")]
    bkey = f"{CHANNEL}:daily:2026-09-18"
    assert digest_boundaries == [
        ("digest:before", bkey),
        ("digest:after", bkey),
        ("after_digests", None),
    ]
