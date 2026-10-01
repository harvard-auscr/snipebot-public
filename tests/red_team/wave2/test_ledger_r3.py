"""Red-team wave 2, round 3 (invariants across runs) against snipebot.ledger.

Scope: the durable-file layer's cross-run invariants — canonical byte-identity,
fail-closed loading, atomic saves, count_moved_pairs, check_integrity and State
serialization (00-data.md sections 3, 4, 7; 20-sync-ledger.md section 7).

Result of the hunt: the heavily-specified surfaces are hardened. Every load
fail-closed case in the 00-data section 3 list (extra/missing key, wrong type per
key, non-hex rendition_hash, negative detect_attempts, ts out of grammar, integer
floats, duplicate ts, BOM), every canonical-byte rule (20-key order, sorted maps,
LF only, no trailing spaces, ensure_ascii, integers-not-floats), every
count_moved_pairs case (status/reason/blocked_by/selfie flip, vanished, brand-new,
reorder, order-independence, empty baseline), and every check_integrity trip
(non-COOLDOWN blocked_by, COOLDOWN blocked_by later/missing, non-anchor blocked_by,
duplicate/non-increasing ts) were probed empirically and behave per spec. A 400-row
dump->load->dump fuzz found byte-identity with zero mismatches, and prior wave-2
ledger findings (float `version`, blocked_by non-anchor) are verified FIXED.

The two remaining deviations below are genuine spec INCONSISTENCIES (a data-model
requirement in one place, a fail-closed load list that omits it in another), so
they are reported as spec_issues in the structured output, not as findings. The
tests are reproducible evidence: each asserts the fail-closed behaviour the data
model implies and FAILS on the current (spec-compliant-to-the-load-list) code.

Inputs use the same small factory the sanctioned ledger tests use (copied here so
the file is self-contained).
"""

from __future__ import annotations

import json

import pytest

from snipebot.ledger import MalformedLedgerError, dumps_row, load_ledger
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


def _load_obj(obj: dict) -> list[Candidate]:
    import tempfile
    from pathlib import Path

    p = Path(tempfile.mkdtemp()) / "ledger.jsonl"
    p.write_bytes((json.dumps(obj) + "\n").encode("utf-8"))
    return load_ledger(p)


def test_load_rejects_non_hex_file_sig(tmp_path):
    """SPEC-ISSUE evidence. 00-data.md section 3 key 10 declares `file_sigs` as
    "64-hex sigs, sorted ascending, duplicates kept", and the fail-closed Load list
    (lines 350-358) rejects "a `rendition_hash` value that is not 64 lowercase hex
    chars". The two 64-hex fields are declared identically, and the load's stated
    purpose is that "A partial ledger would flip cooldown chains and reactions", yet
    the list validates only `rendition_hash`'s hex and is silent on `file_sigs`.
    _parse_row runs `file_sigs` through `_req_str_list` (string-shape only), so a
    non-hex sig loads. This test asserts the symmetric fail-closed behaviour the data
    model implies and FAILS on the current code, which accepts it.
    """
    obj = json.loads(dumps_row(faceless()))
    obj["file_sigs"] = ["not_sixty_four_hex"]
    with pytest.raises(MalformedLedgerError):
        _load_obj(obj)


def test_load_rejects_reaction_override_valued_false(tmp_path):
    """SPEC-ISSUE evidence. 00-data.md section 2 (SelfieOverride, line 129): "A
    reaction never writes `False` and never replaces a `CLI` override." A stored
    `selfie_override` with `source == "reaction"` and `value == false` is therefore a
    state the writer can never produce; on disk it can only be corruption, and it
    flips the sibfam selfie classification (section 4) that drives reactions and
    points — exactly what the section 3 fail-closed load exists to stop ("A partial
    ledger would flip cooldown chains and reactions"). But the section 3 Load list
    defines a valid override purely by shape ("an object `{value: bool, by: string,
    source: "reaction"|"cli"}`"), and `_parse_selfie_override` enforces only that
    shape, so the impossible reaction/False override loads. This test asserts the
    fail-closed behaviour the section 2 invariant implies and FAILS on the current
    code, which accepts it.
    """
    obj = json.loads(dumps_row(faceless()))
    obj["selfie_override"] = {"value": False, "by": "U9ADMIN", "source": "reaction"}
    with pytest.raises(MalformedLedgerError):
        _load_obj(obj)
