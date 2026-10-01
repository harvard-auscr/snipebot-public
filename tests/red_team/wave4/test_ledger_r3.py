"""Red-team wave 4, surface "ledger", round 3."""

from __future__ import annotations

from pathlib import Path

import pytest

from snipebot.ledger import MalformedLedgerError, load_ledger, load_state

_ROW = (
    '{"ts":"1790000000.000100","sender":"U0AAA001","subtype":null,"thread_ts":null,'
    '"targets":["U0AAA002"],"live_images":1,"live_videos":0,"linked_images":0,'
    '"last_edit_ts":null,"file_sigs":[],"vetoes":[],"missing_runs":0,'
    '"first_seen_targets":["U0AAA002"],"first_sight_edited":false,"target_edited_in":[],'
    '"live_image_ids":["F0FILE001"],"face_counts":{},"rendition_hash":{},'
    '"detect_attempts":0,"selfie_override":null}'
)

_DEPTH = 50_000


def test_deeply_nested_line_escapes_as_recursion_error_not_malformed(tmp_path: Path):
    """A corrupted ledger line (or state.json) whose value nests past Python's recursion
    limit makes json.loads raise RecursionError. _parse_row and load_state catch only
    ValueError/TypeError, so the RecursionError escapes load_ledger/load_state instead of
    MalformedLedgerError. sync maps only MalformedLedgerError to exit 4, the CLI's
    _load_rows likewise, and doctor's DOC-LEDGER-INTEGRITY check catches only
    MalformedLedgerError, so the run dies as exit 1 'unexpected error' (doctor crashes
    instead of reporting a FAIL line).

    Violates 00-data section 3 Load / 20 section 7.1: ANY malformed line (bad JSON, wrong
    type) aborts with MalformedLedgerError; and 20 section 7.1 load_state: a malformed
    file aborts fail closed (exit 4)."""
    nested = "[" * _DEPTH + "]" * _DEPTH
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes((_ROW.replace('"face_counts":{}', '"face_counts":' + nested) + "\n")
                       .encode("ascii"))
    with pytest.raises(MalformedLedgerError):
        load_ledger(ledger)

    state = tmp_path / "state.json"
    state.write_bytes(
        ('{"version":1,"watermark":null,"fingerprints":{},"opted_out":' + nested + "}\n")
        .encode("ascii")
    )
    with pytest.raises(MalformedLedgerError):
        load_state(state)
