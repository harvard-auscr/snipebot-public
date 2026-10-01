"""Wave 4 rulings on FakeSlack fidelity: regression tests for E-W4-7, E-W4-8 and E-W4-20d.

Each test pins the fake to the real Slack shape the ruling adopted, so a sync or rig test
driven by the fake exercises the branch production actually reaches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snipebot.parse import parse
from snipebot.slack_io import SlackHTTPError
from tests.fake_slack import FakeSlack, FakeUser

NOW = "1790200000.000000"
CH = "C0MAIN01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
BOT = "U0BOT01"

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _slack() -> FakeSlack:
    return FakeSlack(
        now=NOW,
        bot_user_id=BOT,
        users={
            BOT: FakeUser(id=BOT, is_bot=True),
            SNIPER: FakeUser(id=SNIPER, display_name="user-1", real_name="user-1"),
            TARGET: FakeUser(id=TARGET, display_name="user-2", real_name="user-2"),
        },
        channels=(CH,),
        bot_member_of=(CH,),
        horizon_days=None,
    )


def _photo(file_id: str) -> dict:
    return {
        "id": file_id,
        "mimetype": "image/jpeg",
        "filetype": "jpg",
        "name": "photo-1.jpg",
        "size": 14,
        "mode": "hosted",
        "file_access": "visible",
        "original_w": 1280,
        "original_h": 960,
        "url_private": f"https://fixture.invalid/files/{file_id}/download/photo.jpg",
        "thumb_1024": f"https://fixture.invalid/files/{file_id}/thumb_1024.jpg",
        "_bytes": b"jpeg-bytes-001",
    }


def test_fake_has_no_emoji_list():
    """E-W4-7: DOC-EMOJI-EXISTS is removed, and with it the `emoji_list` seam; the fake
    must not offer a method the real client no longer has."""
    assert not hasattr(FakeSlack, "emoji_list")


def test_deleted_file_matches_captured_stub_keys():
    """E-W4-8: a file removed with `delete_file` renders with exactly the keys of the
    captured deleted-file stub (refetch/file-deleted/after.json)."""
    real = json.loads((_FIXTURES / "refetch" / "file-deleted" / "after.json")
                      .read_text(encoding="utf-8"))["files"][0]
    slack = _slack()
    ts = slack.post(at="1790200100.000100", user=SNIPER, channel=CH,
                    text=f"got <@{TARGET}>", files=[_photo("F0FILE001")])
    slack.delete_file(at="1790200300.000000", ts=ts, channel=CH)
    slack.as_of("1790200400.000000")

    (msg,) = slack.history(CH, oldest="0.000000")
    f = msg["files"][0]
    assert set(f) == set(real)
    assert f["file_access"] == "file_not_found"
    assert (f["created"], f["timestamp"], f["filetype"]) == (0, 0, "jpg")


def test_deleted_file_stub_drops_out_of_parse_and_bytes():
    """E-W4-8: parse counts the fake's deleted-file stub as no live image, and its old
    rendition URL serves a 404, as with a real deletion."""
    slack = _slack()
    ts = slack.post(at="1790200100.000100", user=SNIPER, channel=CH,
                    text=f"got <@{TARGET}>",
                    files=[_photo("F0FILE001"), _photo("F0FILE002")])
    slack.delete_file(at="1790200300.000000", ts=ts, channel=CH, file_index=1)
    slack.as_of("1790200400.000000")

    (msg,) = slack.history(CH, oldest="0.000000")
    cand = parse(msg, CH, BOT)
    assert cand.live_images == 1
    assert cand.live_image_ids == ("F0FILE001",)
    with pytest.raises(SlackHTTPError) as excinfo:
        slack.fetch_file_bytes("https://fixture.invalid/files/F0FILE002/thumb_1024.jpg")
    assert excinfo.value.status == 404
    assert slack.fetch_file_bytes(
        "https://fixture.invalid/files/F0FILE001/thumb_1024.jpg") == b"jpeg-bytes-001"


def test_edit_in_the_posts_own_second_reports_an_earlier_edited_ts():
    """E-W4-20d: `edited.ts` carries a zero fraction (G2 fact 21), so an edit in the
    post's own second reports an `edited.ts` earlier than the message ts."""
    slack = _slack()
    ts = slack.post(at="1790201456.935209", user=SNIPER, channel=CH, text="got")
    slack.edit(at="1790201456.990000", ts=ts, channel=CH, user=SNIPER,
               text=f"got <@{TARGET}>")
    slack.as_of("1790201500.000000")

    (msg,) = slack.history(CH, oldest="0.000000")
    assert msg["edited"] == {"user": SNIPER, "ts": "1790201456.000000"}
