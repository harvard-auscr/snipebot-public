"""Breaker round 1, surface tools-scrub: tools/scrub.py and tools/capture_fixtures.py.

Every payload here is hand-built and offline. Raw (pre-scrub) ids are
synthetic ids shaped like the real workspace's (11 characters, `X0...`);
raw names are synthetic strings (`Rawfirst`, `rawdisplay`, ...). Nothing
touches the network; the capture-tool test drives fake clients and writes
only under tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import scrub as scrub_mod
from tools.scrub import no_real_identifiers, save_scrub_map, scrub

RAW_SNIPER = "U0RAWSNIP01"
RAW_TARGET = "U0RAWTARG02"
RAW_TEAM = "T0RAWTEAM01"
RAW_APP_BOT = "B0RAWAPP002"

RAW_NAME_PARTS = ("Rawfirst", "Rawlast", "rawdisplay", "rawhandle")


def _leftover_names(node: Any) -> list[str]:
    dumped = json.dumps(node)
    return [part for part in RAW_NAME_PARTS if part in dumped]


# --- 1. user_profile embedded on a message ------------------------------------

def test_message_user_profile_real_name_and_avatar_hash_survive_scrub():
    """Claim: a message's embedded `user_profile` (real_name, first_name,
    display_name, name, avatar_hash -- the shape a client-posted message
    carries) passes through scrub() untouched, and no_real_identifiers()
    reports nothing, so a capture would commit a member's real name and
    email-derived avatar hash. Violates spec 10 section 8 ID-scrub rules:
    "display / real names, `profile.*`, `username` -> empty string or
    `user-<n>` (never a real name)"; feed item 15 (email-derived avatar_hash)."""
    msg = {
        "type": "message",
        "user": RAW_SNIPER,
        "text": f"got <@{RAW_TARGET}>",
        "ts": "1790000001.000100",
        "team": RAW_TEAM,
        "user_profile": {
            "avatar_hash": "g0rawhash123",
            "first_name": "Rawfirst",
            "real_name": "Rawfirst Rawlast",
            "display_name": "rawdisplay",
            "team": RAW_TEAM,
            "name": "rawhandle",
            "is_restricted": False,
            "is_ultra_restricted": False,
        },
    }
    out = scrub(msg, {})

    assert _leftover_names(out) == [], f"real name parts survived scrub: {_leftover_names(out)}"
    assert "g0rawhash123" not in json.dumps(out), "email-derived avatar_hash survived scrub"


# --- 2. message-unfurl attachment author --------------------------------------

def test_message_unfurl_attachment_author_name_survives_scrub():
    """Claim: an attachment unfurling a Slack message carries the author's
    display/real name in `author_name`, `author_subname` and the `fallback`
    line; scrub() leaves all three verbatim and no_real_identifiers() does not
    flag them, so the name reaches a committed fixture. Violates spec 10
    section 8 ("display / real names ... never a real name"); feed item 15
    wave-4 angle (`attachments[].author_name`, `attachments[].fallback/text`)."""
    msg = {
        "type": "message",
        "user": RAW_SNIPER,
        "text": f"look <@{RAW_TARGET}>",
        "ts": "1790000002.000200",
        "attachments": [
            {
                "is_msg_unfurl": True,
                "author_name": "Rawfirst Rawlast",
                "author_subname": "rawdisplay",
                "fallback": "[September 25th, 2026 3:00 PM] rawdisplay: nice shot",
                "text": "nice shot",
                "ts": "1790000000.000100",
                "id": 1,
            }
        ],
    }
    out = scrub(msg, {})

    left = _leftover_names(out["attachments"])
    assert left == [], f"unfurl author name parts survived scrub: {left}"


# --- 3. message-level username --------------------------------------------------

def test_message_level_username_survives_scrub():
    """Claim: a message's top-level `username` (set on a bot_message or a post
    made with a display username) is copied through scrub() unchanged. Spec
    10 section 8 names `username` explicitly: "display / real names,
    `profile.*`, `username` -> empty string or `user-<n>` (never a real
    name)"."""
    msg = {
        "type": "message",
        "subtype": "bot_message",
        "username": "Rawfirst Rawlast",
        "bot_id": RAW_APP_BOT,
        "text": "hello",
        "ts": "1790000003.000300",
    }
    out = scrub(msg, {})

    assert out["username"] in ("", "user-bot") or out["username"].startswith("user-"), (
        f"username not scrubbed: {out['username']!r}"
    )


# --- 4. a real id shaped like a placeholder prefix -----------------------------

def test_real_channel_id_with_placeholder_prefix_is_kept_and_committed(tmp_path: Path):
    """Claim: _looks_like_own_placeholder treats ANY `C0CH<alnum>` id as an
    already-scrubbed placeholder, with no length bound. A workspace's real
    channel ids can begin `C0C`, so about 1 in 36 new channels is a real 11-char
    `C0CH...` id: scrub() identity-maps it (the fixture keeps the real id),
    no_real_identifiers() exempts it, and save_scrub_map() writes it into the
    COMMITTED vocabulary file. Violates spec 10 section 8 ("channel ids ->
    ... stable per real id" placeholders) and ruling E-G2-2 (the committed
    map holds the placeholder vocabulary only, never a real id)."""
    raw_channel = "C0CHQ7M2XKB"  # 11 chars: real-id length, not a generated C0CH### placeholder
    payload = {
        "ok": True,
        "type": "message",
        "channel": raw_channel,
        "message": {"type": "message", "user": RAW_SNIPER, "text": "x", "ts": "1790000004.000400"},
    }
    scrub_map: dict[str, str] = {}
    out = scrub(payload, scrub_map)
    committed = tmp_path / "scrub_map.json"
    save_scrub_map(scrub_map, committed, local_path=tmp_path / "scrub_map.local.json")

    assert out["channel"] != raw_channel, "real channel id kept verbatim in the scrubbed fixture"
    assert raw_channel not in committed.read_text(encoding="utf-8"), (
        "real channel id written into the committed vocabulary file"
    )


# --- 5. capture: every shape scrubbed with a throwaway map ---------------------

class _FakeUserClient:
    def __init__(self) -> None:
        self.posted: dict[str, dict[str, Any]] = {}
        self._n = 0

    def auth_test(self) -> dict[str, Any]:
        return {"user_id": RAW_SNIPER, "team_id": RAW_TEAM}

    def files_upload_v2(self, channel: str, initial_comment: str, file_uploads: list, **_: Any):
        self._n += 1
        ts = f"17900001{self._n:02d}.000100"
        file_id = f"F0RAWFILE{self._n:02d}"
        self.posted[ts] = {
            "type": "message",
            "user": RAW_SNIPER,
            "text": initial_comment,
            "ts": ts,
            "team": RAW_TEAM,
            "files": [{
                "id": file_id, "mimetype": "image/jpeg", "filetype": "jpg",
                "name": "IMG_0001.jpg", "title": "raw-device-title",
                "user": RAW_SNIPER, "user_team": RAW_TEAM,
            }],
        }
        return {"ok": True, "files": [{"id": file_id, "ts": ts}]}


class _FakeBotClient:
    def __init__(self, user_client: _FakeUserClient) -> None:
        self._user = user_client

    def auth_test(self) -> dict[str, Any]:
        return {"user_id": "U0RAWBOT001", "bot_id": "B0RAWBOT001", "team_id": RAW_TEAM}

    def conversations_history(self, channel: str, oldest: str = "", latest: str = "", **_: Any):
        msg = self._user.posted.get(oldest)
        return {"ok": True, "messages": [msg] if msg else []}


def test_capture_runs_scrub_each_shape_with_a_throwaway_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Claim: capture_shape() scrubs every shape with a FRESH load_scrub_map()
    and never saves what it assigned, so two different real uploads captured
    in two runs (or two shapes of one run) both become F0FILE001 -- the
    committed fixtures already show it: photo-and-tag, photo-only and
    multi-image all carry F0FILE002 for three distinct real uploads. Violates
    spec 10 section 8: "one deterministic map ... so cross-references stay
    consistent within and across files" (a placeholder must name one real id)."""
    from tools import capture_fixtures as cf

    committed = tmp_path / "scrub_map.json"
    local = tmp_path / "scrub_map.local.json"

    def _load() -> dict[str, str]:
        return scrub_mod.load_scrub_map(committed, local_path=local)

    def _save(scrub_map: dict[str, str], path: Path | None = None, *, note: str | None = None,
              local_path: Path | None = None) -> None:
        scrub_mod.save_scrub_map(scrub_map, committed, note=note, local_path=local)

    user_client = _FakeUserClient()
    bot_client = _FakeBotClient(user_client)
    monkeypatch.setattr(cf, "FIXTURES_DIR", tmp_path)
    monkeypatch.setattr(cf, "PROVISIONAL_MD", tmp_path / "PROVISIONAL.md")
    monkeypatch.setattr(cf, "CAPTURED_MD", tmp_path / "CAPTURED.md")
    monkeypatch.setattr(cf, "load_scrub_map", _load)
    monkeypatch.setattr(cf, "save_scrub_map", _save)
    monkeypatch.setattr(cf, "_build_clients", lambda b, u: (bot_client, user_client))
    monkeypatch.setattr(cf.time, "sleep", lambda s: None)
    config = SimpleNamespace(channel="C0RAWMAIN01", reports=[], admins=[RAW_TARGET])

    assert cf.run_capture("bot-token-placeholder", "user-token-placeholder", config, "photo-and-tag") == 0
    assert cf.run_capture("bot-token-placeholder", "user-token-placeholder", config, "photo-only") == 0

    first = json.loads((tmp_path / "history" / "photo-and-tag.json").read_text(encoding="utf-8"))
    second = json.loads((tmp_path / "history" / "photo-only.json").read_text(encoding="utf-8"))
    id_first = first["files"][0]["id"]
    id_second = second["files"][0]["id"]
    assert id_first != id_second, (
        f"two different real files both scrubbed to {id_first} across fixtures"
    )


# --- 6. a bot id seen first under edited.user ---------------------------------

def test_bot_id_first_seen_in_edited_user_gets_a_user_placeholder():
    """Claim: the real image-link capture (feed item 8; history/image-link.json)
    carries the user-token bot id in `edited.user`, keyed BEFORE `bot_id`.
    scrub() classifies an id by the key it sits under, not by its own `B`
    prefix, so on first sight the bot id gets a `U0AAA...` user placeholder
    and `bot_id` then reuses it: the fixture's bot_id reads as a member id.
    Violates spec 10 section 8: "bot id (`B...`) -> `B0BOT`" (user ids ->
    `U0AAA...`, bot ids -> `B0BOT...`)."""
    msg = {
        "user": RAW_SNIPER,
        "type": "message",
        "ts": "1790000005.000500",
        "edited": {"user": RAW_APP_BOT, "ts": "1790000006.000000"},
        "bot_id": RAW_APP_BOT,
        "text": f"look <@{RAW_TARGET}>",
    }
    out = scrub(msg, {})

    assert out["bot_id"].startswith("B0BOT"), f"bot id scrubbed to a user placeholder: {out['bot_id']}"
