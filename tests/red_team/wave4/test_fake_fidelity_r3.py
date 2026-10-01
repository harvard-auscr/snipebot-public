"""Fake-fidelity breaker, round 3: FakeSlack behaviours that disagree with real Slack
or with the fake's own spec (10 section 5-7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snipebot.slack_io import SlackAPIError, SlackHTTPError
from tests.fake_slack import FakeSlack, FakeUser, FileBackedFakeSlack

_CH = "C0MAIN01"
_BOT = "U0BOT01"
_SNIPER = "U0AAA001"
_TARGET = "U0AAA002"
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _slack(now: str, **kw) -> FakeSlack:
    users = {
        _BOT: FakeUser(id=_BOT, is_bot=True),
        _SNIPER: FakeUser(id=_SNIPER),
        _TARGET: FakeUser(id=_TARGET),
    }
    return FakeSlack(now=now, bot_user_id=_BOT, users=users, **kw)


def _photo(n: int, body: bytes) -> dict:
    return {
        "id": f"F0FILE{n:03d}",
        "mimetype": "image/jpeg",
        "name": f"photo-{n}.jpg",
        "size": len(body),
        "file_access": "visible",
        "mode": "hosted",
        "original_w": 1280,
        "original_h": 960,
        "thumb_1024": f"https://fixture.invalid/files/{n}/thumb_1024",
        "url_private_download": f"https://fixture.invalid/files/{n}/download",
        "_bytes": body,
    }


def test_fetch_file_bytes_ignores_an_edit_authored_after_now():
    """Claim: the round-2 repair of fetch_file_bytes is incomplete. `_find_file_bytes`
    still replays with as_of_now=False and only holds back FUTURE delete events
    (deletes_as_of_now=True); every future `edit` is folded in. A test that drives the
    captured file-deleted shape the way 10 section 5 prescribes ("a test copies a file
    object out of a fixture") edits the message's files to the refetch/file-deleted
    stub at a later instant. Before that instant `history` still shows the live photo
    with its thumb URL, yet fetch_file_bytes already answers 404, so the earlier
    sync's face fetch fails and burns a detect attempt that real Slack (which serves
    the rendition until the file is actually removed) would have spent on a count.
    Violates: 10 section 5 `as_of(now)` Visibility ("A read method reflects only
    events with at <= now") and the serving-bytes rule (404 only "once the
    underlying object is gone")."""
    stub = json.loads((_FIXTURES / "refetch" / "file-deleted" / "after.json")
                      .read_text(encoding="utf-8"))["files"]
    body = b"\xff\xd8\xff\xe0fixture-photo-7"
    slack = _slack("1790200000.000000")
    ts = slack.post(at="1790200100.000100", user=_SNIPER, channel=_CH,
                    text=f"<@{_TARGET}>", files=[_photo(7, body)])
    # Later, the file is removed: the message now carries the captured stub.
    slack.edit(at="1790200900.000000", ts=ts, channel=_CH, user=_SNIPER, files=stub)

    slack.as_of("1790200500.000000")            # after the post, before the removal
    live = slack.history(_CH, oldest="0.000000")
    assert [m["ts"] for m in live] == [ts]
    url = live[0]["files"][0]["thumb_1024"]     # history still serves the live photo
    try:
        got = slack.fetch_file_bytes(url)
    except SlackHTTPError as exc:               # fake: the future edit is already folded
        pytest.fail(f"live photo 404s before the file is removed: {exc!r}")
    assert got == body


def test_vanish_is_not_spent_on_a_fetch_that_failed():
    """Claim: the `vanish` fault is consumed inside `_history_candidates`, before the
    pagination loop, so a `history` call that then raises (fail_mid_page) still
    spends one of the vanish's `for_fetches`. The next complete fetch shows the
    message again. A sync that aborts on a failed fetch (exit 5, nothing written)
    therefore never observes the arranged miss on its next, successful run: the
    missing_runs/two-miss delete inference and the transient-absence paths are
    exercised one fetch short, so a test combining the two faults passes without
    the absence ever reaching sync.
    Violates: 10 section 6 `vanish` exact semantics: "message ts is omitted from
    history for the next `for_fetches` COMPLETE fetches, then reappears"."""
    slack = _slack("1790200000.000000")
    ts = slack.post(at="1790199000.000100", user=_SNIPER, channel=_CH,
                    text=f"<@{_TARGET}>", files=[_photo(8, b"photo-8")])
    slack.faults.vanish(ts=ts, for_fetches=1)
    slack.faults.fail_mid_page(after_pages=0, error="internal_error", times=1)

    with pytest.raises(SlackAPIError):
        slack.history(_CH, oldest="0.000000")   # aborted fetch: not a complete fetch

    first_complete = slack.history(_CH, oldest="0.000000")
    assert ts not in [m["ts"] for m in first_complete], (
        "the vanish was spent on the failed fetch; the first complete fetch shows it")
    second_complete = slack.history(_CH, oldest="0.000000")
    assert ts in [m["ts"] for m in second_complete]


def test_file_backed_reload_keeps_an_empty_channel_members_map(tmp_path):
    """Claim: to_world_dict writes `members: null` for every channel that has no key
    in channel_members, and from_world_dict turns an all-null map back into
    channel_members=None ("every FakeUser is in every channel"). A world built with
    channel_members={} (no human is in any channel) therefore reloads as "everyone
    is in every channel": conversations_members answers [bot] in memory but every
    user id through FileBackedFakeSlack, so the same doctor/members check passes or
    fails depending on which fake it runs against.
    Violates: 10 section 5 "FileBackedFakeSlack preserves channel_members across
    reloads (section 7)" and the fake's own comment that a reloaded world answers
    identically."""
    seed = _slack("1790200000.000000", channel_members={})
    direct = seed.conversations_members(_CH)
    assert direct == [_BOT]

    path = tmp_path / "world.json"
    path.write_text(json.dumps(seed.to_world_dict(), ensure_ascii=True,
                               separators=(",", ":")), encoding="utf-8")
    backed = FileBackedFakeSlack(path=str(path)).conversations_members(_CH)
    assert backed == direct
