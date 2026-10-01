"""Round 1 red-team (spec conformance): break the schedule-replay harness
(``tests/oracle/replay.py`` + ``tests/oracle/test_schedule_replay.py``).

Each test encodes an equality the shipped harness's own contract asserts
(``_masked(got) == _masked(reference)`` over the two generators it compares,
``six_hourly`` and ``single_final``) and FAILS on the current code, proving the
harness's placeholder timeline / mask / hardcoded detector is unrepresentative:
its single placeholder timeline (two clean snipes, no delete, no tombstone, no
selfie) is exactly the one shape under which the divergences the spec documents
never fire, so the harness gives false assurance.

Inputs are built with the FakeSlack authoring API via ``tests._helpers_sync``.
Every test is expected to FAIL against the current tree.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("snipebot.sync", reason="sync.run_sync not implemented yet")
pytest.importorskip("tests.fake_slack", reason="FakeSlack authoring API not implemented yet")

from tests.oracle import replay  # noqa: E402
from tests._helpers_sync import image_file, make_config, mkts, roster_of  # noqa: E402

CHANNEL = "C0MAIN01"
A, B, C, D = "U0A", "U0B", "U0C", "U0D"

# The exact mask the shipped harness applies (tests/oracle/test_schedule_replay.py
# _MASKED_KEYS): keys 13-15 and 17-19. Key 12 `missing_runs` and key 16
# `live_image_ids` are deliberately NOT masked -- the spec asserts them byte-identical.
_MASKED_KEYS = frozenset({
    "first_seen_targets", "first_sight_edited", "target_edited_in",
    "face_counts", "rendition_hash", "detect_attempts",
})


def _cfg(**kw):
    roster = roster_of({A: "fam", B: "fam", C: "fam", D: "fam"})
    return make_config(roster=roster, reports=(), **kw)


def _masked(ledger_bytes: bytes) -> list[dict]:
    rows: list[dict] = []
    for line in ledger_bytes.decode("utf-8").splitlines():
        if not line:
            continue
        obj = json.loads(line)
        rows.append({k: v for k, v in obj.items() if k not in _MASKED_KEYS})
    return rows


def _post(h: int, m: int, user: str, tgt: str, fid: str, data: bytes) -> "replay.AuthoringEvent":
    return replay.AuthoringEvent(
        at=mkts(2026, 9, 18, h, m), kind="post",
        payload={"user": user, "channel": CHANNEL, "text": f"<@{tgt}>",
                 "files": [image_file(fid, data)]},
    )


def test_file_tombstone_drops_whole_row_under_single_final():
    """50 section 4.4 lines 548-561 documents a mid-span file tombstone as an in-bound
    carve-out confined to keys 17-19: the tombstoned-first-sight schedule "records
    neither and classifies from the empty facts" -- i.e. the spec assumes the row still
    exists. It does not. An image-only snipe whose file is tombstoned before a schedule's
    first sight has no live image at first sight, so parse emits NO candidate row under
    that schedule -- a whole-row divergence no mask over keys 13-15/17-19 can cover.
    ``single_final`` (one of the two generators the shipped harness compares) omits the
    row ``every_minute`` keeps, so the harness's own ``_masked`` equality fails.
    """
    tl = replay.Timeline(
        config=_cfg(), channels=(CHANNEL,), horizon_days=90,
        events=(
            _post(12, 0, A, B, "F01", b"snap-a"),
            _post(12, 5, C, D, "F02", b"snap-b"),
            replay.AuthoringEvent(
                at=mkts(2026, 9, 18, 14, 0), kind="delete_file",
                payload={"ts": mkts(2026, 9, 18, 12, 0), "channel": CHANNEL},
            ),
        ),
    )
    reference = replay.replay(tl, replay.every_minute(tl))
    got = replay.replay(tl, replay.single_final(tl))
    assert _masked(got) == _masked(reference)


def test_replay_cannot_exercise_the_faces_carveout():
    """50 section 4.3 lines 511-516: keys 17-19 (``face_counts``/``rendition_hash``/
    ``detect_attempts``) are "the second carve-out: face facts are captured only while a
    file is live". The schedule harness masks them, claiming to honour that carve-out.
    But ``replay.replay`` hardcodes ``detector=FakeFaceDetector({})`` (replay.py line 180),
    and ``faces.count_faces`` raises ``KeyError`` for any sha not pre-seeded. So the moment
    a timeline actually fetches a rendition -- a sib-tagged snipe with ``selfie_bonus`` in
    force (20 section 5; sync ``_detect_faces`` gate) -- the replay crashes instead of
    producing face facts. The mask over 17-19 is thus vacuous: the carve-out the harness
    documents cannot be exercised through it at all.
    """
    tl = replay.Timeline(
        config=_cfg(selfie_bonus=True), channels=(CHANNEL,), horizon_days=90,
        events=(
            _post(12, 0, A, B, "F01", b"selfie-a"),
        ),
    )
    # Currently raises KeyError before returning; the harness cannot replay a face timeline.
    got = replay.replay(tl, replay.single_final(tl))
    row = json.loads(got.decode("utf-8").splitlines()[0])
    assert row["face_counts"], "expected a captured face count for the live rendition"
