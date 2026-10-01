"""Red-team wave 2, round 1 (spec conformance) against snipebot.ledger.

Every test in this file is a BREAK: it asserts the behaviour the specification
requires and FAILS on the current code, proving a deviation. Inputs are built
with the same small factories the sanctioned ledger tests use (copied here so the
file is self-contained). Spec sources: 00-data.md sections 3, 4, 7; 20-sync-ledger.md
section 7.
"""

from __future__ import annotations

import json

import pytest

from snipebot.ledger import MalformedLedgerError, dumps_row, load_ledger, load_state
from snipebot.parse import Candidate


def faceless(ts: str = "1000.000001", **over) -> Candidate:
    kw = dict(
        ts=ts,
        sender="U1",
        subtype=None,
        thread_ts=None,
        targets=("U2", "U3"),
        live_images=1,
        live_image_ids=("F1",),
        live_videos=0,
        linked_images=0,
        last_edit_ts=None,
        file_sigs=("0" * 64,),
        vetoes=(),
        missing_runs=0,
        first_seen_targets=frozenset({"U2", "U3"}),
        first_sight_edited=False,
        target_edited_in=(),
        face_counts={},
        rendition_hash={},
        detect_attempts=0,
        selfie_override=None,
    )
    kw.update(over)
    return Candidate(**kw)


def test_load_state_rejects_float_version(tmp_path):
    """00-data.md section 7 (state.json canonical serialization, lines 1109-1114):
    "No floats (`watermark` is a string; `opted_out` values and `version` are ints)."
    The load contract (00-data section 7 / 20-sync-ledger section 7.1) says a malformed
    file "aborts (fail closed)". A `version` of 1.0 is a float, not the integer the schema
    requires, so load_state must raise MalformedLedgerError just as it does for a float
    `opted_out` value. load_state instead accepts it (`1.0 != 1` is False), returning a
    State. Fail-closed is breached.
    """
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps({"version": 1.0, "watermark": None, "fingerprints": {}, "opted_out": {}}),
        encoding="utf-8",
    )
    with pytest.raises(MalformedLedgerError):
        load_state(p)


def test_load_ledger_directory_fails_closed(tmp_path):
    """00-data.md section 3 Load (lines 348-358): a durable file that cannot be read as a
    valid ledger "aborts the run with `MalformedLedgerError` and nothing is written";
    20-sync-ledger section 7.1 repeats that load "aborts with MalformedLedgerError". sync
    (step 1) catches only MalformedLedgerError and maps it to exit 4. When the ledger path
    is a directory, load_ledger leaks a raw OSError (PermissionError / IsADirectoryError)
    from `Path.read_bytes()` instead of failing closed with MalformedLedgerError, so the
    documented fail-closed path is bypassed.
    """
    d = tmp_path / "ledger_is_a_dir.jsonl"
    d.mkdir()
    with pytest.raises(MalformedLedgerError):
        load_ledger(d)
