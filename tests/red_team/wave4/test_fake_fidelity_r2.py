"""Fake-fidelity breaker, round 2: FakeSlack behaviours that disagree with real Slack."""

from __future__ import annotations

import pytest

from snipebot.slack_io import SlackAPIError, SlackHTTPError
from tests.fake_slack import FakeSlack, FakeUser

_CH = "C0MAIN01"
_BOT = "U0BOT01"
_SNIPER = "U0AAA001"


def _slack(now: str) -> FakeSlack:
    users = {
        _BOT: FakeUser(id=_BOT, is_bot=True),
        _SNIPER: FakeUser(id=_SNIPER),
        "U0AAA002": FakeUser(id="U0AAA002"),
    }
    return FakeSlack(now=now, bot_user_id=_BOT, users=users)


def _photo(n: int, body: bytes) -> dict:
    return {
        "id": f"F0FILE{n:03d}",
        "mimetype": "image/jpeg",
        "name": f"photo-{n}.jpg",
        "file_access": "visible",
        "mode": "hosted",
        "original_w": 1280,
        "original_h": 960,
        "thumb_1024": f"https://fixture.invalid/files/{n}/thumb_1024",
        "url_private_download": f"https://fixture.invalid/files/{n}/download",
        "_bytes": body,
    }


def test_update_message_refuses_a_message_the_bot_did_not_post():
    """Claim: FakeSlack.update_message rewrites any message in the channel, whoever
    posted it. Real chat.update with the bot token answers ok:false
    `cant_update_message` for a message the bot did not author (Slack chat.update
    error table). sync.post_digests Pass B revises every `snipe_digest` message it
    finds in history "regardless of sender" (parse step 1 only warns), so against the
    fake a digest-shaped message from anyone else is silently revised and the suite
    passes, while against real Slack the same run fails with an untolerated
    SlackAPIError on every sync until the message is removed.
    Violates: 10 section 5 "Fidelity: reproducing what Slack really exposes" (the
    fake must answer as the real chat.update does) and 10 section 3 (ok:false ->
    SlackAPIError with `error` set)."""
    slack = _slack("1790200000.000000")
    ts = slack.post(
        at="1790199000.000100", user=_SNIPER, channel=_CH, text="standings",
        metadata={"event_type": "snipe_digest",
                  "event_payload": {"report": "daily", "period_key": "daily:2026-09-18",
                                    "channel": _CH, "semester": "fall",
                                    "numbers_hash": "h0", "revision": 0}},
    )
    with pytest.raises(SlackAPIError) as ei:
        slack.update_message(_CH, ts, text="rewritten", blocks=[],
                             metadata={"event_type": "snipe_digest", "event_payload": {}})
    assert getattr(ei.value, "error", None) == "cant_update_message"
    # And the person's message is untouched.
    msg = slack.history(_CH, oldest="0.000000")[0]
    assert msg["text"] == "standings"


def test_fetch_file_bytes_ignores_a_delete_authored_after_now():
    """Claim: FakeSlack.fetch_file_bytes folds the WHOLE event log
    (`_replay(as_of_now=False)`), so a delete_message authored at a later instant
    already 404s the photo while the same message is still live in `history` as of
    now. Real Slack serves the rendition until the delete actually happens. In a
    replay/schedule test whose sender deletes a snipe later, the earlier sync's
    face fetch fails with SlackHTTPError(404) and burns a detect attempt that real
    Slack would have spent on a successful count.
    Violates: 10 section 5 `as_of(now)` semantics, Visibility: "A read method
    reflects only events with at <= now", and the serving-bytes rule that a URL
    404s only "once the underlying object is gone"."""
    body = b"\xff\xd8\xff\xe0fixture-photo-1"
    slack = _slack("1790200000.000000")
    f = _photo(1, body)
    ts = slack.post(at="1790200100.000100", user=_SNIPER, channel=_CH,
                    text="<@U0AAA002>", files=[f])
    slack.delete_message(at="1790200900.000000", ts=ts, channel=_CH)

    slack.as_of("1790200500.000000")            # after the post, before the delete
    live = slack.history(_CH, oldest="0.000000")
    assert [m["ts"] for m in live] == [ts]      # the message is still there
    url = live[0]["files"][0]["thumb_1024"]
    try:
        got = slack.fetch_file_bytes(url)
    except SlackHTTPError as exc:               # fake: future delete already folded
        pytest.fail(f"live photo 404s before its delete: {exc!r}")
    assert got == body

