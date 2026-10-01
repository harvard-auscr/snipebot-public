"""Local photo review tests (snipebot/review.py and the `review` CLI command).

The review tool reads Slack history, runs the face detector on every tagged photo post
and keeps the boxes in a local folder; the owner's yes/no labels are calibration data
only. These tests cover the queue reasons and their thresholds, the queue order and its
inclusive semester window, the label and cache files, `scan` against a `FakeSlack` world
with a box-returning fake detector, the `review list|stats|label|open` subcommands through
`main()` with injected factories, the gallery server on 127.0.0.1 (port 0, shut down by
the test), and the guard that a review run never touches the ledger, state or any Slack
write. The one slow test checks `detect_boxes` against `count_faces` on the photo
fixtures and is skipped without cv2.
"""

from __future__ import annotations

import calendar
import hashlib
import http.server
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from snipebot import cli, review
from snipebot.cli import Exit, main
from snipebot.ts import US_PER_SECOND, TsFormatError, format_ts, parse_ts

from tests.controls import faces_fixtures
from tests.controls.faces_fixtures import EXPECTED_COUNTS
from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file

CHANNEL = "C0MAIN01"
BOT = "U0BOT"
ALEX = "U0AAAA1"
BAILEY = "U0BBBB1"
CASEY = "U0CCCC1"
WORKSPACE = "https://fake.slack.example"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = parse_ts(NOW_TS)
SEM_START = _ts(2026, 9, 1)
SEM_END = _ts(2026, 12, 20, 23, 59, 59, 999_999)


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class BoxDetector:
    """detect_boxes keyed by the exact image bytes; an Exception value is raised (a decode
    fault), unknown bytes raise KeyError. count_faces must never be reached from review."""

    def __init__(self, table: dict) -> None:
        self.table = dict(table)
        self.calls: list[bytes] = []

    def detect_boxes(self, data: bytes):
        self.calls.append(data)
        value = self.table[data]
        if isinstance(value, Exception):
            raise value
        return value

    def count_faces(self, data: bytes) -> int:  # pragma: no cover - a failure if reached
        raise AssertionError("review must not call count_faces")


class GuardSlack(FakeSlack):
    """A FakeSlack whose every write method records the call and raises."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.writes: list[str] = []

    def _refuse(self, name: str):
        self.writes.append(name)
        raise AssertionError(f"review called the Slack write method {name}")

    def post_message(self, *a, **k):
        return self._refuse("post_message")

    def update_message(self, *a, **k):
        return self._refuse("update_message")

    def reactions_add(self, *a, **k):
        return self._refuse("reactions_add")

    def reactions_remove(self, *a, **k):
        return self._refuse("reactions_remove")


def _users() -> dict:
    return {
        BOT: FakeUser(id=BOT, is_bot=True),
        ALEX: FakeUser(id=ALEX, display_name="Alex"),
        BAILEY: FakeUser(id=BAILEY, display_name="Bailey"),
        CASEY: FakeUser(id=CASEY, display_name="Casey"),
    }


def _slack(cls=FakeSlack) -> FakeSlack:
    return cls(now=NOW_TS, bot_user_id=BOT, users=_users())


# Boxes are (x, y, w, h, score_milli); the frame is 100 x 80.
ONE_CLEAR = ([(10, 10, 20, 20, 950)], 100, 80)
NO_BOXES = ([], 100, 80)
ONE_FAINT = ([(5, 5, 10, 10, 700)], 100, 80)
ONE_MID = ([(5, 5, 10, 10, 850)], 100, 80)
TWO_CLEAR = ([(1, 1, 10, 10, 990), (40, 1, 10, 10, 920)], 100, 80)


# --------------------------------------------------------------------------- #
# Hand-built cache posts
# --------------------------------------------------------------------------- #

def _img(sha: str, *boxes) -> dict:
    return {"sha": sha, "w": 100, "h": 80, "boxes": [list(b) for b in boxes]}


def _post(targets: int, **images) -> dict:
    return {"targets": targets, "images": dict(images)}


def _box(score: int) -> tuple:
    return (1, 2, 3, 4, score)


# --------------------------------------------------------------------------- #
# threshold_milli
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, milli", [("0.9", 900), ("0.5", 500), ("1.0", 1000),
                                         ("0.95", 950), ("0.85", 850)])
def test_threshold_milli(text, milli):
    assert review.threshold_milli(text) == milli


def test_threshold_milli_rejects_non_decimal():
    with pytest.raises(review.ReviewError):
        review.threshold_milli("high")


# --------------------------------------------------------------------------- #
# reasons_for
# --------------------------------------------------------------------------- #

def test_reasons_no_images_is_empty():
    assert review.reasons_for("1.000001", _post(3), 900, 2, {}) == []


def test_reasons_matching_faces_is_empty():
    post = _post(2, F1=_img("a", _box(950), _box(900)))
    assert review.reasons_for("1.000001", post, 900, 5, {"a": {"1.000001"}}) == []


def test_reasons_no_clear_face():
    post = _post(1, F1=_img("a", _box(899), _box(650)))
    assert review.reasons_for("1.000001", post, 900, None, {}) == [review.NO_FACE]


def test_reasons_no_face_with_no_boxes_at_all():
    post = _post(1, F1=_img("a"))
    assert review.reasons_for("1.000001", post, 900, None, {}) == [review.NO_FACE]


def test_reasons_box_at_exactly_the_threshold_is_clear():
    post = _post(1, F1=_img("a", _box(900)))
    assert review.reasons_for("1.000001", post, 900, None, {}) == []
    assert review.reasons_for("1.000001", post, 901, None, {}) == [review.NO_FACE]


def test_reasons_fewer_faces_than_tags():
    post = _post(3, F1=_img("a", _box(950), _box(910)))
    assert review.reasons_for("1.000001", post, 900, None, {}) == [review.FEWER]


def test_reasons_extra_faces():
    post = _post(1, F1=_img("a", _box(950), _box(910)))
    assert review.reasons_for("1.000001", post, 900, None, {}) == [review.EXTRA]


def test_reasons_clear_count_is_the_best_image_not_the_sum():
    # One clear face in each of two photos of one person: clear is 1, not 2.
    post = _post(1, F1=_img("a", _box(950)), F2=_img("b", _box(960)))
    assert review.reasons_for("1.000001", post, 900, None, {}) == []
    # A post of two people across two one-face photos still reads as fewer.
    post2 = _post(2, F1=_img("a", _box(950)), F2=_img("b", _box(960)))
    assert review.reasons_for("1.000001", post2, 900, None, {}) == [review.FEWER]


def test_reasons_many_tags_threshold_is_inclusive():
    post = _post(4, F1=_img("a", *[_box(950)] * 4))
    assert review.reasons_for("1.000001", post, 900, 4, {}) == [review.MANY_TAGS]
    assert review.reasons_for("1.000001", post, 900, 5, {}) == []


def test_reasons_many_tags_off_when_min_targets_null():
    post = _post(40, F1=_img("a", *[_box(950)] * 40))
    assert review.reasons_for("1.000001", post, 900, None, {}) == []


def test_reasons_repeat_photo_needs_a_second_post():
    post = _post(1, F1=_img("a", _box(950)))
    assert review.reasons_for("1.000001", post, 900, None, {"a": {"1.000001"}}) == []
    both = {"a": {"1.000001", "2.000001"}}
    assert review.reasons_for("1.000001", post, 900, None, both) == [review.REPEAT]


def test_reasons_same_bytes_twice_in_one_post_is_not_a_repeat():
    post = _post(1, F1=_img("a", _box(950)), F2=_img("a", _box(950)))
    index = review.sha_index({"posts": {"1.000001": post}})
    assert index == {"a": {"1.000001"}}
    assert review.reasons_for("1.000001", post, 900, None, index) == []


def test_reasons_come_out_in_priority_order():
    post = _post(6, F1=_img("a", _box(500)))
    index = {"a": {"1.000001", "2.000001"}}
    assert review.reasons_for("1.000001", post, 900, 5, index) == [
        review.REPEAT, review.MANY_TAGS, review.NO_FACE]


def test_reason_text_covers_every_reason():
    assert set(review.REASON_TEXT) == set(review.REASONS)


# --------------------------------------------------------------------------- #
# build_queue
# --------------------------------------------------------------------------- #

def _queue_cache() -> dict:
    return {"version": review.CACHE_VERSION, "posts": {
        # no clear face, newest
        "1000.000003": _post(1, F1=_img("n3", _box(600))),
        # no clear face, older
        "1000.000001": _post(1, F1=_img("n1")),
        # extra faces (lowest priority), newest of all
        "1000.000009": _post(1, F1=_img("x9", _box(950), _box(950))),
        # repeat photo (highest priority), oldest
        "999.000000": _post(1, F1=_img("dup", _box(950))),
        "999.000001": _post(1, F1=_img("dup", _box(950))),
        # fine: no reason
        "1000.000005": _post(1, F1=_img("ok", _box(950))),
        # no live images: never listed, even with everything
        "1000.000006": _post(1),
    }}


def test_build_queue_order_unlabeled_then_priority_then_newest():
    cache = _queue_cache()
    items = review.build_queue(cache, {}, 900, None, 0, 10**12)
    assert [i.ts for i in items] == [
        "999.000001", "999.000000",      # repeat_photo, newest first
        "1000.000003", "1000.000001",    # no_clear_face, newest first
        "1000.000009",                   # extra_faces
    ]
    assert all(i.label is None for i in items)


def test_build_queue_labeled_items_sink_below_unlabeled():
    cache = _queue_cache()
    labels = {"999.000001": "yes", "1000.000003": "no"}
    items = review.build_queue(cache, labels, 900, None, 0, 10**12)
    assert [i.ts for i in items] == [
        "999.000000", "1000.000001", "1000.000009",
        "999.000001", "1000.000003",
    ]
    assert [i.label for i in items][-2:] == ["yes", "no"]


def test_build_queue_everything_adds_reasonless_posts_last_in_their_group():
    cache = _queue_cache()
    items = review.build_queue(cache, {}, 900, None, 0, 10**12, everything=True)
    assert [i.ts for i in items][-1] == "1000.000005"
    assert items[-1].reasons == ()
    assert "1000.000006" not in {i.ts for i in items}


def test_build_queue_item_fields():
    cache = {"version": 1, "posts": {
        "5.000000": _post(3, F1=_img("a", _box(950), _box(700), _box(599)),
                          F2=_img("b", _box(960), _box(910), _box(650))),
    }}
    [item] = review.build_queue(cache, {"5.000000": "no"}, 900, 3, 0, 10**12)
    assert item.targets == 3
    assert item.clear_faces == 2       # the best single photo
    assert item.faint_faces == 1       # the best single photo too; 599 is below the floor
    assert item.label == "no"
    assert item.reasons == (review.MANY_TAGS, review.FEWER)


def test_build_queue_window_is_inclusive():
    start, end = "2000.000000", "3000.000000"
    cache = {"version": 1, "posts": {
        "1999.999999": _post(1, F1=_img("a")),
        start: _post(1, F1=_img("b")),
        "2500.000000": _post(1, F1=_img("c")),
        end: _post(1, F1=_img("d")),
        "3000.000001": _post(1, F1=_img("e")),
    }}
    items = review.build_queue(cache, {}, 900, None, parse_ts(start), parse_ts(end))
    assert sorted(i.ts for i in items) == sorted([start, "2500.000000", end])


def test_build_queue_live_threshold_changes_reasons_without_rescan():
    cache = {"version": 1, "posts": {"5.000000": _post(1, F1=_img("a", _box(850)))}}
    assert review.build_queue(cache, {}, 900, None, 0, 10**12)[0].reasons == (review.NO_FACE,)
    assert review.build_queue(cache, {}, 800, None, 0, 10**12) == []


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

def test_labels_missing_file_is_empty(tmp_path):
    assert review.load_labels(tmp_path / "nowhere") == {}


def test_labels_round_trip_latest_wins_and_clear_removes(tmp_path):
    folder = tmp_path / "review"
    review.append_label(folder, "100.000001", "yes", now_s=1)
    review.append_label(folder, "100.000002", "no", now_s=2)
    review.append_label(folder, "100.000001", "no", now_s=3)
    review.append_label(folder, "100.000002", "clear", now_s=4)
    review.append_label(folder, "100.000003", "clear", now_s=5)  # clearing nothing is fine
    assert review.load_labels(folder) == {"100.000001": "no"}
    review.append_label(folder, "100.000002", "yes", now_s=6)
    assert review.load_labels(folder) == {"100.000001": "no", "100.000002": "yes"}


def test_labels_file_holds_only_ts_label_at_with_lf(tmp_path):
    folder = tmp_path / "review"
    review.append_label(folder, "100.000001", "yes", now_s=1700000000)
    raw = (folder / "labels.jsonl").read_bytes()
    assert b"\r\n" not in raw
    [line] = raw.decode("utf-8").splitlines()
    assert json.loads(line) == {"ts": "100.000001", "label": "yes", "at": 1700000000}


def test_labels_default_time_is_integer_seconds(tmp_path):
    review.append_label(tmp_path, "100.000001", "yes")
    rec = json.loads((tmp_path / "labels.jsonl").read_text(encoding="utf-8"))
    assert isinstance(rec["at"], int) and rec["at"] > 1_600_000_000


@pytest.mark.parametrize("ts", ["1.0", "abc", "", "100.0000001", "-1.000000"])
def test_append_label_rejects_bad_ts(tmp_path, ts):
    with pytest.raises(TsFormatError):
        review.append_label(tmp_path, ts, "yes")
    assert not (tmp_path / "labels.jsonl").exists()


def test_append_label_rejects_unknown_label(tmp_path):
    with pytest.raises(review.ReviewError):
        review.append_label(tmp_path, "100.000001", "maybe")
    assert not (tmp_path / "labels.jsonl").exists()


def test_labels_blank_lines_are_skipped(tmp_path):
    (tmp_path / "labels.jsonl").write_bytes(
        b'\n{"ts": "1.000001", "label": "yes", "at": 1}\n   \n')
    assert review.load_labels(tmp_path) == {"1.000001": "yes"}


@pytest.mark.parametrize("line", [
    b"not json",
    b'{"label": "yes"}',
    b'{"ts": "1.000001"}',
    b"[1, 2]",
    b'"a string"',
    b"null",
    b'{"ts": "1.000001", "label": "maybe"}',
])
def test_labels_malformed_line_raises_with_line_number(tmp_path, line):
    (tmp_path / "labels.jsonl").write_bytes(
        b'{"ts": "1.000001", "label": "yes", "at": 1}\n' + line + b"\n")
    with pytest.raises(review.ReviewError, match=r"labels\.jsonl:2"):
        review.load_labels(tmp_path)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def test_cache_missing_is_empty_current_version(tmp_path):
    assert review.load_cache(tmp_path / "nowhere") == {"version": review.CACHE_VERSION,
                                                       "posts": {}}


def test_cache_round_trip(tmp_path):
    folder = tmp_path / "review"
    cache = {"version": review.CACHE_VERSION, "posts": {
        "100.000001": _post(2, F1=_img("abcd", _box(950), _box(610))),
    }}
    review.save_cache(folder, cache)
    assert review.load_cache(folder) == cache
    assert not (folder / "faces.json.tmp").exists()


def test_cache_file_uses_lf(tmp_path):
    review.save_cache(tmp_path, {"version": review.CACHE_VERSION,
                                 "posts": {"1.000001": _post(1, F1=_img("a", _box(950)))}})
    assert b"\r\n" not in (tmp_path / "faces.json").read_bytes()


@pytest.mark.parametrize("body", [
    '{"version": 3, "posts": {}}',
    '{"version": 0, "posts": {}}',
    '{"posts": {}}',
    '{"version": 2}',
    '{"version": 2, "posts": []}',
    '[1, 2]',
    '{not json',
])
def test_cache_wrong_version_or_shape_raises(tmp_path, body):
    (tmp_path / "faces.json").write_text(body, encoding="utf-8")
    with pytest.raises(review.ReviewError):
        review.load_cache(tmp_path)


def test_cache_from_an_older_detector_starts_over(tmp_path):
    """Version 1 rounded scores (0.8996 read as a clear 900); its boxes are not reused."""
    (tmp_path / "faces.json").write_text(
        '{"version": 1, "posts": {"10.000000": {"targets": 1, "images": {}}}}',
        encoding="utf-8")
    assert review.load_cache(tmp_path) == {"version": review.CACHE_VERSION, "posts": {}}


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #

def _scan(slack, detector, cache, oldest=SEM_START, latest=SEM_END, **kw):
    return review.scan(slack, CHANNEL, oldest, latest, detector, cache, BOT, **kw)


def _empty_cache() -> dict:
    return {"version": review.CACHE_VERSION, "posts": {}}


def test_scan_caches_boxes_per_live_image():
    slack = _slack()
    ts = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                    text=f"got <@{BAILEY}>", files=[image_file("F01", b"img-one")])
    det = BoxDetector({b"img-one": ONE_CLEAR})
    cache = _empty_cache()
    result = _scan(slack, det, cache)
    assert (result.posts, result.detected, result.failed) == (1, 1, 0)
    assert cache["posts"] == {ts: {"targets": 1, "unread": 0, "images": {"F01": {
        "sha": sha16(b"img-one"), "w": 100, "h": 80, "boxes": [[10, 10, 20, 20, 950]]}}}}
    assert result.urls == {(ts, "F01"): "https://files.example/F01"}


def test_scan_does_not_redetect_cached_images():
    slack = _slack()
    ts = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                    text=f"<@{BAILEY}>", files=[image_file("F01", b"img-one")])
    det = BoxDetector({b"img-one": ONE_CLEAR})
    cache = _empty_cache()
    _scan(slack, det, cache)
    again = _scan(slack, det, cache)
    assert (again.posts, again.detected, again.failed) == (1, 0, 0)
    assert len(det.calls) == 1
    # The URL map is rebuilt every scan so the gallery can still fetch the photo.
    assert again.urls == {(ts, "F01"): "https://files.example/F01"}


def test_scan_survives_a_save_and_load(tmp_path):
    slack = _slack()
    slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
               text=f"<@{BAILEY}>", files=[image_file("F01", b"img-one")])
    det = BoxDetector({b"img-one": ONE_CLEAR})
    cache = review.load_cache(tmp_path)
    _scan(slack, det, cache)
    review.save_cache(tmp_path, cache)
    reloaded = review.load_cache(tmp_path)
    assert reloaded == cache
    assert _scan(slack, det, reloaded).detected == 0
    assert len(det.calls) == 1


def test_scan_drops_images_no_longer_live():
    slack = _slack()
    ts = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                    text=f"<@{BAILEY}>",
                    files=[image_file("F01", b"img-one"), image_file("F02", b"img-two")])
    det = BoxDetector({b"img-one": ONE_CLEAR, b"img-two": NO_BOXES})
    cache = _empty_cache()
    _scan(slack, det, cache)
    assert set(cache["posts"][ts]["images"]) == {"F01", "F02"}
    slack.delete_file(at=_ts(2026, 9, 11), ts=ts, channel=CHANNEL, file_index=1)
    result = _scan(slack, det, cache)
    assert set(cache["posts"][ts]["images"]) == {"F01"}
    assert result.detected == 0 and len(det.calls) == 2
    assert set(result.urls) == {(ts, "F01")}


def test_scan_drops_a_post_whose_only_photo_was_deleted():
    slack = _slack()
    ts = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                    text=f"<@{BAILEY}>", files=[image_file("F01", b"img-two")])
    det = BoxDetector({b"img-two": NO_BOXES})
    cache = _empty_cache()
    _scan(slack, det, cache)
    assert review.build_queue(cache, {}, 900, None, 0, NOW_US)[0].ts == ts
    slack.delete_file(at=_ts(2026, 9, 11), ts=ts, channel=CHANNEL, file_index=0)
    _scan(slack, det, cache)
    assert review.build_queue(cache, {}, 900, None, 0, NOW_US, everything=True) == []


def test_scan_counts_fetch_failures_and_retries_next_scan():
    slack = _slack()
    a = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                   text=f"<@{BAILEY}>", files=[image_file("F01", b"img-one")])
    b = slack.post(at=_ts(2026, 9, 10, 10), user=ALEX, channel=CHANNEL,
                   text=f"<@{CASEY}>", files=[image_file("F02", b"img-two")])
    det = BoxDetector({b"img-one": ONE_CLEAR, b"img-two": NO_BOXES})
    slack.faults.fetch_timeout(times=1)  # the first fetch (newest post first) fails
    cache = _empty_cache()
    first = _scan(slack, det, cache)
    assert (first.posts, first.detected, first.failed) == (2, 1, 1)
    assert cache["posts"][b]["images"] == {}
    assert "F01" in cache["posts"][a]["images"]
    # A post whose only photo failed is queued as unreadable meanwhile.
    queue = review.build_queue(cache, {}, 900, None, 0, NOW_US, everything=True)
    assert [(i.ts, i.reasons) for i in queue] == [(b, (review.UNREAD,)), (a, ())]
    second = _scan(slack, det, cache)
    assert (second.posts, second.detected, second.failed) == (2, 1, 0)
    assert "F02" in cache["posts"][b]["images"]


def test_scan_counts_detector_failures_and_retries():
    slack = _slack()
    ts = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                    text=f"<@{BAILEY}>", files=[image_file("F01", b"img-one")])
    det = BoxDetector({b"img-one": ValueError("cannot decode")})
    cache = _empty_cache()
    result = _scan(slack, det, cache)
    assert (result.detected, result.failed) == (0, 1)
    assert cache["posts"][ts]["images"] == {}
    det.table[b"img-one"] = ONE_CLEAR
    result = _scan(slack, det, cache)
    assert (result.detected, result.failed) == (1, 0)


def test_scan_skips_untagged_self_only_photoless_and_bot_posts():
    slack = _slack()
    slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
               text="no tag here", files=[image_file("F01", b"untagged")])
    slack.post(at=_ts(2026, 9, 10, 10), user=ALEX, channel=CHANNEL,
               text=f"me <@{ALEX}>", files=[image_file("F02", b"self")])
    slack.post(at=_ts(2026, 9, 10, 11), user=ALEX, channel=CHANNEL,
               text=f"<@{BAILEY}> no photo")
    slack.post(at=_ts(2026, 9, 10, 12), user=BOT, channel=CHANNEL,
               text=f"<@{BAILEY}>", files=[image_file("F03", b"bot")])
    slack.post(at=_ts(2026, 9, 10, 13), user=ALEX, channel=CHANNEL, text=f"<@{BAILEY}>",
               files=[{**image_file("F04", b"video"), "mimetype": "video/mp4"}])
    det = BoxDetector({})
    cache = _empty_cache()
    result = _scan(slack, det, cache)
    assert (result.posts, result.detected, result.failed) == (0, 0, 0)
    assert cache["posts"] == {} and det.calls == []


def test_scan_counts_targets_without_the_sender():
    slack = _slack()
    ts = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                    text=f"<@{ALEX}> <@{BAILEY}> <@{CASEY}>",
                    files=[image_file("F01", b"img-one")])
    det = BoxDetector({b"img-one": TWO_CLEAR})
    cache = _empty_cache()
    _scan(slack, det, cache)
    assert cache["posts"][ts]["targets"] == 2


def test_scan_refreshes_tag_count_after_an_edit():
    slack = _slack()
    ts = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                    text=f"<@{BAILEY}>", files=[image_file("F01", b"img-one")])
    det = BoxDetector({b"img-one": TWO_CLEAR})
    cache = _empty_cache()
    _scan(slack, det, cache)
    assert cache["posts"][ts]["targets"] == 1
    slack.edit(at=_ts(2026, 9, 10, 9, 5), ts=ts, channel=CHANNEL, user=ALEX,
               text=f"<@{BAILEY}> <@{CASEY}>")
    _scan(slack, det, cache)
    assert cache["posts"][ts]["targets"] == 2
    assert len(det.calls) == 1


def test_scan_reads_only_the_window():
    slack = _slack()
    slack.post(at=_ts(2026, 8, 25), user=ALEX, channel=CHANNEL,
               text=f"<@{BAILEY}>", files=[image_file("F01", b"before")])
    inside = slack.post(at=_ts(2026, 9, 2), user=ALEX, channel=CHANNEL,
                        text=f"<@{BAILEY}>", files=[image_file("F02", b"inside")])
    det = BoxDetector({b"inside": ONE_CLEAR})
    cache = _empty_cache()
    result = _scan(slack, det, cache)
    assert result.posts == 1 and list(cache["posts"]) == [inside]
    assert det.calls == [b"inside"]


def test_scan_same_bytes_in_two_posts_is_a_repeat():
    slack = _slack()
    a = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                   text=f"<@{BAILEY}>", files=[image_file("F01", b"same")])
    b = slack.post(at=_ts(2026, 9, 11, 9), user=CASEY, channel=CHANNEL,
                   text=f"<@{BAILEY}>", files=[image_file("F02", b"same")])
    det = BoxDetector({b"same": ONE_CLEAR})
    cache = _empty_cache()
    _scan(slack, det, cache)
    items = review.build_queue(cache, {}, 900, None, 0, NOW_US)
    assert [i.ts for i in items] == [b, a]
    assert all(i.reasons == (review.REPEAT,) for i in items)


def test_scan_progress_every_25_posts():
    slack = _slack()
    table = {}
    for n in range(26):
        data = f"img-{n}".encode()
        table[data] = ONE_CLEAR
        slack.post(at=_ts(2026, 9, 5, 0, n), user=ALEX, channel=CHANNEL,
                   text=f"<@{BAILEY}>", files=[image_file(f"F{n:02d}", data)])
    calls = []
    result = _scan(slack, BoxDetector(table), _empty_cache(),
                   progress=lambda seen, detected: calls.append((seen, detected)))
    assert result.posts == 26
    assert calls == [(25, 25)]


def test_scan_cache_holds_no_names_ids_urls_or_bytes(tmp_path):
    slack = _slack()
    slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
               text=f"Alex got <@{BAILEY}>", files=[image_file("F01", b"secret-bytes")])
    det = BoxDetector({b"secret-bytes": ONE_CLEAR})
    cache = _empty_cache()
    _scan(slack, det, cache)
    review.save_cache(tmp_path, cache)
    text = (tmp_path / "faces.json").read_text(encoding="utf-8")
    for needle in ("U0", "Alex", "Bailey", "http", "files.example", "secret-bytes",
                   hashlib.sha256(b"secret-bytes").hexdigest()):
        assert needle not in text


# --------------------------------------------------------------------------- #
# stats, permalink, gallery payload
# --------------------------------------------------------------------------- #

def test_stats_lines_tables():
    cache = {"version": 1, "posts": {
        "10.000000": _post(1, F1=_img("a", _box(700))),   # no face at 0.9, face at 0.7
        "11.000000": _post(1, F1=_img("b", _box(950))),   # fine
        "12.000000": _post(2, F1=_img("c", _box(950))),   # fewer
    }}
    labels = {"10.000000": "no", "12.000000": "yes"}
    lines = review.stats_lines(cache, labels, 900, None, 0, 10**12)
    assert lines[0] == "posts 3  labeled 2  yes 1  no 1"
    rows = {line.split()[0]: line.split()[1:] for line in lines[3:10]}
    assert rows[review.UNREAD] == ["0", "0", "0", "0"]
    assert rows[review.NO_FACE] == ["1", "0", "1", "0"]
    assert rows[review.FEWER] == ["1", "1", "0", "0"]
    assert rows["(none)"] == ["1", "0", "0", "1"]
    assert rows[review.REPEAT] == ["0", "0", "0", "0"]
    threshold_rows = {line.split()[0]: line for line in lines if line.startswith("  0.")}
    assert list(threshold_rows) == [f"0.{m:03d}" for m in range(500, 1000, 50)]
    assert threshold_rows["0.900"].endswith("<- live")
    assert threshold_rows["0.700"].split()[1:4] == ["0", "0", "0"]
    assert threshold_rows["0.750"].split()[1:4] == ["1", "0", "1"]
    assert sum("<- live" in line for line in lines) == 1
    assert lines[-2:] == ["labeled no (scores unchanged; veto to remove one):", "  10.000000"]


def test_stats_lines_ignore_labels_outside_window():
    cache = {"version": 1, "posts": {"10.000000": _post(1, F1=_img("a"))}}
    lines = review.stats_lines(cache, {"10.000000": "no"}, 900, None,
                               parse_ts("11.000000"), parse_ts("12.000000"))
    assert lines[0] == "posts 0  labeled 0  yes 0  no 0"
    assert not any("labeled no" in line for line in lines)


def test_permalink():
    assert review.permalink("https://ws.slack.com/", "C0MAIN01", "1758189600.000100") == \
        "https://ws.slack.com/archives/C0MAIN01/p1758189600000100"


def test_gallery_payload_draws_clear_and_faint_boxes_only():
    cache = {"version": 1, "posts": {
        "10.000000": _post(1, F2=_img("b", (1, 1, 5, 5, 599)),
                           F1=_img("a", (1, 2, 3, 4, 950), (5, 6, 7, 8, 600))),
    }}
    items = review.build_queue(cache, {}, 900, None, 0, 10**12, everything=True)
    [p] = review.gallery_payload(items, cache, 900, WORKSPACE, CHANNEL, lambda ts: "WHEN")
    assert p["when"] == "WHEN" and p["label"] is None
    assert p["link"] == f"{WORKSPACE}/archives/{CHANNEL}/p10000000"
    assert [img["id"] for img in p["images"]] == ["F1", "F2"]
    assert p["images"][0]["boxes"] == [[1, 2, 3, 4, True], [5, 6, 7, 8, False]]
    assert p["images"][1]["boxes"] == []
    assert (p["clear"], p["faint"]) == (1, 1)


# --------------------------------------------------------------------------- #
# Gallery server: 127.0.0.1, port 0, shut down by the test
# --------------------------------------------------------------------------- #

class _Gallery:
    """Runs review.serve in a thread with a fixed path secret and a recorded server so
    the test can shut it down; every request goes through a proxy-free opener."""

    SECRET = "fixed-test-secret"

    def __init__(self, monkeypatch, folder, payload, fetch, urls):
        created = []
        ready = threading.Event()

        class _Recording(http.server.ThreadingHTTPServer):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                created.append(self)
                ready.set()

        monkeypatch.setattr(http.server, "ThreadingHTTPServer", _Recording)
        monkeypatch.setattr(review.secrets, "token_urlsafe", lambda n=None: self.SECRET)
        self.result: dict = {}
        self.thread = threading.Thread(
            target=lambda: self.result.setdefault("labels", review.serve(
                folder, payload, fetch, urls, "Photo review <test>", open_browser=False)),
            daemon=True)
        self.thread.start()
        assert ready.wait(10), "gallery server did not start"
        self.server = created[0]
        host, port = self.server.server_address[:2]
        assert host == "127.0.0.1" and port != 0
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data,
                                     method="GET" if data is None else "POST")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def refused(self, path, body=None, raw=None):
        """Status of a POST the server answers without reading its body. Closing a socket
        with unread input may reset the connection on Windows, which counts as refused."""
        try:
            return self.request(path, body, raw)[0]
        except (ConnectionError, urllib.error.URLError):
            return "reset"

    def stop(self):
        self.server.shutdown()
        self.thread.join(10)
        assert not self.thread.is_alive()
        return self.result["labels"]


JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body"


def _gallery_payload() -> tuple[list, dict]:
    payload = [
        {"ts": "100.000001", "when": "Mon", "reasons": [review.NO_FACE], "targets": 1,
         "clear": 0, "faint": 0, "label": None, "link": "x", "images": []},
        {"ts": "100.000002", "when": "Tue", "reasons": [review.EXTRA], "targets": 1,
         "clear": 2, "faint": 0, "label": "yes", "link": "x", "images": []},
    ]
    urls = {("100.000001", "F01"): "https://files.example/F01",
            ("100.000002", "F02"): "https://files.example/F02"}
    return payload, urls


def test_gallery_serves_page_images_and_labels(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "review"
    payload, urls = _gallery_payload()
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url.endswith("F02"):
            raise OSError("network down")
        return JPEG

    g = _Gallery(monkeypatch, folder, payload, fetch, urls)
    try:
        secret = f"/{g.SECRET}"
        status, body, headers = g.request(secret + "/")
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "no-referrer"
        page = body.decode("utf-8")
        assert "Photo review &lt;test&gt;" in page and "100.000001" in page
        assert "files.example" not in page          # URLs stay server side

        assert g.request("/")[0] == 404
        assert g.request("/wrong-secret/")[0] == 404
        assert g.request(f"/{g.SECRET}x/")[0] == 404

        status, body, headers = g.request(secret + "/img/100.000001/F01")
        assert (status, body, headers["Content-Type"]) == (200, JPEG, "image/jpeg")
        assert g.request(secret + "/img/100.000001/F01")[0] == 200
        assert fetched == ["https://files.example/F01"]   # second hit from memory
        assert g.request(secret + "/img/100.000002/F02")[0] == 502
        assert g.request(secret + "/img/100.000001/F99")[0] == 404
        assert g.request(secret + "/img/999.000001/F01")[0] == 404

        assert g.request(secret + "/label", {"ts": "100.000001", "label": "no"})[0] == 200
        assert g.request(secret + "/label", {"ts": "100.000002", "label": "clear"})[0] == 200
        assert g.request(secret + "/label", {"ts": "999.000001", "label": "yes"})[0] == 400
        assert g.request(secret + "/label", {"ts": "100.000001", "label": "maybe"})[0] == 400
        assert g.request(secret + "/label", {"ts": "100.000001"})[0] == 400
        assert g.request(secret + "/label", raw=b"not json")[0] == 400
        assert g.refused(secret + "/label", raw=b"x" * 5000) in (400, "reset")
        assert g.refused("/wrong-secret/label", {"ts": "100.000001", "label": "yes"})             in (404, "reset")
    finally:
        labels_now = g.stop()
    assert labels_now == {"100.000001": "no", "100.000002": None}
    assert review.load_labels(folder) == {"100.000001": "no"}
    out = capsys.readouterr().out
    assert f"http://127.0.0.1:{g.server.server_address[1]}/{g.SECRET}/" in out
    # Only the labels file is written; no image bytes reach the folder.
    assert sorted(p.name for p in folder.iterdir()) == ["labels.jsonl"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _config_dict(*, score_threshold=None, min_targets=5) -> dict:
    cfg = {
        "enabled": True,
        "persistence": "files",
        "slack": {"channel": CHANNEL},
        "timezone": "UTC",
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}],
        "rules": {
            "cooldown": {"minutes": 15, "scope": "pair", "rejected_attempts_reset": False},
            "multi_tag": "per_target",
            "max_targets_per_message": None,
            "edit_grace_minutes": 10,
            "max_snipes_per_target_per_day": None,
            "allow_self": False,
            "allow_bots": False,
            "count_thread_replies": False,
            "count_image_links": False,
            "allow_video": False,
            "selfie_bonus": False,
        },
        "players": {"count_intra_group": True,
                    "groups": {"fam": [ALEX, BAILEY, CASEY]}, "extras": []},
        "consent": {
            "veto": {"emoji": "no_entry_sign", "by": ["admins"]},
            "optout_messages": [],
            "opted_out": [],
        },
        "admins": [ALEX],
        "feedback": {
            "reactions": {
                "counted": "white_check_mark",
                "cooldown": "hourglass_flowing_sand",
                "untagged": None,
                "not_counted": "x",
                "selfie": None,
            },
            "review": {"min_targets": min_targets, "emoji": "question"},
        },
        "reports": [],
    }
    if score_threshold is not None:
        cfg["faces"] = {"score_threshold": score_threshold}
    return cfg


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)


class _Env:
    def __init__(self, tmp_path: Path, **cfg) -> None:
        self.root = tmp_path
        self.cfg = tmp_path / "config.yaml"
        self.cfg.write_text(yaml.safe_dump(_config_dict(**cfg), sort_keys=False),
                            encoding="utf-8")
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.dir = tmp_path / "review"

    def argv(self, sub, *extra):
        return ["review", sub, "--config", str(self.cfg), "--data-dir", str(self.data),
                "--dir", str(self.dir), *extra]


TS_FINE = _ts(2026, 9, 18, 10)      # one clear face, one tag
TS_NOFACE = _ts(2026, 9, 17, 10)    # no boxes
TS_MID = _ts(2026, 9, 16, 10)       # one box at 0.85


def _cli_world(cls=FakeSlack) -> tuple[FakeSlack, BoxDetector]:
    slack = _slack(cls)
    slack.post(at=TS_FINE, user=ALEX, channel=CHANNEL, text=f"<@{BAILEY}>",
               files=[image_file("F01", b"fine")])
    slack.post(at=TS_NOFACE, user=ALEX, channel=CHANNEL, text=f"<@{BAILEY}>",
               files=[image_file("F02", b"noface")])
    slack.post(at=TS_MID, user=CASEY, channel=CHANNEL, text=f"<@{ALEX}>",
               files=[image_file("F03", b"mid")])
    slack.post(at=_ts(2026, 9, 15), user=ALEX, channel=CHANNEL, text="untagged",
               files=[image_file("F04", b"untagged")])
    det = BoxDetector({b"fine": ONE_CLEAR, b"noface": NO_BOXES, b"mid": ONE_MID})
    return slack, det


def _no_slack():
    raise AssertionError("this subcommand must not build a Slack client")


def _no_detector():
    raise AssertionError("this subcommand must not build a detector")


def test_cli_parser_review_subcommands():
    parser = cli._build_parser()
    ns = parser.parse_args(["review", "open", "--no-browser", "--all", "--semester", "x"])
    assert ns.review_command == "open" and ns.no_browser and ns.all and ns.dir == "review"
    ns = parser.parse_args(["review", "label", "--ts", "1.000001", "no", "--dir", "d"])
    assert (ns.ts, ns.label, ns.dir) == ("1.000001", "no", "d")
    with pytest.raises(SystemExit):
        parser.parse_args(["review", "label", "--ts", "1.000001", "maybe"])
    with pytest.raises(SystemExit):
        parser.parse_args(["review"])


def test_cli_label_writes_and_needs_no_config(tmp_path, capsys):
    folder = tmp_path / "review"
    argv = ["review", "label", "--config", str(tmp_path / "missing.yaml"),
            "--dir", str(folder), "--ts", "1758189600.000100", "yes"]
    rc = main(argv, slack_factory=_no_slack, detector_factory=_no_detector)
    assert rc == int(Exit.OK)
    assert capsys.readouterr().out.strip() == "1758189600.000100 yes"
    assert review.load_labels(folder) == {"1758189600.000100": "yes"}
    rc = main([*argv[:-1], "clear"], slack_factory=_no_slack, detector_factory=_no_detector)
    assert rc == int(Exit.OK)
    assert review.load_labels(folder) == {}


@pytest.mark.parametrize("ts", ["abc", "1.0", "1758189600.1234567", "1758189600"])
def test_cli_label_bad_ts_is_config_invalid(tmp_path, capsys, ts):
    folder = tmp_path / "review"
    rc = main(["review", "label", "--dir", str(folder), "--ts", ts, "yes"],
              slack_factory=_no_slack, detector_factory=_no_detector)
    assert rc == int(Exit.CONFIG_INVALID)
    assert "--ts" in capsys.readouterr().err
    assert not (folder / "labels.jsonl").exists()


def test_cli_list_scans_and_prints_the_queue(tmp_path, capsys):
    env = _Env(tmp_path)
    slack, det = _cli_world()
    rc = main(env.argv("list"), slack_factory=lambda: slack, detector_factory=lambda: det)
    assert rc == int(Exit.OK)
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert [line.split()[0] for line in lines] == [TS_NOFACE, TS_MID]
    assert all("open" in line and "no clear face" in line for line in lines)
    assert f"{WORKSPACE}/archives/{CHANNEL}/p{TS_NOFACE.replace('.', '')}" in lines[0]
    assert "scanned 3 tagged photo posts in fall-2026: 3 new photos read, 0 could not be read" \
        in captured.err
    assert "2 queued, 2 open" in captured.err
    cache = review.load_cache(env.dir)
    assert set(cache["posts"]) == {TS_FINE, TS_NOFACE, TS_MID}

    # A second run reads nothing new and shows labels.
    review.append_label(env.dir, TS_NOFACE, "no")
    rc = main(env.argv("list"), slack_factory=lambda: slack, detector_factory=lambda: det)
    assert rc == int(Exit.OK)
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert [line.split()[0] for line in lines] == [TS_MID, TS_NOFACE]
    assert " no " in lines[1]
    assert "0 new photos read" in captured.err and "2 queued, 1 open" in captured.err
    assert len(det.calls) == 3


def test_cli_list_all_includes_reasonless_posts(tmp_path, capsys):
    env = _Env(tmp_path)
    slack, det = _cli_world()
    rc = main(env.argv("list", "--all"), slack_factory=lambda: slack,
              detector_factory=lambda: det)
    assert rc == int(Exit.OK)
    lines = capsys.readouterr().out.strip().splitlines()
    assert [line.split()[0] for line in lines] == [TS_NOFACE, TS_MID, TS_FINE]
    assert "  -  " in lines[2]


def test_cli_live_threshold_comes_from_config(tmp_path, capsys):
    env = _Env(tmp_path, score_threshold="0.8")
    slack, det = _cli_world()
    rc = main(env.argv("list"), slack_factory=lambda: slack, detector_factory=lambda: det)
    assert rc == int(Exit.OK)
    lines = capsys.readouterr().out.strip().splitlines()
    assert [line.split()[0] for line in lines] == [TS_NOFACE]   # 0.85 is clear at 0.8
    rc = main(env.argv("stats"), slack_factory=_no_slack, detector_factory=_no_detector)
    assert rc == int(Exit.OK)
    out = capsys.readouterr().out
    [live] = [line for line in out.splitlines() if "<- live" in line]
    assert live.split()[0] == "0.800"


def test_stats_marks_a_live_threshold_off_the_sweep_grid():
    cache = {"version": review.CACHE_VERSION, "posts": {"10.000000": _post(1, F1=_img("a", _box(910)))}}
    lines = review.stats_lines(cache, {}, 920, None, 0, 10**12)
    [live] = [line for line in lines if "<- live" in line]
    assert live.split()[:2] == ["0.920", "1"]


def test_cli_many_tags_uses_config_min_targets(tmp_path, capsys):
    env = _Env(tmp_path, min_targets=1)
    slack, det = _cli_world()
    rc = main(env.argv("list"), slack_factory=lambda: slack, detector_factory=lambda: det)
    assert rc == int(Exit.OK)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3 and all("many people tagged" in line for line in lines)


def test_cli_stats_reads_only_the_folder(tmp_path, capsys):
    env = _Env(tmp_path)
    slack, det = _cli_world()
    assert main(env.argv("list"), slack_factory=lambda: slack,
                detector_factory=lambda: det) == int(Exit.OK)
    review.append_label(env.dir, TS_NOFACE, "no")
    review.append_label(env.dir, TS_FINE, "yes")
    capsys.readouterr()
    rc = main(env.argv("stats"), slack_factory=_no_slack, detector_factory=_no_detector)
    assert rc == int(Exit.OK)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "posts 3  labeled 2  yes 1  no 1"
    assert out[-1] == f"  {TS_NOFACE}"


def test_cli_stats_unknown_semester_is_config_invalid(tmp_path, capsys):
    env = _Env(tmp_path)
    rc = main(env.argv("stats", "--semester", "spring-1999"),
              slack_factory=_no_slack, detector_factory=_no_detector)
    assert rc == int(Exit.CONFIG_INVALID)


def test_cli_malformed_labels_is_config_invalid(tmp_path, capsys):
    env = _Env(tmp_path)
    env.dir.mkdir()
    (env.dir / "labels.jsonl").write_bytes(b"garbage\n")
    rc = main(env.argv("stats"), slack_factory=_no_slack, detector_factory=_no_detector)
    assert rc == int(Exit.CONFIG_INVALID)
    assert capsys.readouterr().err.startswith("review: ")


def test_cli_wrong_cache_version_is_config_invalid_before_slack(tmp_path, capsys):
    env = _Env(tmp_path)
    env.dir.mkdir()
    (env.dir / "faces.json").write_text('{"version": 99, "posts": {}}', encoding="utf-8")
    rc = main(env.argv("list"), slack_factory=_no_slack, detector_factory=_no_detector)
    assert rc == int(Exit.CONFIG_INVALID)
    assert f"version-{review.CACHE_VERSION}" in capsys.readouterr().err
    # The bad cache is left as found.
    assert json.loads((env.dir / "faces.json").read_text(encoding="utf-8"))["version"] == 99


def test_cli_slack_failure_is_slack_error_and_keeps_progress(tmp_path, capsys):
    env = _Env(tmp_path)
    slack, det = _cli_world()
    slack.bot_member_of = []          # history now raises NotInChannel
    rc = main(env.argv("list"), slack_factory=lambda: slack, detector_factory=lambda: det)
    assert rc == int(Exit.SLACK_ERROR)
    assert "NotInChannel" in capsys.readouterr().err
    assert review.load_cache(env.dir) == {"version": review.CACHE_VERSION, "posts": {}}


def test_cli_open_builds_gallery_without_a_browser(tmp_path, monkeypatch, capsys):
    env = _Env(tmp_path)
    slack, det = _cli_world()
    seen = {}

    def fake_serve(folder, payload, fetch, urls, title, open_browser=True, port=0):
        seen.update(folder=folder, payload=payload, fetch=fetch, urls=urls, title=title,
                    open_browser=open_browser)
        return {}

    monkeypatch.setattr(review, "serve", fake_serve)
    rc = main(env.argv("open", "--no-browser"), slack_factory=lambda: slack,
              detector_factory=lambda: det)
    assert rc == int(Exit.OK)
    assert seen["open_browser"] is False
    assert seen["folder"] == env.dir
    assert seen["title"] == "Photo review, fall-2026"
    assert [p["ts"] for p in seen["payload"]] == [TS_NOFACE, TS_MID]
    assert seen["payload"][0]["when"] == "Thu Sep 17 10:00"
    assert set(seen["urls"]) == {(TS_FINE, "F01"), (TS_NOFACE, "F02"), (TS_MID, "F03")}
    assert seen["fetch"] == slack.fetch_file_bytes


def test_cli_review_never_writes_ledger_state_or_slack(tmp_path, monkeypatch, capsys):
    env = _Env(tmp_path)
    slack, det = _cli_world(GuardSlack)
    monkeypatch.setattr(review, "serve", lambda *a, **k: {})
    factories = {"slack_factory": lambda: slack, "detector_factory": lambda: det}
    assert main(env.argv("list", "--all"), **factories) == int(Exit.OK)
    assert main(env.argv("open", "--no-browser"), **factories) == int(Exit.OK)
    assert main(env.argv("stats"), **factories) == int(Exit.OK)
    assert main(["review", "label", "--dir", str(env.dir), "--ts", TS_NOFACE, "no"],
                **factories) == int(Exit.OK)
    assert slack.writes == []
    assert [e for e in slack._events if e["actor"] == BOT] == []
    assert list(env.data.iterdir()) == []
    assert not (env.data / "ledger.jsonl").exists() and not (env.data / "state.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.yaml", "data", "review"]
    assert sorted(p.name for p in env.dir.iterdir()) == ["faces.json", "labels.jsonl"]
    # Labels never feed back into the scan: the cache is unchanged by a label.
    before = (env.dir / "faces.json").read_bytes()
    assert main(["review", "label", "--dir", str(env.dir), "--ts", TS_MID, "yes"],
                **factories) == int(Exit.OK)
    assert (env.dir / "faces.json").read_bytes() == before


def test_cli_review_folder_holds_no_names_ids_urls(tmp_path, capsys):
    env = _Env(tmp_path)
    slack, det = _cli_world()
    assert main(env.argv("list"), slack_factory=lambda: slack,
                detector_factory=lambda: det) == int(Exit.OK)
    assert main(["review", "label", "--dir", str(env.dir), "--ts", TS_MID, "no"],
                slack_factory=_no_slack, detector_factory=_no_detector) == int(Exit.OK)
    for path in env.dir.iterdir():
        text = path.read_text(encoding="utf-8")
        for needle in ("U0", "Alex", "Bailey", "Casey", "http", "files.example",
                       "fine", "noface"):
            assert needle not in text, (path.name, needle)


# --------------------------------------------------------------------------- #
# detect_boxes vs count_faces on the real detector (slow, needs cv2 + the model)
# --------------------------------------------------------------------------- #

try:
    import cv2  # noqa: F401

    HAS_CV2 = True
except Exception:  # pragma: no cover - environment without opencv
    HAS_CV2 = False

MODEL_PATH = Path(__file__).resolve().parents[1] / "snipebot" / "models" / \
    "face_detection_yunet_2023mar.onnx"
requires_yunet = pytest.mark.skipif(not (HAS_CV2 and MODEL_PATH.exists()),
                                    reason="cv2 or the YuNet model is unavailable")


@pytest.mark.slow
@requires_yunet
@pytest.mark.parametrize("name", list(EXPECTED_COUNTS))
def test_detect_boxes_agrees_with_count_faces(name):
    from snipebot.faces import YuNetDetector

    data = faces_fixtures.photo_path(name).read_bytes()
    live = YuNetDetector(str(MODEL_PATH), "0.9")
    boxes, width, height = live.detect_boxes(data)
    assert len(boxes) == live.count_faces(data) == EXPECTED_COUNTS[name]
    assert 0 < width <= 1024 and 0 < height <= 1024
    for box in boxes:
        assert len(box) == 5 and all(type(v) is int for v in box)
        assert box[2] > 0 and box[3] > 0 and 900 <= box[4] <= 1000
    # The review scan runs at a 0.5 floor and applies the live threshold afterwards;
    # that must give the live count.
    floor = YuNetDetector(str(MODEL_PATH), "0.5")
    low_boxes, w2, h2 = floor.detect_boxes(data)
    assert (w2, h2) == (width, height)
    assert sum(1 for b in low_boxes if b[4] >= 900) == EXPECTED_COUNTS[name]
    assert json.loads(json.dumps([list(b) for b in boxes])) == [list(b) for b in boxes]


# --------------------------------------------------------------------------- #
# A token that cannot read files; a photo with no rendition
# --------------------------------------------------------------------------- #

class NoFilesSlack(FakeSlack):
    """files:read missing: every rendition fetch is a scope error."""

    def fetch_file_bytes(self, url: str) -> bytes:
        from snipebot.slack_io import MissingScope

        raise MissingScope("missing_scope")


def test_scan_stops_when_the_token_cannot_read_files():
    """Not "N could not be read" and an empty gallery: the scan raises, the CLI exits 5."""
    from snipebot.slack_io import MissingScope

    slack = _slack(NoFilesSlack)
    slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
               text=f"<@{BAILEY}>", files=[image_file("F01", b"img-one")])
    with pytest.raises(MissingScope):
        _scan(slack, BoxDetector({}), _empty_cache())


def test_cli_scope_error_on_fetch_is_slack_error(tmp_path, capsys):
    env = _Env(tmp_path)
    slack, det = _cli_world(NoFilesSlack)
    rc = main(env.argv("list"), slack_factory=lambda: slack, detector_factory=lambda: det)
    assert rc == int(Exit.SLACK_ERROR)
    assert "MissingScope" in capsys.readouterr().err


def test_photo_without_a_rendition_is_queued_unreadable():
    slack = _slack()
    f = image_file("F01", b"img-one")
    for key in ("thumb_1024", "url_private_download"):
        del f[key]
    ts = slack.post(at=_ts(2026, 9, 10, 9), user=ALEX, channel=CHANNEL,
                    text=f"<@{BAILEY}>", files=[f])
    cache = _empty_cache()
    result = _scan(slack, BoxDetector({}), cache)
    assert (result.detected, result.failed) == (0, 1)
    [item] = review.build_queue(cache, {}, 900, None, 0, NOW_US)
    assert (item.ts, item.reasons, item.clear_faces) == (ts, (review.UNREAD,), 0)
