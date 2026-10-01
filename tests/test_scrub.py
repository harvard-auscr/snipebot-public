"""tools/scrub.py: the section 8 ID-scrub rules, offline, on hand-built
captured-looking payloads and on the current provisional fixtures; plus
tools/capture_fixtures.py --dry-run touching no network and constructing no
WebClient.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.scrub import load_scrub_map, no_real_identifiers, scrub

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "tests" / "fixtures"


def _captured_looking_message() -> dict:
    """A hand-built payload shaped like a real, un-scrubbed Slack capture:
    realistic real-looking ids, a real-looking permalink/url_private/thumb
    set, a mention with a label, a token and a client_msg_id that must never
    survive."""
    return {
        "type": "message",
        "user": "U012ABCDEF",
        "text": "got <@U013ABCDEQ|Real Person> for real",
        "ts": "1758210000.000100",
        "team": "T012ABCDEF",
        "subtype": "file_share",
        "client_msg_id": "9c6f2b3a-1111-4c2d-9e21-abcdef012345",
        "token": "xoxb-not-a-real-token",
        "files": [
            {
                "id": "F012ABCDEFG",
                "mimetype": "image/jpeg",
                "filetype": "jpg",
                "name": "IMG_20260918_120000.jpg",
                "size": 2355812,
                "original_w": 3024,
                "original_h": 4032,
                "is_tombstoned": False,
                "mode": "hosted",
                "file_access": "visible",
                "url_private": (
                    "https://files.slack.com/files-pri/T012ABCDEF-F012ABCDEFG/img.jpg"
                ),
                "url_private_download": (
                    "https://files.slack.com/files-pri/T012ABCDEF-F012ABCDEFG/download/img.jpg"
                ),
                "thumb_1024": (
                    "https://files.slack.com/files-tmb/T012ABCDEF-F012ABCDEFG-x/img_1024.jpg"
                ),
                "thumb_1024_w": 768,
                "thumb_1024_h": 1024,
                "permalink": "https://my-real-workspace.slack.com/archives/C012ABCDEF/p1758210000000100",
                "permalink_public": "https://slack-files.com/T012ABCDEF-F012ABCDEFG-deadbeef",
            }
        ],
    }


# --- scrub(): assignment, determinism, mentions --------------------------------

def test_scrub_assigns_ids_first_seen_order_per_category():
    payload = _captured_looking_message()
    scrub_map: dict[str, str] = {}

    out = scrub(payload, scrub_map)

    # sender seen first (the "user" key), the mentioned id second (only in "text")
    assert scrub_map["U012ABCDEF"] == "U0AAA001"
    assert scrub_map["U013ABCDEQ"] == "U0AAA002"
    assert scrub_map["T012ABCDEF"] == "T0TEAM"
    assert scrub_map["F012ABCDEFG"] == "F0FILE001"

    assert out["user"] == "U0AAA001"
    assert out["team"] == "T0TEAM"
    assert out["text"] == "got <@U0AAA002> for real"  # label dropped


def test_scrub_next_ids_continue_from_a_populated_map():
    scrub_map = {"U0REALOLD01": "U0AAA001", "U0REALOLD02": "U0AAA002"}
    payload = {"type": "message", "user": "U0REALNEW03", "text": "hi", "ts": "1.000000"}

    out = scrub(payload, scrub_map)

    assert out["user"] == "U0AAA003"
    assert scrub_map["U0REALNEW03"] == "U0AAA003"


def test_scrub_is_deterministic_across_equivalent_calls():
    map_a: dict[str, str] = {}
    map_b: dict[str, str] = {}

    out_a = scrub(copy.deepcopy(_captured_looking_message()), map_a)
    out_b = scrub(copy.deepcopy(_captured_looking_message()), map_b)

    assert out_a == out_b
    assert map_a == map_b


def test_scrub_does_not_mutate_its_input_payload():
    payload = _captured_looking_message()
    before = copy.deepcopy(payload)

    scrub(payload, {})

    assert payload == before


def test_scrub_is_a_no_op_once_ids_are_identity_mapped():
    """A payload already written with placeholder ids, scrubbed against a map
    that already maps those placeholders to themselves, comes back unchanged
    -- exactly the provisional-fixture situation before G2."""
    payload = {
        "type": "message", "user": "U0AAA001", "text": "got <@U0AAA002>",
        "ts": "1758210000.000100", "team": "T0TEAM", "subtype": "file_share",
        "files": [{
            "id": "F0FILE001", "mimetype": "image/jpeg", "filetype": "jpg",
            "name": "photo-1.jpg", "size": 100, "original_w": 10, "original_h": 10,
            "url_private": "https://fixture.invalid/files/F0FILE001/download/photo.jpg",
            "url_private_download": "https://fixture.invalid/files/F0FILE001/download/photo.jpg?dl=1",
            "thumb_1024": "https://fixture.invalid/files/F0FILE001/thumb_1024.jpg",
            "permalink": None, "permalink_public": None,
        }],
    }
    identity_map = {"U0AAA001": "U0AAA001", "U0AAA002": "U0AAA002",
                     "T0TEAM": "T0TEAM", "F0FILE001": "F0FILE001"}

    out = scrub(copy.deepcopy(payload), identity_map)

    assert out == payload


# --- rules table: permalink/url/token/technical fields -------------------------

def test_scrub_nulls_permalink_and_rewrites_file_urls():
    out = scrub(_captured_looking_message(), {})
    f = out["files"][0]

    assert f["permalink"] is None
    assert f["permalink_public"] is None
    assert f["url_private"] == "https://fixture.invalid/files/F0FILE001/download/photo.jpg"
    assert f["url_private_download"] == (
        "https://fixture.invalid/files/F0FILE001/download/photo.jpg?dl=1"
    )
    assert f["thumb_1024"] == "https://fixture.invalid/files/F0FILE001/thumb_1024.jpg"
    assert f["name"] == "photo-1.jpg"
    for url in (f["url_private"], f["url_private_download"], f["thumb_1024"]):
        assert url.startswith("https://fixture.invalid/")


def test_scrub_keeps_technical_file_fields_and_ts():
    out = scrub(_captured_looking_message(), {})
    f = out["files"][0]

    assert f["mimetype"] == "image/jpeg"
    assert f["is_tombstoned"] is False
    assert f["mode"] == "hosted"
    assert f["file_access"] == "visible"
    assert f["size"] == 2355812
    assert f["original_w"] == 3024
    assert f["original_h"] == 4032
    assert f["thumb_1024_w"] == 768
    assert f["thumb_1024_h"] == 1024
    # ts is kept as an opaque string, never touched
    assert out["ts"] == "1758210000.000100"


def test_scrub_video_file_gets_a_clip_name_not_a_photo_name():
    payload = {
        "type": "message", "user": "U0AAA001", "text": "clip", "ts": "1.000000",
        "files": [{
            "id": "F0REALVIDEO1", "mimetype": "video/mp4", "filetype": "mp4",
            "name": "IMG_0099.mp4", "size": 999,
            "url_private": "https://files.slack.com/x/clip.mp4",
            "thumb_video": "https://files.slack.com/x/thumb.jpg",
            "permalink": None, "permalink_public": None,
        }],
    }
    scrub_map = {"U0AAA001": "U0AAA001"}

    out = scrub(payload, scrub_map)
    f = out["files"][0]

    assert f["name"].startswith("clip-")
    assert f["name"].endswith(".mp4")
    assert f["thumb_video"].startswith("https://fixture.invalid/files/")
    assert f["thumb_video"].endswith(".jpg")


def test_scrub_phone_upload_file_fields_seen_on_a_real_capture():
    """A real iPhone upload (G2) carried, inside the file object: the photo's
    own bytes inline as base64 `thumb_tiny`, the device photo UUID as `title`,
    the uploader's id as `user` + `user_team`, and a `shares` block with
    channel names. None of those may reach a fixture; the ids must map to the
    same placeholders the message-level fields get."""
    payload = {
        "type": "message", "user": "U012ABCDEF", "text": "<@U013ABCDEQ> ",
        "ts": "1.000000", "team": "T012ABCDEF",
        "files": [{
            "id": "F012ABCDEFG", "mimetype": "image/jpeg", "filetype": "jpg",
            "name": "43312E2F-513C-435E-8AEC-1BB8AF4BCBF0.jpg",
            "title": "43312E2F-513C-435E-8AEC-1BB8AF4BCBF0",
            "thumb_tiny": "AwAiADCuoAdfrV1Fqvtyy/UVbj4dh3yMfl/9amSOC0oWopofM27ycYPHvU0",
            "user": "U012ABCDEF", "user_team": "T012ABCDEF",
            "shares": {"public": {"C012ABCDEF": [{"ts": "1.000000", "channel_name": "real-name",
                                                   "team_id": "T012ABCDEF"}]}},
            "channels": ["C012ABCDEF"], "groups": [], "ims": [],
            "permalink": None, "permalink_public": None,
        }],
    }

    out = scrub(payload, {})
    f = out["files"][0]

    assert f["name"] == "photo-1.jpg"
    assert f["title"] == "photo-1"
    assert f["thumb_tiny"] == "AAAA"
    assert f["user"] == out["user"] == "U0AAA001"
    assert f["user_team"] == out["team"] == "T0TEAM"
    assert "shares" not in f
    assert f["channels"] == ["C0CH001"]
    assert no_real_identifiers(out, vocabulary=()) == []
    # idempotent: scrubbing the scrubbed payload changes nothing
    assert scrub(out, {}) == out


def test_scrub_user_token_post_bot_profile_seen_on_a_real_capture():
    """A text message posted through an app's user token (the capture tool,
    the rig) carries `bot_id`, `app_id` and a `bot_profile` whose `id` is a
    bot id and whose `name` is the app's name (G2). The ids map like any
    other, the name never survives, and the icons become fixture urls."""
    payload = {
        "type": "message", "user": "U012ABCDEF", "text": "got <@U013ABCDEQ>",
        "ts": "1.000000", "team": "T012ABCDEF", "bot_id": "B0ZZZZZZZZZ", "app_id": "A0YYYYYYYYY",
        "bot_profile": {
            "id": "B0ZZZZZZZZZ", "app_id": "A0YYYYYYYYY", "team_id": "T012ABCDEF",
            "name": "Some Real App Name", "deleted": False, "updated": 1790000000,
            "icons": {"image_36": "https://avatars.slack-edge.com/x_36.png"},
        },
    }

    out = scrub(payload, {})
    bp = out["bot_profile"]

    assert out["bot_id"] == bp["id"] == "B0BOT"
    assert out["app_id"] == bp["app_id"] == "A0APP"
    assert bp["team_id"] == out["team"] == "T0TEAM"
    assert bp["name"] == "user-bot"
    assert bp["icons"]["image_36"].startswith("https://fixture.invalid/")
    assert bp["updated"] == 1790000000
    assert no_real_identifiers(out, vocabulary=()) == []
    assert scrub(out, {}) == out


def test_scrub_thread_shapes_seen_on_a_real_capture():
    """A real broadcast reply embeds its parent under `root` (with a
    `reply_users` id list); a real reply carries `parent_user_id` (G2). Both
    map with the same user map as everything else."""
    payload = {
        "type": "message", "subtype": "thread_broadcast", "user": "U012ABCDEF",
        "text": "got <@U013ABCDEQ>", "ts": "2.000000", "thread_ts": "1.000000",
        "parent_user_id": "U012ABCDEF",
        "root": {
            "type": "message", "user": "U012ABCDEF", "text": "parent", "ts": "1.000000",
            "thread_ts": "1.000000", "reply_count": 1, "reply_users_count": 1,
            "latest_reply": "2.000000", "reply_users": ["U012ABCDEF"],
            "is_locked": False, "subscribed": True,
        },
    }

    out = scrub(payload, {})

    assert out["user"] == out["parent_user_id"] == "U0AAA001"
    assert out["root"]["user"] == "U0AAA001"
    assert out["root"]["reply_users"] == ["U0AAA001"]
    assert out["text"] == "got <@U0AAA002>"
    assert out["root"]["latest_reply"] == "2.000000"
    assert no_real_identifiers(out, vocabulary=()) == []
    assert scrub(out, {}) == out


def test_scrub_channel_object_ids_seen_on_a_real_capture():
    """A real conversations.info channel carries `creator`, `context_team_id`,
    `shared_team_ids` and `topic`/`purpose` sub-objects with their own
    `creator` (G2); all are ids and map like the rest."""
    payload = {
        "ok": True,
        "channel": {
            "id": "C012ABCDEF", "name": "snipes", "is_channel": True, "is_member": True,
            "is_private": False, "created": 1790197500, "creator": "U012ABCDEF",
            "context_team_id": "T012ABCDEF", "shared_team_ids": ["T012ABCDEF"],
            "topic": {"value": "", "creator": "", "last_set": 0},
            "purpose": {"value": "game on", "creator": "U012ABCDEF", "last_set": 1790197500},
            "previous_names": [],
        },
    }

    out = scrub(payload, {})
    ch = out["channel"]

    assert ch["id"] == "C0CH001"
    assert ch["creator"] == ch["purpose"]["creator"] == "U0AAA001"
    assert ch["topic"]["creator"] == ""
    assert ch["context_team_id"] == "T0TEAM"
    assert ch["shared_team_ids"] == ["T0TEAM"]
    assert ch["name"] == "snipes"
    assert no_real_identifiers(out, vocabulary=()) == []
    assert scrub(out, {}) == out


def test_scrub_keeps_slack_link_brackets_seen_on_a_real_capture():
    """Slack stores a posted link in `text` as `<url>` (or `<url|label>`); the
    URL placeholder must not swallow the closing bracket (G2 saw
    `look <https://fixture.invalid/misc/1 <@U…>` come out of the scrub)."""
    payload = {
        "type": "message", "user": "U0AAA001", "ts": "1.000000",
        "text": "look <https://example.com/shared/pic.jpg> and <https://example.com/x|x> <@U0AAA002>",
    }

    out = scrub(payload, {"U0AAA001": "U0AAA001", "U0AAA002": "U0AAA002"})

    assert out["text"] == (
        "look <https://fixture.invalid/misc/1> and <https://fixture.invalid/misc/2|x> <@U0AAA002>"
    )
    assert no_real_identifiers(out, vocabulary=("U0AAA001", "U0AAA002")) == []


def test_scrub_users_list_entry_real_names_seen_on_a_real_capture():
    """A real users.list entry (G2) carries the person's name at the top
    level (`real_name`) and split across `profile.first_name` /
    `profile.last_name`, an email-derived `avatar_hash`, and free-text
    profile fields. None may survive, and the verifier must catch the
    unscrubbed entry on its own."""
    entry = {
        "id": "U012ABCDEF", "name": "somebody", "real_name": "Some Body",
        "is_bot": False, "deleted": False, "team_id": "T012ABCDEF",
        "profile": {
            "real_name": "Some Body", "display_name": "sb", "first_name": "Some",
            "last_name": "Body", "real_name_normalized": "Some Body",
            "display_name_normalized": "sb", "avatar_hash": "g0123456789ab",
            "title": "president", "phone": "555", "status_text": "at the lake",
            "fields": {"Xf01": {"value": "x"}}, "team": "T012ABCDEF",
        },
    }
    payload = {"ok": True, "members": [entry], "response_metadata": {"next_cursor": ""}}

    assert any("unscrubbed name" in p for p in no_real_identifiers(payload, vocabulary=()))

    out = scrub(payload, {})
    m = out["members"][0]
    assert m["id"] == "U0AAA001"
    assert m["name"] == m["real_name"] == "user-1"
    assert m["profile"]["first_name"] == m["profile"]["real_name"] == "user-1"
    assert m["profile"]["last_name"] == ""
    assert m["profile"]["avatar_hash"] == "0000000000"
    assert m["profile"]["title"] == m["profile"]["phone"] == m["profile"]["status_text"] == ""
    assert m["profile"]["fields"] == {}
    assert m["profile"]["team"] == "T0TEAM"
    assert no_real_identifiers(out, vocabulary=()) == []
    assert scrub(out, {}) == out


def test_scrub_removes_tokens_and_client_msg_id():
    out = scrub(_captured_looking_message(), {})

    assert "token" not in out
    assert "client_msg_id" not in out


def test_scrub_rewrites_usergroup_and_at_channel_stays_literal():
    payload = {
        "type": "message", "user": "U0AAA001",
        "text": "<!subteam^S012REALGRP> <!channel> got <@U0AAA002>",
        "ts": "1.000000",
    }
    scrub_map = {"U0AAA001": "U0AAA001", "U0AAA002": "U0AAA002"}

    out = scrub(payload, scrub_map)

    assert "<!subteam^S0TEAM01>" in out["text"]
    assert "<!channel>" in out["text"]  # left as literal Slack syntax, no id inside
    assert scrub_map["S012REALGRP"] == "S0TEAM01"


def test_scrub_reaction_users_list_and_channel_object():
    reactions_payload = {
        "ok": True, "type": "message",
        "message": {"type": "message", "user": "U0AAA001", "text": "hi", "ts": "1.000000",
                    "reactions": [{"name": "x", "users": ["U099REALADMIN"], "count": 1}]},
        "channel": "C099REALCHAN",
    }
    scrub_map = {"U0AAA001": "U0AAA001"}

    out = scrub(reactions_payload, scrub_map)

    assert out["message"]["reactions"][0]["users"] == [scrub_map["U099REALADMIN"]]
    assert out["channel"] == scrub_map["C099REALCHAN"]
    assert out["channel"] not in ("C099REALCHAN",)


def test_scrub_users_list_entry_gets_placeholder_name_and_bot_is_special():
    payload = {
        "ok": True,
        "members": [
            {"id": "U011REALHUMAN", "team_id": "T0TEAM", "name": "robin.real.name",
             "deleted": False, "is_bot": False, "is_admin": False,
             "profile": {"display_name": "Robin Real Name", "real_name": "Robin Real Name"}},
            {"id": "U022REALBOT", "team_id": "T0TEAM", "name": "shoutout-bot",
             "deleted": False, "is_bot": True, "is_admin": False,
             "profile": {"display_name": "Shoutout Bot", "real_name": "Shoutout Bot",
                         "bot_id": "B0123456", "api_app_id": "A0123456"}},
        ],
        "response_metadata": {"next_cursor": ""},
    }
    scrub_map = {"T0TEAM": "T0TEAM"}

    out = scrub(payload, scrub_map)
    human, bot = out["members"]

    assert human["name"].startswith("user-")
    assert human["profile"]["display_name"] == human["name"]
    assert human["profile"]["real_name"] == human["name"]
    assert bot["name"] == "user-bot"
    assert bot["profile"]["display_name"] == "user-bot"
    assert bot["profile"]["bot_id"].startswith("B0BOT")
    assert bot["profile"]["api_app_id"].startswith("A0APP")


def test_scrub_channel_object_keeps_its_name():
    payload = {"ok": True, "channel": {"id": "C0123456AB", "name": "main",
                                       "is_member": True, "is_private": False}}
    out = scrub(payload, {})
    assert out["channel"]["name"] == "main"
    assert out["channel"]["id"] != "C0123456AB"


def test_scrub_generic_url_outside_a_file_object():
    payload = {
        "type": "message", "user": "U0AAA001",
        "text": "look https://media.example.com/real/photo.jpg <@U0AAA001>",
        "ts": "1.000000",
        "attachments": [{"id": 1, "image_url": "https://media.example.com/real/photo.jpg",
                          "from_url": "https://media.example.com/real/photo.jpg"}],
    }
    scrub_map = {"U0AAA001": "U0AAA001"}

    out = scrub(payload, scrub_map)

    assert "https://media.example.com" not in out["text"]
    assert "https://fixture.invalid/" in out["text"]
    att = out["attachments"][0]
    assert att["image_url"].startswith("https://fixture.invalid/")
    assert att["from_url"] == att["image_url"]  # same raw url -> same placeholder, one call
    assert att["id"] == 1  # a plain int, never touched


# --- verifier -------------------------------------------------------------------

def test_no_real_identifiers_catches_leftover_real_id():
    problems = no_real_identifiers({"user": "U012345678REAL"}, vocabulary=set())
    assert any("real-looking id" in p for p in problems)


def test_no_real_identifiers_ignores_ids_in_vocabulary():
    problems = no_real_identifiers({"user": "U0AAA00001LONG"}, vocabulary={"U0AAA00001LONG"})
    assert problems == []


def test_no_real_identifiers_catches_leftover_url():
    problems = no_real_identifiers({"file": {"url": "https://files.slack.com/real/path.jpg"}})
    assert any("non-fixture URL" in p for p in problems)


def test_no_real_identifiers_catches_token_key():
    problems = no_real_identifiers({"token": "xoxb-should-not-be-here"})
    assert any("token-like key" in p for p in problems)


def test_no_real_identifiers_catches_unscrubbed_permalink():
    problems = no_real_identifiers(
        {"permalink": "https://fixture.invalid/x", "permalink_public": None}
    )
    assert any("unscrubbed permalink" in p for p in problems)
    assert not any("permalink_public" in p for p in problems)


def test_no_real_identifiers_clean_payload_passes():
    payload = json.loads((FIXTURES_DIR / "history" / "photo-and-tag.json").read_text())
    assert no_real_identifiers(payload) == []


# --- the current provisional fixtures: scrub() is a no-op ----------------------

def _all_fixture_json_files() -> list[Path]:
    files: list[Path] = []
    for sub in ("history", "refetch", "reactions", "users", "channels", "controls"):
        d = FIXTURES_DIR / sub
        if d.is_dir():
            files.extend(sorted(d.rglob("*.json")))
    return files


@pytest.mark.parametrize("path", _all_fixture_json_files(), ids=lambda p: str(p.relative_to(FIXTURES_DIR)))
def test_scrubbing_current_provisional_fixtures_is_a_noop(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    scrub_map = load_scrub_map()  # the persisted provisional identity map

    out = scrub(copy.deepcopy(payload), scrub_map)

    assert out == payload
    assert no_real_identifiers(out) == []


def test_provisional_scrub_map_is_untouched_by_the_noop_sweep():
    before = load_scrub_map()
    for path in _all_fixture_json_files():
        payload = json.loads(path.read_text(encoding="utf-8"))
        scrub(payload, load_scrub_map())
    after = load_scrub_map()
    assert before == after


def test_committed_scrub_map_names_no_real_id():
    """The committed map is the placeholder vocabulary only (G2: the first real
    capture wrote the real team, channel, user and bot ids into it, which would
    have made every scrubbed fixture reversible once pushed)."""
    raw = json.loads((FIXTURES_DIR / "scrub_map.json").read_text(encoding="utf-8"))
    leaked = sorted(k for k, v in raw.items() if k != "_note" and k != v)
    assert leaked == []


def test_save_scrub_map_keeps_real_ids_out_of_the_committed_file(tmp_path: Path):
    from tools.scrub import save_scrub_map

    committed, local = tmp_path / "scrub_map.json", tmp_path / "scrub_map.local.json"
    working = {"U012ABCDEF": "U0AAA001", "T012ABCDEF": "T0TEAM", "U0AAA002": "U0AAA002"}

    save_scrub_map(working, committed, note="n", local_path=local)

    on_disk = json.loads(committed.read_text(encoding="utf-8"))
    assert "U012ABCDEF" not in committed.read_text(encoding="utf-8")
    assert {k: v for k, v in on_disk.items() if k != "_note"} == {
        "U0AAA001": "U0AAA001", "T0TEAM": "T0TEAM", "U0AAA002": "U0AAA002",
    }
    real = json.loads(local.read_text(encoding="utf-8"))
    assert {k: v for k, v in real.items() if k != "_note"} == {
        "U012ABCDEF": "U0AAA001", "T012ABCDEF": "T0TEAM",
    }
    # Round trip: the working map comes back whole, and a machine without the
    # local file still sees the full vocabulary.
    merged = load_scrub_map(committed, local_path=local)
    assert merged["U012ABCDEF"] == "U0AAA001" and merged["U0AAA002"] == "U0AAA002"
    assert set(load_scrub_map(committed, local_path=None).values()) == set(working.values())


# --- capture_fixtures.py --dry-run: no network, no WebClient --------------------

def test_dry_run_prints_plan_and_exits_0_with_no_tokens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)

    def _raise(*_args, **_kwargs):
        raise AssertionError("dry-run must never construct a WebClient")

    monkeypatch.setattr("slack_sdk.WebClient", _raise)

    from tools import capture_fixtures

    exit_code = capture_fixtures.main(["--dry-run"])

    assert exit_code == 0


def test_dry_run_only_filters_the_plan(capsys: pytest.CaptureFixture[str]):
    from tools import capture_fixtures

    exit_code = capture_fixtures.main(["--dry-run", "--only", "photo-and-tag"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "photo-and-tag" in out
    assert "multi-tag" not in out
    assert "Also captured" not in out  # --only trims the plan to just that shape


def test_refuses_without_tokens_and_touches_no_network(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)

    def _raise(*_args, **_kwargs):
        raise AssertionError("must never construct a WebClient without both tokens")

    monkeypatch.setattr("slack_sdk.WebClient", _raise)

    from tools import capture_fixtures

    exit_code = capture_fixtures.main([])

    assert exit_code != 0


def test_dry_run_subprocess_exits_0():
    """The exact command from the definition of done, run as a real
    subprocess with no tokens in the environment."""
    import os as _os

    env = dict(_os.environ)
    env.pop("SLACK_BOT_TOKEN", None)
    env.pop("SLACK_USER_TOKEN", None)

    result = subprocess.run(
        [sys.executable, "tools/capture_fixtures.py", "--dry-run"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "G2 capture plan" in result.stdout
