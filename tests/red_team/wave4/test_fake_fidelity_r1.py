"""Wave 4, round 1: FakeSlack fidelity against the real Slack API (gate G2 captures).

Each test pins one place where tests/fake_slack.py answers differently from real Slack,
so a sync or rig test driven by the fake can pass while production would behave otherwise.
"""

from __future__ import annotations

import pytest

from tests.fake_slack import FakeSlack, FakeUser

NOW = "1790200000.000000"
CH = "C0MAIN01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"


def _slack() -> FakeSlack:
    return FakeSlack(
        now=NOW,
        bot_user_id="U0BOT01",
        users={
            "U0BOT01": FakeUser(id="U0BOT01", is_bot=True),
            SNIPER: FakeUser(id=SNIPER, display_name="user-1", real_name="user-1"),
            TARGET: FakeUser(id=TARGET, display_name="user-2", real_name="user-2"),
        },
        channels=(CH,),
        bot_member_of=(CH,),
        horizon_days=None,
    )


def _photo(file_id: str = "F0FILE001") -> dict:
    return {
        "id": file_id,
        "mimetype": "image/jpeg",
        "filetype": "jpg",
        "name": "photo-1.jpg",
        "size": 284596,
        "mode": "hosted",
        "file_access": "visible",
        "original_w": 1280,
        "original_h": 960,
        "url_private": f"https://fixture.invalid/files/{file_id}/download/photo.jpg",
        "url_private_download": f"https://fixture.invalid/files/{file_id}/download/photo.jpg?dl=1",
        "thumb_1024": f"https://fixture.invalid/files/{file_id}/thumb_1024.jpg",
        "thumb_960": f"https://fixture.invalid/files/{file_id}/thumb_960.jpg",
        "thumb_720": f"https://fixture.invalid/files/{file_id}/thumb_720.jpg",
        "thumb_480": f"https://fixture.invalid/files/{file_id}/thumb_480.jpg",
        "permalink": None,
        "permalink_public": None,
        "_bytes": b"jpeg-bytes-001",
    }


def test_deleted_file_renders_as_real_file_not_found_stub():
    """Claim: FakeSlack.delete_file keeps the whole original file object (mimetype, name,
    size, every thumb/url) and only adds `is_tombstoned: true` / `mode: "tombstone"`. The real
    G2 capture `tests/fixtures/refetch/file-deleted/after.json` shows Slack instead replaces a
    deleted file with a stub `{id, file_access: "file_not_found", created: 0, timestamp: 0,
    user, filetype}`: no `is_tombstoned`, no `mimetype`, no `name`/`size`, no rendition URLs
    (CAPTURED.md: G2 was to confirm the tombstone flag name; the capture refutes it). Sync and
    rig tests therefore only ever exercise parse's `is_tombstoned` branch, which production
    never reaches, and never the real stub shape.
    """
    slack = _slack()
    ts = slack.post(at="1790200100.000100", user=SNIPER, channel=CH,
                    text=f"got <@{TARGET}>", files=[_photo()])
    slack.delete_file(at="1790200300.000000", ts=ts, channel=CH, file_index=0)
    slack.as_of("1790200400.000000")

    (msg,) = slack.history(CH, oldest="0.000000")
    f = msg["files"][0]
    assert f["id"] == "F0FILE001"
    assert f.get("file_access") == "file_not_found"
    leaked = sorted(k for k in ("is_tombstoned", "mimetype", "name", "size", "thumb_1024",
                                "thumb_480", "url_private_download") if k in f)
    assert leaked == [], f"real deleted-file stub carries none of {leaked}"


def test_edited_ts_carries_a_zero_fraction():
    """Claim: FakeSlack renders `edited.ts` as the exact authored edit instant, fraction
    included. Real Slack returns `edited.ts` with a zero fraction ("1790201457.000000";
    G2 real-API fact 21, captured in `refetch/tag-edited-in/after.json`). The late-tag rule
    (rules._late_tag: edit_ts > ts + edit_grace) and `target_edited_in.edit_ts` read this
    value, so fake-driven tests see a sub-second precision real Slack never gives, including
    an edit in the post's own second whose real `edited.ts` is earlier than the message ts.
    """
    slack = _slack()
    ts = slack.post(at="1790201456.935209", user=SNIPER, channel=CH, text="got")
    slack.edit(at="1790201457.412345", ts=ts, channel=CH, user=SNIPER,
               text=f"got <@{TARGET}>")
    slack.as_of("1790201500.000000")

    (msg,) = slack.history(CH, oldest="0.000000")
    assert msg["edited"]["ts"] == "1790201457.000000"
