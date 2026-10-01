"""Wave-2 round-3 red team: invariants across runs, aimed at the harnesses other
tests lean on (tests/oracle/replay.py + test_schedule_replay.py, tests/test_crash_matrix.py).

Each test asserts a property the specification requires of the harness and FAILS on the
current code, proving the harness under-verifies a documented cross-run invariant.
Build inputs with the FakeSlack authoring API via tests._helpers_sync. Fast (< 5 s each).
"""
from __future__ import annotations

import inspect
import json
import tempfile
from pathlib import Path

from snipebot.ledger import load_ledger
from snipebot.rules import evaluate

from tests.oracle import replay
from tests._helpers_sync import image_file, make_config, mkts, roster_of

CHANNEL = "C0MAIN01"
A, B, C, D = "U0A", "U0B", "U0C", "U0D"

# The exact carve-out mask test_schedule_replay.py applies before its equality check
# (50 §4.3, keys 13-15 and 17-19).
_MASKED_KEYS = frozenset({
    "first_seen_targets", "first_sight_edited", "target_edited_in",
    "face_counts", "rendition_hash", "detect_attempts",
})


def _masked(ledger_bytes: bytes) -> list[dict]:
    rows: list[dict] = []
    for line in ledger_bytes.decode("utf-8").splitlines():
        if not line:
            continue
        obj = json.loads(line)
        rows.append({k: v for k, v in obj.items() if k not in _MASKED_KEYS})
    return rows


def _statuses(ledger_bytes: bytes, config) -> list[tuple[str, str]]:
    """Recompute the (ts, message-status) verdict list a schedule's final ledger implies.
    evaluate() is a pure function of the ledger + config (00-data §5), so this is exactly
    what verdicts.jsonl would hold for that schedule."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ledger.jsonl"
        p.write_bytes(ledger_bytes)
        rows = load_ledger(p)
    verdicts = evaluate(rows, config.rules, config.roster, set(),
                        config.semesters, config.tz)
    return [(v.ts, v.status.value) for v in verdicts]


def test_crash_matrix_cannot_reach_second_ordinal_per_item_boundary():
    """50 §5.1: 'for the per-item boundaries over each key and the 1st/2nd ordinal, so a
    crash between two reactions, between two per-image detections, and between two digests
    is covered.' The ordinal gate is SNIPEBOT_CRASH_NTH (20 §2.2).

    The authored crash world gives each message exactly one image, so a per-message faces:*
    boundary fires only once and 'a crash between two per-image detections' is unreachable;
    and test_crash_matrix.py never sets SNIPEBOT_CRASH_NTH anywhere, so no per-item boundary
    is ever crashed on its 2nd occurrence. The 1st/2nd ordinal coverage the matrix is
    required to provide does not exist."""
    import tests.test_crash_matrix as cm

    world = cm._author_world()
    posts = [e for e in world["events"] if e["kind"] == "post"]
    max_images = max(len(e["data"].get("files") or []) for e in posts)
    src = inspect.getsource(cm)

    assert max_images >= 2 or "SNIPEBOT_CRASH_NTH" in src, (
        "50 §5.1 requires a crash between two per-image detections and on the 2nd ordinal, "
        f"but every authored message carries {max_images} image and the matrix never arms "
        "SNIPEBOT_CRASH_NTH, so no per-item boundary is ever crashed on a 2nd occurrence"
    )


def test_schedule_replay_never_drives_jitter_or_outage_generators():
    """50 §4.3: the ledger is 'asserted byte-identical across all five schedules'. 50 §4.2:
    jitter_dropped is 'the only place cron jitter/dropped ticks exist -- plan §9 L2', and
    three_day_outage is the only generator with a >3-day gap at the §2 bound edge.

    test_schedule_replay.py parametrises its equality test over six_hourly and single_final
    only (plus every_minute as the reference). jitter_dropped and three_day_outage are never
    driven, so the cron jitter / dropped-tick invariance (plan §9 L2) and the 3-day-gap
    recovery-watermark convergence are unverified by the harness."""
    import tests.oracle.test_schedule_replay as srt

    src = inspect.getsource(srt)
    assert "jitter_dropped" in src and "three_day_outage" in src, (
        "50 §4.3 asserts equality across all five schedules, but the harness never drives "
        "jitter_dropped (the sole cron-jitter/dropped-tick source, plan §9 L2) or "
        "three_day_outage (the >3-day gap case); both are absent from the test module"
    )
