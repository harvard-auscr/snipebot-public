"""Fidelity self-tests for FakeSlack / FileBackedFakeSlack (fake-slack workstream,
spec 10-slack-io.md sections 5-7). Exercises the fake exactly as `sync` would:
through the SlackIO surface, with the authoring API building ground truth."""

from __future__ import annotations

import json

import pytest

from snipebot.slack_io import (
    AlreadyReacted,
    ChannelNotFound,
    FileTooLarge,
    MessageNotFound,
    NoReaction,
    NotInChannel,
    RateLimited,
    SlackAPIError,
    SlackHTTPError,
    SlackPaginationError,
    SlackTransportError,
)

from tests.fake_slack import FakeSlack, FakeUser, FileBackedFakeSlack

NOW = "1758210000.000000"


def _basic_slack(**overrides) -> FakeSlack:
    kwargs = {
        "now": NOW,
        "users": {
            "U0BOT": FakeUser(id="U0BOT", is_bot=True),
            "U01AAA": FakeUser(id="U01AAA", display_name="sniper", real_name="sniper"),
            "U02AAA": FakeUser(id="U02AAA", display_name="target", real_name="target"),
        },
        "channels": ("C0MAIN01",),
        "bot_member_of": ("C0MAIN01",),
        "horizon_days": 90,
    }
    kwargs.update(overrides)
    return FakeSlack(**kwargs)


def _photo_file(file_id: str = "F0FILE001", *, payload: bytes = b"jpeg-bytes-001") -> dict:
    return {
        "id": file_id,
        "mimetype": "image/jpeg",
        "filetype": "jpg",
        "name": "photo-1.jpg",
        "size": len(payload),
        "url_private": f"https://fixture.invalid/files/{file_id}/download/photo.jpg",
        "url_private_download": f"https://fixture.invalid/files/{file_id}/download/photo.jpg?dl=1",
        "thumb_1024": f"https://fixture.invalid/files/{file_id}/thumb_1024.jpg",
        "thumb_960": f"https://fixture.invalid/files/{file_id}/thumb_960.jpg",
        "thumb_720": f"https://fixture.invalid/files/{file_id}/thumb_720.jpg",
        "thumb_480": f"https://fixture.invalid/files/{file_id}/thumb_480.jpg",
        "permalink": None,
        "permalink_public": None,
        "_bytes": payload,
    }


# -- edits collapse ------------------------------------------------------------

def test_edits_collapse_to_latest():
    slack = _basic_slack()
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="v1")
    slack.edit(at="1758210001.000000", ts="1758210000.000100", channel="C0MAIN01",
               user="U01AAA", text="v2")
    slack.edit(at="1758210002.000000", ts="1758210000.000100", channel="C0MAIN01",
               user="U01AAA", text="v3")
    slack.as_of("1758210003.000000")

    msgs = slack.history("C0MAIN01", oldest="0.000000")
    assert len(msgs) == 1
    assert msgs[0]["text"] == "v3"
    assert msgs[0]["edited"] == {"user": "U01AAA", "ts": "1758210002.000000"}


def test_intermediate_edit_unobservable_before_it_lands():
    slack = _basic_slack()
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="v1")
    slack.edit(at="1758210005.000000", ts="1758210000.000100", channel="C0MAIN01",
               user="U01AAA", text="v2")
    slack.as_of("1758210003.000000")  # before the edit lands

    msgs = slack.history("C0MAIN01", oldest="0.000000")
    assert msgs[0]["text"] == "v1"
    assert "edited" not in msgs[0]


# -- deletions -------------------------------------------------------------

def test_deleted_message_absent_from_history_after_delete_instant():
    slack = _basic_slack()
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="gone soon")
    slack.delete_message(at="1758210010.000000", ts="1758210000.000100", channel="C0MAIN01")

    slack.as_of("1758210005.000000")
    assert len(slack.history("C0MAIN01", oldest="0.000000")) == 1

    slack.as_of("1758210010.000000")
    assert slack.history("C0MAIN01", oldest="0.000000") == []


def test_deleted_message_raises_message_not_found_on_reactions_get():
    slack = _basic_slack()
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="x")
    slack.delete_message(at="1758210010.000000", ts="1758210000.000100", channel="C0MAIN01")
    slack.as_of("1758210010.000000")
    with pytest.raises(MessageNotFound):
        slack.reactions_get("C0MAIN01", "1758210000.000100")


# -- deleted files -----------------------------------------------------------

def test_delete_file_stubs_file_but_keeps_message():
    slack = _basic_slack()
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01",
               text="got <@U02AAA>", files=[_photo_file()], subtype="file_share")
    slack.delete_file(at="1758210010.000000", ts="1758210000.000100", channel="C0MAIN01",
                       file_index=0)
    slack.as_of("1758210010.000000")

    msgs = slack.history("C0MAIN01", oldest="0.000000")
    assert len(msgs) == 1
    f = msgs[0]["files"][0]
    # The real deleted-file stub (refetch/file-deleted/after.json).
    assert f == {"id": f["id"], "file_access": "file_not_found", "created": 0,
                 "timestamp": 0, "user": "U01AAA", "filetype": "jpg"}
    assert msgs[0]["text"] == "got <@U02AAA>"


# -- horizon -----------------------------------------------------------------

def test_horizon_hides_old_message_from_history_and_reactions_get():
    slack = _basic_slack(horizon_days=90)
    old_ts = "1750000000.000000"  # well over 90 days before NOW
    slack.post(at=old_ts, user="U01AAA", channel="C0MAIN01", text="ancient")

    assert slack.history("C0MAIN01", oldest="0.000000") == []
    with pytest.raises(MessageNotFound):
        slack.reactions_get("C0MAIN01", old_ts)


def test_reactions_add_remove_past_horizon_raise_message_not_found():
    slack = _basic_slack(horizon_days=90)
    old_ts = "1750000000.000000"  # well over 90 days before NOW
    slack.post(at=old_ts, user="U01AAA", channel="C0MAIN01", text="ancient")

    with pytest.raises(MessageNotFound):
        slack.reactions_add("C0MAIN01", old_ts, "x")
    with pytest.raises(MessageNotFound):
        slack.reactions_remove("C0MAIN01", old_ts, "x")


def test_set_horizon_fault_changes_cutoff_live():
    slack = _basic_slack(horizon_days=None)
    old_ts = "1750000000.000000"
    slack.post(at=old_ts, user="U01AAA", channel="C0MAIN01", text="ancient")
    assert len(slack.history("C0MAIN01", oldest="0.000000")) == 1

    slack.faults.set_horizon(days=90)
    assert slack.history("C0MAIN01", oldest="0.000000") == []


# -- reaction truncation + reactions_get completeness ------------------------

def test_reaction_users_truncated_in_history_but_complete_via_reactions_get():
    slack = _basic_slack(reaction_users_limit=1)
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="x")
    slack.react(at="1758210001.000000", ts="1758210000.000100", channel="C0MAIN01",
                user="U01AAA", name="x")
    slack.react(at="1758210002.000000", ts="1758210000.000100", channel="C0MAIN01",
                user="U02AAA", name="x")
    slack.react(at="1758210003.000000", ts="1758210000.000100", channel="C0MAIN01",
                user="U0BOT", name="x")
    slack.as_of("1758210004.000000")

    msgs = slack.history("C0MAIN01", oldest="0.000000")
    reaction = msgs[0]["reactions"][0]
    assert reaction["count"] == 3
    assert len(reaction["users"]) == 1

    full = slack.reactions_get("C0MAIN01", "1758210000.000100")
    full_reaction = full["reactions"][0]
    assert full_reaction["count"] == 3
    assert len(full_reaction["users"]) == 3


def test_truncate_reaction_users_fault_drops_bot_id():
    slack = _basic_slack()
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="x")
    slack.react(at="1758210001.000000", ts="1758210000.000100", channel="C0MAIN01",
                user="U0BOT", name="x")
    slack.react(at="1758210002.000000", ts="1758210000.000100", channel="C0MAIN01",
                user="U01AAA", name="x")
    slack.faults.truncate_reaction_users(limit=2, drop_bot=True)
    slack.as_of("1758210003.000000")

    msgs = slack.history("C0MAIN01", oldest="0.000000")
    reaction = msgs[0]["reactions"][0]
    assert reaction["count"] == 2
    assert "U0BOT" not in reaction["users"]
    assert reaction["count"] > len(reaction["users"])


# -- newest-first pagination --------------------------------------------------

def test_history_newest_first_and_paginates_full_set():
    slack = _basic_slack()
    total = 450  # > 2 * HISTORY_PAGE_SIZE (200)
    base = 1758210000
    for i in range(total):
        slack.post(at=f"{base + i}.000000", user="U01AAA", channel="C0MAIN01", text=str(i))
    slack.as_of(f"{base + total}.000000")

    msgs = slack.history("C0MAIN01", oldest="0.000000")
    assert len(msgs) == total
    texts = [m["text"] for m in msgs]
    assert texts == [str(i) for i in reversed(range(total))]


def test_fail_mid_page_aborts_the_call():
    slack = _basic_slack()
    base = 1758210000
    for i in range(450):
        slack.post(at=f"{base + i}.000000", user="U01AAA", channel="C0MAIN01", text=str(i))
    slack.as_of(f"{base + 450}.000000")

    slack.faults.fail_mid_page(after_pages=1, error="internal_error")
    with pytest.raises(SlackAPIError):
        slack.history("C0MAIN01", oldest="0.000000")


def test_fail_mid_page_transport_variant():
    slack = _basic_slack()
    base = 1758210000
    for i in range(450):
        slack.post(at=f"{base + i}.000000", user="U01AAA", channel="C0MAIN01", text=str(i))
    slack.as_of(f"{base + 450}.000000")

    slack.faults.fail_mid_page(after_pages=1, error="transport")
    with pytest.raises(SlackTransportError):
        slack.history("C0MAIN01", oldest="0.000000")


def test_repeat_cursor_raises_pagination_error_never_spins():
    slack = _basic_slack()
    base = 1758210000
    for i in range(450):
        slack.post(at=f"{base + i}.000000", user="U01AAA", channel="C0MAIN01", text=str(i))
    slack.as_of(f"{base + 450}.000000")

    slack.faults.repeat_cursor()
    with pytest.raises(SlackPaginationError):
        slack.history("C0MAIN01", oldest="0.000000")


def test_empty_page_with_more_keeps_paging():
    slack = _basic_slack()
    base = 1758210000
    for i in range(10):
        slack.post(at=f"{base + i}.000000", user="U01AAA", channel="C0MAIN01", text=str(i))
    slack.as_of(f"{base + 10}.000000")

    slack.faults.empty_page_with_more()
    msgs = slack.history("C0MAIN01", oldest="0.000000")
    assert len(msgs) == 10


# -- multi-channel -------------------------------------------------------------

def test_multi_channel_isolation_and_membership():
    slack = _basic_slack(channels=("C0MAIN01", "C0OFF001"), bot_member_of=("C0MAIN01",))
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="main")
    slack.post(at="1758210000.000200", user="U01AAA", channel="C0OFF001", text="off")
    slack.as_of("1758210001.000000")

    with pytest.raises(NotInChannel):
        slack.history("C0OFF001", oldest="0.000000")

    with pytest.raises(ChannelNotFound):
        slack.history("C0NOPE", oldest="0.000000")

    main_msgs = slack.history("C0MAIN01", oldest="0.000000")
    assert [m["text"] for m in main_msgs] == ["main"]


# -- metadata round-trip -------------------------------------------------------

def test_metadata_round_trips_through_post_and_update():
    slack = _basic_slack()
    metadata = {
        "event_type": "snipe_digest",
        "event_payload": {"report": "daily", "period_key": "daily:2026-09-18",
                           "channel": "C0MAIN01", "revision": 0},
    }
    ts = slack.post_message("C0MAIN01", text="standings", blocks=[], metadata=metadata)
    msgs = slack.history("C0MAIN01", oldest="0.000000")
    assert msgs[0]["metadata"] == metadata
    assert msgs[0]["bot_id"] == "B0BOT"
    assert msgs[0]["app_id"] == "A0APP"
    assert msgs[0]["subtype"] == "bot_message"

    metadata_rev1 = dict(metadata)
    metadata_rev1["event_payload"] = dict(metadata["event_payload"])
    metadata_rev1["event_payload"]["revision"] = 1
    slack.update_message("C0MAIN01", ts, text="standings (updated)", blocks=[],
                          metadata=metadata_rev1)
    msgs = slack.history("C0MAIN01", oldest="0.000000")
    assert msgs[0]["metadata"] == metadata_rev1
    assert msgs[0]["text"] == "standings (updated)"


# -- reaction add/remove semantics --------------------------------------------

def test_reactions_add_remove_natural_semantics():
    slack = _basic_slack()
    ts = slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="x")
    slack.as_of("1758210001.000000")

    slack.reactions_add("C0MAIN01", ts, "x")
    with pytest.raises(AlreadyReacted):
        slack.reactions_add("C0MAIN01", ts, "x")

    slack.reactions_remove("C0MAIN01", ts, "x")
    with pytest.raises(NoReaction):
        slack.reactions_remove("C0MAIN01", ts, "x")


def test_reaction_error_fault_forces_the_desired_outcome():
    slack = _basic_slack()
    ts = slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="x")
    slack.as_of("1758210001.000000")

    slack.faults.reaction_error(method="reactions_add", error="already_reacted")
    with pytest.raises(AlreadyReacted):
        slack.reactions_add("C0MAIN01", ts, "x")
    # fault consumed and no state change: a real add now succeeds
    slack.reactions_add("C0MAIN01", ts, "x")


def test_reactions_add_on_vanished_message_raises_message_not_found():
    slack = _basic_slack()
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="x")
    slack.delete_message(at="1758210001.000000", ts="1758210000.000100", channel="C0MAIN01")
    slack.as_of("1758210002.000000")
    with pytest.raises(MessageNotFound):
        slack.reactions_add("C0MAIN01", "1758210000.000100", "x")


# -- users_list / conversations_members paging --------------------------------

def test_users_list_returns_every_user_regardless_of_count():
    users = {f"U{i:05d}": FakeUser(id=f"U{i:05d}") for i in range(350)}
    users["U0BOT"] = FakeUser(id="U0BOT", is_bot=True)
    slack = _basic_slack(users=users)

    raw = slack.users_list()
    assert len(raw) == 351
    assert {u["id"] for u in raw} == set(users.keys())


def test_conversations_members_union_and_bot_membership():
    slack = _basic_slack(
        channel_members={"C0MAIN01": [f"U{i:05d}" for i in range(300)]},
    )
    members = slack.conversations_members("C0MAIN01")
    assert len(members) == 301  # 300 humans + the bot (bot_member_of includes C0MAIN01)
    assert "U0BOT" in members
    assert members == sorted(members)


def test_conversations_members_omits_id_left_out_of_the_map():
    slack = _basic_slack(
        channel_members={"C0MAIN01": ["U01AAA"]},
        bot_member_of=(),
    )
    members = slack.conversations_members("C0MAIN01")
    assert members == ["U01AAA"]
    assert "U02AAA" not in members


def test_channel_info_and_conversations_members_unknown_channel():
    slack = _basic_slack()
    with pytest.raises(ChannelNotFound):
        slack.channel_info("C0NOPE")
    with pytest.raises(ChannelNotFound):
        slack.conversations_members("C0NOPE")


# -- fetch_file_bytes + fetch faults -------------------------------------------

def test_fetch_file_bytes_serves_registered_bytes():
    slack = _basic_slack()
    payload = b"\xff\xd8\xff-fake-jpeg-body"
    f = _photo_file(payload=payload)
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01",
               text="x", files=[f], subtype="file_share")

    got = slack.fetch_file_bytes(f["thumb_1024"])
    assert got == payload


def test_fetch_file_bytes_unknown_url_is_404():
    slack = _basic_slack()
    with pytest.raises(SlackHTTPError) as excinfo:
        slack.fetch_file_bytes("https://fixture.invalid/files/NOPE/thumb_1024.jpg")
    assert excinfo.value.status == 404


@pytest.mark.parametrize(
    "arm, expect",
    [
        (lambda s: s.faults.fetch_timeout(), SlackTransportError),
        (lambda s: s.faults.fetch_429(retry_after_seconds=5), RateLimited),
        (lambda s: s.faults.fetch_oversize(), FileTooLarge),
    ],
)
def test_fetch_file_bytes_faults(arm, expect):
    slack = _basic_slack()
    payload = b"some-bytes"
    f = _photo_file(payload=payload)
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01",
               text="x", files=[f], subtype="file_share")
    arm(slack)
    with pytest.raises(expect):
        slack.fetch_file_bytes(f["thumb_1024"])
    # fault is consumed: the next fetch succeeds normally
    assert slack.fetch_file_bytes(f["thumb_1024"]) == payload


def test_fetch_oversize_raises_with_the_armed_limit():
    slack = _basic_slack()
    f = _photo_file(payload=b"some-bytes")
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01",
               text="x", files=[f], subtype="file_share")
    slack.faults.fetch_oversize(limit_bytes=2_000_000)

    with pytest.raises(FileTooLarge) as excinfo:
        slack.fetch_file_bytes(f["thumb_1024"])
    assert excinfo.value.limit_bytes == 2_000_000


def test_fetch_file_bytes_on_tombstoned_file_is_404():
    slack = _basic_slack()
    f = _photo_file(payload=b"some-bytes")
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01",
               text="x", files=[f], subtype="file_share")
    slack.delete_file(at="1758210010.000000", ts="1758210000.000100", channel="C0MAIN01",
                       file_index=0)
    slack.as_of("1758210010.000000")

    with pytest.raises(SlackHTTPError) as excinfo:
        slack.fetch_file_bytes(f["thumb_1024"])
    assert excinfo.value.status == 404


def test_fetch_file_bytes_on_deleted_message_is_404():
    slack = _basic_slack()
    f = _photo_file(payload=b"some-bytes")
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01",
               text="x", files=[f], subtype="file_share")
    slack.delete_message(at="1758210010.000000", ts="1758210000.000100", channel="C0MAIN01")
    slack.as_of("1758210010.000000")

    with pytest.raises(SlackHTTPError) as excinfo:
        slack.fetch_file_bytes(f["thumb_1024"])
    assert excinfo.value.status == 404


def test_fetch_truncate_returns_a_short_prefix():
    slack = _basic_slack()
    payload = b"0123456789abcdef"
    f = _photo_file(payload=payload)
    slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01",
               text="x", files=[f], subtype="file_share")
    slack.faults.fetch_truncate()

    got = slack.fetch_file_bytes(f["thumb_1024"])
    assert got != payload
    assert len(got) < len(payload)
    assert payload.startswith(got)


# -- thread replies -------------------------------------------------------------

def test_non_broadcast_reply_invisible_broadcast_reply_visible():
    slack = _basic_slack()
    parent = slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="parent")
    slack.reply(at="1758210001.000000", user="U02AAA", channel="C0MAIN01",
                parent_ts=parent, text="quiet reply", broadcast=False)
    reply_ts = slack.reply(at="1758210002.000000", user="U02AAA", channel="C0MAIN01",
                            parent_ts=parent, text="loud reply", broadcast=True)
    slack.as_of("1758210003.000000")

    msgs = slack.history("C0MAIN01", oldest="0.000000")
    texts = {m["text"] for m in msgs}
    assert "loud reply" in texts
    assert "quiet reply" not in texts

    broadcast_msg = next(m for m in msgs if m["ts"] == reply_ts)
    assert broadcast_msg["subtype"] == "thread_broadcast"
    assert broadcast_msg["thread_ts"] == parent

    parent_msg = next(m for m in msgs if m["ts"] == parent)
    assert parent_msg["thread_ts"] == parent
    assert parent_msg["reply_count"] == 2
    assert parent_msg["reply_users"] == ["U02AAA"]
    assert parent_msg["latest_reply"] == "1758210002.000000"


# -- rate_limit fault (generic) -------------------------------------------------

def test_rate_limit_absorbed_within_retry_budget():
    slack = _basic_slack()
    slack.faults.rate_limit(method="history", retry_after_seconds=1, times=3)
    # MAX_RATE_LIMIT_RETRIES is 8: 3 <= 8, so the call quietly succeeds.
    assert slack.history("C0MAIN01", oldest="0.000000") == []


def test_rate_limit_exhausted_raises():
    slack = _basic_slack()
    slack.faults.rate_limit(method="reactions_add", retry_after_seconds=7, times=20)
    ts = slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="x")
    with pytest.raises(RateLimited) as excinfo:
        slack.reactions_add("C0MAIN01", ts, "x")
    assert excinfo.value.retry_after_seconds == 7
    assert excinfo.value.method == "reactions_add"


def test_lose_post_response_applies_but_raises():
    slack = _basic_slack()
    slack.faults.lose_post_response(method="post_message")
    with pytest.raises(SlackTransportError):
        slack.post_message("C0MAIN01", text="digest", blocks=[], metadata=None)
    # the message really posted despite the lost response
    msgs = slack.history("C0MAIN01", oldest="0.000000")
    assert len(msgs) == 1
    assert msgs[0]["text"] == "digest"


# -- world file round-trip -----------------------------------------------------

def test_world_file_round_trip_preserves_state(tmp_path):
    path = tmp_path / "world.json"
    seed = _basic_slack()
    ts = seed.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01",
                    text="got <@U02AAA>", files=[_photo_file()], subtype="file_share")
    seed.react(at="1758210001.000000", ts=ts, channel="C0MAIN01", user="U02AAA", name="x")
    seed.faults.rate_limit(method="reactions_add", retry_after_seconds=3, times=2)
    seed.faults.truncate_reaction_users(limit=1, drop_bot=True)
    seed.as_of("1758210002.000000")

    path.write_text(json.dumps(seed.to_world_dict(), ensure_ascii=True,
                                separators=(",", ":")), encoding="utf-8")

    reloaded = FakeSlack.from_world_dict(json.loads(path.read_text(encoding="utf-8")))
    assert reloaded.to_world_dict() == seed.to_world_dict()

    file_backed = FileBackedFakeSlack(path=str(path))
    direct_msgs = seed.history("C0MAIN01", oldest="0.000000")
    backed_msgs = file_backed.history("C0MAIN01", oldest="0.000000")
    assert backed_msgs == direct_msgs

    # bytes survived the JSON round trip too
    got = file_backed.fetch_file_bytes(_photo_file()["thumb_1024"])
    assert got == b"jpeg-bytes-001"


def test_world_file_round_trip_preserves_channel_members(tmp_path):
    path = tmp_path / "world.json"
    seed = _basic_slack(channel_members={"C0MAIN01": ["U01AAA"]})

    reloaded = FakeSlack.from_world_dict(seed.to_world_dict())
    assert reloaded.channel_members == {"C0MAIN01": ["U01AAA"]}
    assert reloaded.to_world_dict() == seed.to_world_dict()

    path.write_text(json.dumps(seed.to_world_dict(), ensure_ascii=True,
                                separators=(",", ":")), encoding="utf-8")
    from_disk = FakeSlack.from_world_dict(json.loads(path.read_text(encoding="utf-8")))
    assert from_disk.conversations_members("C0MAIN01") == seed.conversations_members("C0MAIN01")


def test_world_file_round_trip_channel_members_none_means_everyone(tmp_path):
    seed = _basic_slack()  # no channel_members override: everyone
    assert seed.channel_members is None

    reloaded = FakeSlack.from_world_dict(seed.to_world_dict())
    assert reloaded.channel_members is None
    assert reloaded.to_world_dict() == seed.to_world_dict()


def test_world_file_round_trip_survives_a_kill_mid_run(tmp_path):
    path = tmp_path / "world.json"
    seed = _basic_slack(horizon_days=90)
    ts = seed.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="x")
    seed.faults.vanish(ts=ts, for_fetches=1)
    seed.as_of("1758210001.000000")
    path.write_text(json.dumps(seed.to_world_dict(), ensure_ascii=True,
                                separators=(",", ":")), encoding="utf-8")

    file_backed = FileBackedFakeSlack(path=str(path))
    # "kill nothing" -- a fresh FileBackedFakeSlack call re-reads the file each
    # time, simulating a subprocess restart between calls.
    first = file_backed.history("C0MAIN01", oldest="0.000000")
    assert first == []  # vanish still armed for this one fetch

    second = file_backed.history("C0MAIN01", oldest="0.000000")
    assert len(second) == 1  # for_fetches consumed: the message reappears

    world_after = json.loads(path.read_text(encoding="utf-8"))
    assert world_after["faults"] == []  # the vanish fault was consumed and persisted
