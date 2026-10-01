"""L1 parse-contract tests (50 §2.1): one test per FX/RF row homed in this file.

Each test loads a provisional fixture, asserts the parse result the row states, and
— where the row names a positive control — a second assertion on a mutated copy of
the payload that lands on the opposite parse-level fact. Downstream `evaluate`
verdicts and sync-level merge behaviour are asserted elsewhere (test_rules /
test_sync); here we pin only what `parse` alone produces.
"""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path

import pytest

from snipebot.parse import Candidate, Digest, ParseAnomaly, parse, rendition_url

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"


def _load(fixtures_dir: Path, *parts: str) -> dict:
    with (fixtures_dir.joinpath(*parts)).open(encoding="utf-8") as fh:
        return json.load(fh)


def _parse(msg: dict) -> object:
    return parse(msg, CHANNEL, BOT)


def _live_image_file(file_id: str) -> dict:
    return {
        "id": file_id,
        "mimetype": "image/jpeg",
        "filetype": "jpg",
        "name": f"{file_id}.jpg",
        "size": 100000,
        "original_w": 1000,
        "original_h": 1000,
        "thumb_1024": f"https://fixture.invalid/files/{file_id}/thumb_1024.jpg",
        "url_private_download": f"https://fixture.invalid/files/{file_id}/download?dl=1",
    }


# --- FX rows ---------------------------------------------------------------


def test_L1_FX_photo_and_tag(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "photo-and-tag.json")
    cand = _parse(msg)
    assert isinstance(cand, Candidate)
    assert cand.live_images == 1
    assert cand.targets == ("U0AAA002",)

    # Positive control: a non-image mimetype is no live image.
    flipped = copy.deepcopy(msg)
    flipped["files"][0]["mimetype"] = "text/plain"
    assert _parse(flipped).live_images == 0


def test_L1_FX_photo_only(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "photo-only.json")
    cand = _parse(msg)
    assert isinstance(cand, Candidate)
    assert cand.live_images == 1
    assert cand.targets == ()

    # Positive control: with no files there is no live image.
    flipped = copy.deepcopy(msg)
    del flipped["files"]
    assert _parse(flipped).live_images == 0


def test_L1_FX_tag_only(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "tag-only.json")
    cand = _parse(msg)
    assert isinstance(cand, Candidate)
    assert cand.live_images == 0
    assert cand.targets == ("U0AAA002",)

    # Positive control: add a live image -> one live image.
    flipped = copy.deepcopy(msg)
    flipped["files"] = [_live_image_file("F0NEW01")]
    assert _parse(flipped).live_images == 1


def test_L1_FX_multi_tag(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "multi-tag.json")
    cand = _parse(msg)
    assert cand.targets == ("U0AAA002", "U0AAA003", "U0AAA004")
    assert len(cand.targets) == 3
    # Positive control: the repeated mention is present twice in text but deduped
    # to a single target (first appearance kept).
    assert msg["text"].count("<@U0AAA002>") == 2
    assert cand.targets.count("U0AAA002") == 1


def test_L1_FX_multi_image(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "multi-image.json")
    cand = _parse(msg)
    assert isinstance(cand, Candidate)
    assert cand.live_images > 1
    assert cand.live_images == 2
    assert cand.targets == ("U0AAA002",)

    # Positive control (conservation): more images still yield ONE candidate,
    # never one per image.
    more = copy.deepcopy(msg)
    more["files"].append(_live_image_file("F0FILE0EX"))
    grown = _parse(more)
    assert isinstance(grown, Candidate)
    assert grown.live_images == 3


def test_L1_FX_ios_multi_photo_share(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "ios-multi-photo-share.json")
    cand = _parse(msg)
    # Provisional shape: one message x N files. The result is a single candidate
    # carrying all N live images, not N candidates.
    assert isinstance(cand, Candidate)
    assert cand.live_images == 3
    assert len(cand.live_image_ids) == 3
    assert cand.live_image_ids == tuple(sorted(cand.live_image_ids))
    assert cand.targets == ("U0AAA003",)


def test_L1_FX_thread_reply(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "thread-reply.json")
    cand = _parse(msg)
    assert cand.thread_ts is not None and cand.thread_ts != cand.ts
    assert cand.is_top_level is False

    # Positive control: a parent-with-replies (thread_ts == ts) is top-level.
    parent = copy.deepcopy(msg)
    parent["thread_ts"] = parent["ts"]
    assert _parse(parent).is_top_level is True


def test_L1_FX_thread_reply_broadcast(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "thread-reply-broadcast.json")
    cand = _parse(msg)
    assert cand.subtype == "thread_broadcast"
    assert cand.thread_ts != cand.ts
    assert cand.is_top_level is False

    # Positive control: dropping thread_ts makes it top-level (broadcast is not
    # top-level by virtue of its subtype).
    flat = copy.deepcopy(msg)
    del flat["thread_ts"]
    assert _parse(flat).is_top_level is True


def test_L1_FX_image_link(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "image-link.json")
    cand = _parse(msg)
    assert cand.linked_images > 0
    assert cand.linked_images == 1
    assert cand.live_images == 0

    # Positive control: an actual upload counts as a live image (links do not).
    uploaded = copy.deepcopy(msg)
    uploaded["files"] = [_live_image_file("F0FILE0UP")]
    up = _parse(uploaded)
    assert up.live_images == 1


def test_L1_FX_mention_in_quote_or_code(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "mention-in-quote-or-code.json")
    cand = _parse(msg)
    # Quoted mention kept; inline-code and fenced-code mentions dropped.
    assert cand.targets == ("U0AAA002",)
    assert "U0AAA003" not in cand.targets
    assert "U0AAA004" not in cand.targets

    # Positive control: the same id outside code IS a target.
    plain = copy.deepcopy(msg)
    plain["text"] = "got <@U0AAA003>"
    del plain["blocks"]
    assert _parse(plain).targets == ("U0AAA003",)


def test_L1_FX_usergroup_mention(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "usergroup-mention.json")
    cand = _parse(msg)
    assert cand.targets == ("U0AAA002",)
    assert "S0TEAM01" not in cand.targets

    # Positive control: a real-user mention in that slot IS a target.
    real = copy.deepcopy(msg)
    real["text"] = "<@U0AAA005> got <@U0AAA002>"
    del real["blocks"]
    assert "U0AAA005" in _parse(real).targets


def test_L1_FX_at_channel(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "at-channel.json")
    cand = _parse(msg)
    assert cand.targets == ("U0AAA002",)

    # Positive control: a real-user mention in that slot IS a target.
    real = copy.deepcopy(msg)
    real["text"] = "<@U0AAA005> game on <@U0AAA002>"
    del real["blocks"]
    assert "U0AAA005" in _parse(real).targets


def test_L1_FX_text_blocks_disagree(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "controls", "text-blocks-disagree.json")
    # text carries U0AAA002 + U0AAA003; blocks drop U0AAA003. parse follows text,
    # warns ParseAnomaly, and does not raise.
    with pytest.warns(ParseAnomaly):
        cand = _parse(msg)
    assert cand.targets == ("U0AAA002", "U0AAA003")
    assert "U0AAA003" in cand.targets  # the block-only reading would drop it

    # Positive control: when text and blocks agree, no anomaly is raised.
    agree = copy.deepcopy(msg)
    del agree["blocks"]  # blocks absent -> no cross-check, no anomaly
    with warnings.catch_warnings():
        warnings.simplefilter("error", ParseAnomaly)
        agreed = _parse(agree)
    assert agreed.targets == ("U0AAA002", "U0AAA003")


def test_L1_FX_heic(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "heic.json")
    cand = _parse(msg)
    assert cand.live_images == 1
    assert len(cand.live_image_ids) == 1

    # Positive control: a non-image mimetype is not a live image.
    flipped = copy.deepcopy(msg)
    flipped["files"][0]["mimetype"] = "application/octet-stream"
    assert _parse(flipped).live_images == 0


def test_L1_FX_gif(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "gif.json")
    cand = _parse(msg)
    assert cand.live_images == 1

    # Positive control: a non-image mimetype is not a live image.
    flipped = copy.deepcopy(msg)
    flipped["files"][0]["mimetype"] = "application/octet-stream"
    assert _parse(flipped).live_images == 0


def test_L1_FX_video(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "video.json")
    cand = _parse(msg)
    assert cand.live_videos > 0
    assert cand.live_videos == 1
    assert cand.live_images == 0

    # Positive control: the same file as an image counts as a live image.
    as_image = copy.deepcopy(msg)
    as_image["files"][0]["mimetype"] = "image/jpeg"
    img = _parse(as_image)
    assert img.live_images == 1
    assert img.live_videos == 0


def test_L1_FX_slack_connect_file(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "slack-connect-file.json")
    cand = _parse(msg)
    assert cand.live_images == 0

    # Positive control: without the Slack Connect stub the file is live.
    flipped = copy.deepcopy(msg)
    del flipped["files"][0]["file_access"]
    assert _parse(flipped).live_images == 1


def test_L1_FX_digest_roundtrip(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "digest.json")
    dig = _parse(msg)
    assert isinstance(dig, Digest)
    assert dig.ts == msg["ts"]
    assert dig.channel == CHANNEL
    payload = msg["metadata"]["event_payload"]
    assert dig.report == payload["report"]
    assert dig.period_key == payload["period_key"]
    assert dig.semester == payload["semester"]
    assert dig.numbers_hash == payload["numbers_hash"]
    assert dig.revision == payload["revision"]

    # The channel arg wins over event_payload.channel, with an anomaly warning.
    with pytest.warns(ParseAnomaly):
        elsewhere = parse(msg, "C0OFF001", BOT)
    assert elsewhere.channel == "C0OFF001"

    # Positive control: without the snipe_digest marker it is not a Digest.
    plain = copy.deepcopy(msg)
    del plain["metadata"]["event_type"]
    assert not isinstance(_parse(plain), Digest)


# --- RF rows (parse-level part; merge behaviour is in test_sync) ------------


def test_L1_RF_tag_edited_in(fixtures_dir: Path) -> None:
    before = _parse(_load(fixtures_dir, "refetch", "tag-edited-in", "before.json"))
    after = _parse(_load(fixtures_dir, "refetch", "tag-edited-in", "after.json"))
    assert before.targets == ("U0AAA002",)
    assert before.first_seen_targets == frozenset({"U0AAA002"})
    # The edited-in target and the edit ts are visible to sync as a Slack ts string.
    assert after.targets == ("U0AAA002", "U0AAA003")
    assert "U0AAA003" not in before.first_seen_targets
    raw_after = _load(fixtures_dir, "refetch", "tag-edited-in", "after.json")
    assert after.last_edit_ts == raw_after["edited"]["ts"]
    assert isinstance(after.last_edit_ts, str)
    assert after.first_sight_edited is True
    # sync appends TargetEdit(U0AAA003, edit_ts) on merge; asserted in test_sync.


def test_L1_RF_tag_edited_out(fixtures_dir: Path) -> None:
    before = _parse(_load(fixtures_dir, "refetch", "tag-edited-out", "before.json"))
    after = _parse(_load(fixtures_dir, "refetch", "tag-edited-out", "after.json"))
    assert before.targets == ("U0AAA002", "U0AAA003")
    # Wholesale replace: the removed tag is gone from the latest observation.
    assert after.targets == ("U0AAA002",)
    assert "U0AAA003" not in after.targets


def test_L1_RF_file_deleted(fixtures_dir: Path) -> None:
    before = _parse(_load(fixtures_dir, "refetch", "file-deleted", "before.json"))
    after = _parse(_load(fixtures_dir, "refetch", "file-deleted", "after.json"))
    assert before.live_images == 1
    # A tombstoned file drops out of the live-image count and id set.
    assert after.live_images == 0
    assert after.live_image_ids == ()


def test_L1_RF_reply_added(fixtures_dir: Path) -> None:
    before = _parse(_load(fixtures_dir, "refetch", "reply-added", "before.json"))
    after = _parse(_load(fixtures_dir, "refetch", "reply-added", "after.json"))
    # The parent gains thread_ts == ts and stays top-level.
    assert before.is_top_level is True
    assert after.thread_ts == after.ts
    assert after.is_top_level is True


# --- rendition_url selector (10 §2 order) ----------------------------------


def test_rendition_url_prefers_thumb_1024(fixtures_dir: Path) -> None:
    msg = _load(fixtures_dir, "history", "photo-and-tag.json")
    file = msg["files"][0]
    assert rendition_url(file) == file["thumb_1024"]
    # Falls through the order to url_private_download, then None.
    trimmed = {k: v for k, v in file.items() if not k.startswith("thumb_")}
    assert rendition_url(trimmed) == file["url_private_download"]
    assert rendition_url({"id": "F0NONE"}) is None
