"""Correctness regressions for the local photo review tool (snipebot/review.py), each
from a confirmed break; the docstring states the defect the test pins down. Slack is a small hand-written fake (history + fetch only), the
detector a fake with detect_boxes; nothing touches the network.
"""

from __future__ import annotations

import calendar
from pathlib import Path

import pytest

from snipebot import review
from snipebot.slack_io import AuthIdentity, SlackTransportError
from snipebot.ts import US_PER_SECOND

from tests._helpers_sync import image_file

CHANNEL = "C0MAIN01"
BOT = "U0BOT"
SNIPER = "U0AAAA1"
TARGET = "U0BBBB1"


def _secs(y, mo, d, h=0) -> int:
    return calendar.timegm((y, mo, d, h, 0, 0, 0, 0, 0))


def _ts(y, mo, d, h=0) -> str:
    return f"{_secs(y, mo, d, h)}.000100"


FALL_START = _secs(2026, 9, 1) * US_PER_SECOND
FALL_END = (_secs(2026, 12, 21) * US_PER_SECOND) - 1
TS_A = _ts(2026, 9, 10, 12)
TS_B = _ts(2026, 9, 11, 12)


class _IO:
    """History and fetch only, from a mutable message list."""

    def __init__(self, messages, fetch_error=None):
        self.messages = list(messages)
        self.fetch_error = fetch_error

    def auth_identity(self):
        return AuthIdentity(user_id=BOT, bot_id="B0BOT", team_id="T0TEAM",
                            url="https://fake.slack.example")

    def history(self, channel, oldest, latest=None):
        return [dict(m) for m in self.messages]

    def fetch_file_bytes(self, url):
        if self.fetch_error is not None:
            raise self.fetch_error
        for m in self.messages:
            for f in m.get("files", []):
                if url in (f.get("thumb_1024"), f.get("url_private_download")):
                    return f["_bytes"]
        raise KeyError(url)


class _Detector:
    """A fake detector: every image has the given boxes in a 100x100 frame."""

    def __init__(self, boxes=()):
        self.boxes = list(boxes)

    def detect_boxes(self, data):
        return list(self.boxes), 100, 100


def _msg(ts, files, text=f"<@{TARGET}>"):
    return {"type": "message", "ts": ts, "user": SNIPER, "text": text, "files": files}


def _cache():
    return {"version": review.CACHE_VERSION, "posts": {}}


def _scan(io, cache, detector=None):
    return review.scan(io, CHANNEL, f"{FALL_START // US_PER_SECOND}.000000",
                       f"{FALL_END // US_PER_SECOND}.999999", detector or _Detector(),
                       cache, BOT)


def _queue_ts(cache):
    return [it.ts for it in review.build_queue(cache, {}, 900, 5, FALL_START, FALL_END)]


# --- C1: scan forgets a post it no longer sees -----------------------------

def test_C1_deleted_message_stays_queued():
    """C1: a message deleted from Slack used to keep its faces.json entry forever, so
    build_queue (and stats) still listed it with no URL to show its image."""
    io = _IO([_msg(TS_A, [image_file("F01", b"one")])])
    cache = _cache()
    _scan(io, cache)
    assert _queue_ts(cache) == [TS_A]
    io.messages = []  # the post was deleted in Slack
    result = _scan(io, cache)
    assert result.posts == 0
    assert TS_A not in _queue_ts(cache)


def test_C1_all_images_deleted_stays_queued():
    """C1: a post whose every image is deleted (tombstoned) used to be skipped before
    pruning, keeping its stale boxes in the cache and the post in the queue."""
    f = image_file("F01", b"one")
    io = _IO([_msg(TS_A, [f])])
    cache = _cache()
    _scan(io, cache)
    io.messages = [_msg(TS_A, [dict(f, is_tombstoned=True)])]
    _scan(io, cache)
    assert TS_A not in _queue_ts(cache)


def test_C1_tags_removed_keeps_stale_targets():
    """C1: an edit that removes every tag used to leave the post queued with its old
    tag count."""
    io = _IO([_msg(TS_A, [image_file("F01", b"one")])])
    cache = _cache()
    _scan(io, cache)
    io.messages = [_msg(TS_A, [image_file("F01", b"one")], text="nice shot")]
    _scan(io, cache)
    assert TS_A not in _queue_ts(cache)


def test_C1_deleted_original_makes_repost_a_repeat():
    """C1: the stale entry of a deleted post used to feed sha_index, so re-posting the
    same photo (delete, post again) was flagged repeat_photo against a post that no
    longer exists."""
    io = _IO([_msg(TS_A, [image_file("F01", b"same")])])
    cache = _cache()
    _scan(io, cache)
    io.messages = [_msg(TS_B, [image_file("F02", b"same")])]
    _scan(io, cache)
    items = {it.ts: it for it in review.build_queue(cache, {}, 900, 5, FALL_START, FALL_END)}
    assert review.REPEAT not in items[TS_B].reasons


def test_C1_failed_history_read_prunes_nothing():
    """C1: pruning happens only after a complete history read; a Slack fault mid-read
    leaves every cached entry for the caller's save."""
    io = _IO([_msg(TS_A, [image_file("F01", b"one")])])
    cache = _cache()
    _scan(io, cache)

    def broken(channel, oldest, latest=None):
        raise SlackTransportError("down")

    io.history = broken
    with pytest.raises(SlackTransportError):
        _scan(io, cache)
    assert _queue_ts(cache) == [TS_A]


def test_C1_posts_outside_the_window_are_kept():
    """C1: only the scanned window is pruned; another semester's cached posts stay."""
    spring = _ts(2026, 4, 10, 12)
    cache = _cache()
    cache["posts"][spring] = {"targets": 1, "images": {"F09": {
        "sha": "abcd", "w": 100, "h": 100, "boxes": []}}}
    _scan(_IO([]), cache)
    assert spring in cache["posts"]


# --- C4: score_milli is floored, never rounded up to the threshold -------------------------------

def test_C4_score_rounding_counts_subthreshold_face_as_clear():
    """C4: detect_boxes used to store round(score * 1000), so a box scoring 0.8996 became
    900 and counted as a clear face at "0.9", although the live detector drops it."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from snipebot.faces import YuNetDetector

    det = YuNetDetector.__new__(YuNetDetector)
    row = np.array([[10, 10, 20, 20] + [0] * 10 + [0.8996]], dtype=np.float32)
    det._sane_faces = lambda data: (row, 100, 100)
    boxes, _w, _h = det.detect_boxes(b"x")
    post = {"targets": 1, "images": {"F01": {"sha": "s", "w": 100, "h": 100,
                                             "boxes": [list(b) for b in boxes]}}}
    why = review.reasons_for(TS_A, post, review.threshold_milli("0.9"), None, {})
    assert review.NO_FACE in why


@pytest.mark.parametrize("score, milli", [(0.8996, 899), (0.95, 950), (0.5, 500), (1.0, 1000)])
def test_C4_score_milli_is_floored(score, milli):
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from snipebot.faces import YuNetDetector

    det = YuNetDetector.__new__(YuNetDetector)
    row = np.array([[10, 10, 20, 20] + [0] * 10 + [score]], dtype=np.float32)
    det._sane_faces = lambda data: (row, 100, 100)
    (box,), _w, _h = det.detect_boxes(b"x")
    assert box[4] == milli and type(box[4]) is int


# --- C6: clear and faint counts aggregate the same way ---------------------------------

def test_C6_faint_faces_summed_while_clear_faces_maxed():
    """C6: faint_faces used to be summed over images while clear_faces was the max, so a
    two-photo post with one clear and one faint face in each read "1 clear face, 2 faint"."""
    img = {"w": 100, "h": 100, "boxes": [[1, 1, 9, 9, 950], [20, 20, 9, 9, 700]]}
    cache = {"version": review.CACHE_VERSION, "posts": {
        TS_A: {"targets": 3, "images": {"F01": dict(img, sha="s1"),
                                        "F02": dict(img, sha="s2")}}}}
    (item,) = review.build_queue(cache, {}, 900, None, FALL_START, FALL_END)
    assert item.clear_faces == 1
    assert item.faint_faces == 1
