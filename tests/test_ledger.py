"""Tests for snipebot.ledger: canonical serialization, fail-closed loading, atomic
saves, the movement diff and the config-free integrity checks (00-data §3/§4/§7;
20-sync-ledger §7). Covers L1-FX-face-facts-serialize, L8-PF-DOC-LEDGER-INTEGRITY and
L8-PF-moved-pairs."""

from __future__ import annotations

import json
import os

import pytest

from snipebot.ledger import (
    LedgerIntegrityError,
    MalformedLedgerError,
    State,
    check_integrity,
    count_moved_pairs,
    dumps_ledger,
    dumps_row,
    dumps_verdict_row,
    dumps_verdicts,
    load_ledger,
    load_state,
    save_ledger,
    save_state,
    save_verdicts,
)
from snipebot.parse import Candidate, SelfieOverride, TargetEdit, Veto, VetoSource
from snipebot.rules import MessageVerdict, PairVerdict, Reason, SelfieClass, Status

SIG = "0" * 64
HASH_A = "a" * 64
HASH_B = "b" * 64

_LEDGER_KEY_ORDER = [
    "ts", "sender", "subtype", "thread_ts", "targets", "live_images", "live_videos",
    "linked_images", "last_edit_ts", "file_sigs", "vetoes", "missing_runs",
    "first_seen_targets", "first_sight_edited", "target_edited_in", "live_image_ids",
    "face_counts", "rendition_hash", "detect_attempts", "selfie_override",
]


# --- factories --------------------------------------------------------------


def faceless(ts="1000.000001", **over) -> Candidate:
    """A row with every faces field at its empty/null default."""
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
        file_sigs=(SIG,),
        vetoes=(),
        missing_runs=0,
        first_seen_targets=frozenset({"U3", "U2"}),
        first_sight_edited=False,
        target_edited_in=(),
        face_counts={},
        rendition_hash={},
        detect_attempts=0,
        selfie_override=None,
    )
    kw.update(over)
    return Candidate(**kw)


def full_faces(ts="2000.000002") -> Candidate:
    """A row with every faces field populated and every sortable field given out of
    order, to prove canonical serialization re-sorts."""
    return Candidate(
        ts=ts,
        sender="U9",
        subtype="file_share",
        thread_ts="2000.000002",
        targets=("U5", "U4"),  # mention order preserved, NOT sorted
        live_images=2,
        live_image_ids=("F02", "F01"),  # sorted on the wire
        live_videos=1,
        linked_images=3,
        last_edit_ts="2001.000000",
        file_sigs=("f" * 64, SIG),  # sorted on the wire
        vetoes=(Veto("U8", VetoSource.REACTION), Veto("U7", VetoSource.CLI)),
        missing_runs=1,
        first_seen_targets=frozenset({"U4"}),
        first_sight_edited=True,
        target_edited_in=(
            TargetEdit("U6", None),
            TargetEdit("U5", "2000.500000"),
        ),
        face_counts={"F02": 1, "F01": 2},
        rendition_hash={"F02": HASH_B, "F01": HASH_A},
        detect_attempts=4,
        selfie_override=SelfieOverride(True, "U0ADMIN", VetoSource.CLI),
    )


def mv(ts, status=Status.COUNTED, selfie=SelfieClass.NOT_APPLICABLE, pairs=(), reason=Reason.COUNTED):
    return MessageVerdict(ts=ts, status=status, reason=reason, selfie=selfie, pairs=pairs)


def pv(ts, target, status=Status.COUNTED, reason=Reason.COUNTED, blocked_by=None, selfie=False):
    return PairVerdict(ts=ts, target=target, status=status, reason=reason, blocked_by=blocked_by, selfie=selfie)


# --- ledger serialization ---------------------------------------------------


def test_faceless_row_exact_bytes():
    row = faceless()
    expected = (
        '{"ts":"1000.000001","sender":"U1","subtype":null,"thread_ts":null,'
        '"targets":["U2","U3"],"live_images":1,"live_videos":0,"linked_images":0,'
        '"last_edit_ts":null,"file_sigs":["' + SIG + '"],"vetoes":[],'
        '"missing_runs":0,"first_seen_targets":["U2","U3"],"first_sight_edited":false,'
        '"target_edited_in":[],"live_image_ids":["F1"],"face_counts":{},'
        '"rendition_hash":{},"detect_attempts":0,"selfie_override":null}'
    )
    assert dumps_row(row) == expected


def test_key_order_and_defaults_present():
    # L1-FX-face-facts-serialize: every key present on a faceless row, in order.
    ordered = json.loads(dumps_row(faceless()), object_pairs_hook=lambda p: [k for k, _ in p])
    assert ordered == _LEDGER_KEY_ORDER


def test_faces_block_sorted_and_nested_order():
    obj = json.loads(dumps_row(full_faces()))
    assert list(obj["live_image_ids"]) == ["F01", "F02"]
    assert list(obj["face_counts"].keys()) == ["F01", "F02"]
    assert list(obj["rendition_hash"].keys()) == ["F01", "F02"]
    assert obj["file_sigs"] == sorted(obj["file_sigs"])
    # nested object key order
    veto_keys = json.loads(
        dumps_row(full_faces()), object_pairs_hook=lambda p: [k for k, _ in p]
    )
    # vetoes sorted by (by, source): U7/cli before U8/reaction
    v = obj["vetoes"]
    assert v == [{"by": "U7", "source": "cli"}, {"by": "U8", "source": "reaction"}]
    assert obj["target_edited_in"] == [
        {"user": "U5", "edit_ts": "2000.500000"},
        {"user": "U6", "edit_ts": None},
    ]
    assert obj["selfie_override"] == {"value": True, "by": "U0ADMIN", "source": "cli"}


def test_nested_object_key_order_literal():
    line = dumps_row(full_faces())
    assert '"selfie_override":{"value":true,"by":"U0ADMIN","source":"cli"}' in line
    assert '{"by":"U7","source":"cli"}' in line
    assert '{"user":"U5","edit_ts":"2000.500000"}' in line


def test_frozenset_order_does_not_leak():
    a = faceless(first_seen_targets=frozenset({"U2", "U3"}))
    b = faceless(first_seen_targets=frozenset({"U3", "U2"}))
    assert dumps_row(a) == dumps_row(b)


def test_dump_is_deterministic_across_runs():
    row = full_faces()
    assert dumps_row(row) == dumps_row(row)


def test_empty_ledger_is_empty_string():
    assert dumps_ledger([]) == ""


def test_ledger_sorted_and_newline_terminated():
    text = dumps_ledger([full_faces("3000.000000"), faceless("1000.000001")])
    lines = text.split("\n")
    assert lines[-1] == ""  # trailing newline
    assert json.loads(lines[0])["ts"] == "1000.000001"
    assert json.loads(lines[1])["ts"] == "3000.000000"


# --- round trips ------------------------------------------------------------


def test_round_trip_byte_stable(tmp_path):
    rows = [faceless(), full_faces()]
    p = tmp_path / "ledger.jsonl"
    save_ledger(p, rows)
    first = p.read_bytes()
    loaded = load_ledger(p)
    save_ledger(p, loaded)
    second = p.read_bytes()
    assert first == second
    assert dumps_ledger(loaded) == dumps_ledger(rows)


def test_round_trip_preserves_all_faces_fields():
    row = full_faces()
    [back] = load_ledger_from_text(dumps_row(row) + "\n")
    assert back.live_image_ids == ("F01", "F02")
    assert back.face_counts == {"F01": 2, "F02": 1}
    assert back.rendition_hash == {"F01": HASH_A, "F02": HASH_B}
    assert back.detect_attempts == 4
    assert back.selfie_override == SelfieOverride(True, "U0ADMIN", VetoSource.CLI)
    assert back.vetoes == tuple(sorted(row.vetoes, key=lambda v: (v.by, v.source.value)))


def load_ledger_from_text(text):
    import tempfile
    from pathlib import Path

    d = tempfile.mkdtemp()
    p = Path(d) / "ledger.jsonl"
    p.write_bytes(text.encode("utf-8"))
    return load_ledger(p)


def test_missing_file_is_empty(tmp_path):
    assert load_ledger(tmp_path / "nope.jsonl") == []


def test_zero_byte_file_is_empty(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_bytes(b"")
    assert load_ledger(p) == []


def test_load_sorts_ascending():
    text = dumps_row(faceless("5000.000000")) + "\n" + dumps_row(faceless("1000.000000")) + "\n"
    rows = load_ledger_from_text(text)
    assert [r.ts for r in rows] == ["1000.000000", "5000.000000"]


# --- malformed shapes -------------------------------------------------------


def _one_line_obj():
    return json.loads(dumps_row(faceless()))


def _load_obj(obj):
    return load_ledger_from_text(json.dumps(obj) + "\n")


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda o: o.__setitem__("ts", "not-a-ts"), "ts"),
        (lambda o: o.__setitem__("ts", 1000), "ts"),
        (lambda o: o.__setitem__("live_images", 1.0), "live_images"),
        (lambda o: o.__setitem__("live_images", -1), "live_images"),
        (lambda o: o.__setitem__("missing_runs", -1), "missing_runs"),
        (lambda o: o.__setitem__("detect_attempts", -1), "detect_attempts"),
        (lambda o: o.__setitem__("first_sight_edited", "no"), "first_sight_edited"),
        (lambda o: o.__setitem__("targets", "U2"), "targets"),
        (lambda o: o.__setitem__("thread_ts", "bad"), "thread_ts"),
        (lambda o: o.__setitem__("face_counts", {"F1": -1}), "face_counts"),
        (lambda o: o.__setitem__("face_counts", {"F1": 1.5}), "face_counts"),
        (lambda o: o.__setitem__("rendition_hash", {"F1": "xyz"}), "rendition_hash"),
        (lambda o: o.__setitem__("rendition_hash", {"F1": "A" * 64}), "rendition_hash"),
        (lambda o: o.__setitem__("selfie_override", {"value": "yes", "by": "U", "source": "cli"}), "selfie_override"),
        (lambda o: o.__setitem__("selfie_override", {"value": True, "by": "U", "source": "bogus"}), "source"),
        (lambda o: o.__setitem__("vetoes", [{"by": "U", "source": "bogus"}]), "source"),
        (lambda o: o.__setitem__("vetoes", [{"by": "U"}]), "veto"),
        (lambda o: o.__setitem__("extra_key", 1), "key mismatch"),
        (lambda o: o.pop("selfie_override"), "key mismatch"),
        (lambda o: o.__setitem__("live_images", True), "live_images"),
    ],
)
def test_malformed_shapes_rejected(mutate, needle):
    obj = _one_line_obj()
    mutate(obj)
    with pytest.raises(MalformedLedgerError) as ei:
        _load_obj(obj)
    assert needle in str(ei.value)


def test_bad_json_rejected():
    with pytest.raises(MalformedLedgerError):
        load_ledger_from_text('{"ts":\n')


def test_truncated_line_rejected():
    good = dumps_row(faceless())
    with pytest.raises(MalformedLedgerError):
        load_ledger_from_text(good[: len(good) // 2] + "\n")


def test_duplicate_ts_rejected():
    text = dumps_row(faceless("1000.000001")) + "\n" + dumps_row(faceless("1000.000001")) + "\n"
    with pytest.raises(MalformedLedgerError) as ei:
        load_ledger_from_text(text)
    assert "duplicate" in str(ei.value)


def test_error_names_line_number():
    good = dumps_row(faceless("1000.000001"))
    bad = json.dumps({**_one_line_obj(), "ts": "bad"})
    with pytest.raises(MalformedLedgerError) as ei:
        load_ledger_from_text(good + "\n" + bad + "\n")
    assert "line 2" in str(ei.value)


def test_blank_line_rejected():
    text = dumps_row(faceless()) + "\n\n"
    with pytest.raises(MalformedLedgerError):
        load_ledger_from_text(text)


# --- atomic save ------------------------------------------------------------


def test_atomic_save_no_partial_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "ledger.jsonl"
    save_ledger(p, [faceless("1000.000001")])
    original = p.read_bytes()

    import snipebot.ledger as ledger_mod

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(ledger_mod.os, "replace", boom)
    with pytest.raises(OSError):
        save_ledger(p, [faceless("2000.000002")])

    # original preserved, no leftover temp file
    assert p.read_bytes() == original
    leftovers = [q for q in tmp_path.iterdir() if q.name != "ledger.jsonl"]
    assert leftovers == []


def test_save_verdicts_empty_is_zero_bytes(tmp_path):
    p = tmp_path / "verdicts.jsonl"
    save_verdicts(p, [])
    assert p.read_bytes() == b""


def test_save_ledger_empty_is_zero_bytes(tmp_path):
    p = tmp_path / "ledger.jsonl"
    save_ledger(p, [])
    assert p.read_bytes() == b""


# --- verdicts serialization -------------------------------------------------


def test_verdict_row_shape_and_order():
    m = mv(
        "1758210000.000199",
        status=Status.COUNTED,
        selfie=SelfieClass.SELFIE,
        pairs=(
            pv("1758210000.000199", "U01BBB", selfie=True),
            pv("1758210000.000199", "U02CCC", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by="1758209100.000050"),
        ),
    )
    line = dumps_verdict_row(m)
    expected = (
        '{"ts":"1758210000.000199","status":"counted","reason":"counted","selfie":"selfie","pairs":['
        '{"target":"U01BBB","status":"counted","reason":"counted","blocked_by":null,"selfie":true},'
        '{"target":"U02CCC","status":"cooldown","reason":"cooldown","blocked_by":"1758209100.000050","selfie":false}]}'
    )
    assert line == expected


def test_gated_message_has_empty_pairs():
    line = dumps_verdict_row(mv("1000.000001", status=Status.UNTAGGED, pairs=()))
    assert '"pairs":[]' in line


def test_message_reason_serialized_after_status():
    line = dumps_verdict_row(
        mv("1000.000001", status=Status.NOT_COUNTED, reason=Reason.DELETED, pairs=())
    )
    assert (
        line
        == '{"ts":"1000.000001","status":"not_counted","reason":"deleted","selfie":"not_applicable","pairs":[]}'
    )
    obj = json.loads(line)
    assert obj["reason"] == "deleted"
    assert list(obj)[:3] == ["ts", "status", "reason"]


def test_dumps_verdicts_sorted_and_empty():
    assert dumps_verdicts([]) == ""
    text = dumps_verdicts([mv("3000.000000"), mv("1000.000000")])
    lines = [l for l in text.split("\n") if l]
    assert [json.loads(l)["ts"] for l in lines] == ["1000.000000", "3000.000000"]
    assert text.endswith("\n")


# --- count_moved_pairs ------------------------------------------------------


def _verdicts_text(*pairs_specs):
    """pairs_specs: iterable of (ts, [PairVerdict, ...])."""
    verdicts = [mv(ts, pairs=tuple(ps)) for ts, ps in pairs_specs]
    return dumps_verdicts(verdicts)


def test_moved_pairs_empty_baseline_is_zero():
    cur = _verdicts_text(("1000.000001", [pv("1000.000001", "U2")]))
    assert count_moved_pairs("", cur) == 0


def test_moved_pairs_status_change():
    base = _verdicts_text(("1000.000001", [pv("1000.000001", "U2", status=Status.COUNTED)]))
    cur = _verdicts_text(("1000.000001", [pv("1000.000001", "U2", status=Status.NOT_COUNTED, reason=Reason.DAILY_CAP)]))
    assert count_moved_pairs(base, cur) == 1


def test_moved_pairs_reason_change():
    base = _verdicts_text(("1000.000001", [pv("1000.000001", "U2", status=Status.NOT_COUNTED, reason=Reason.DAILY_CAP)]))
    cur = _verdicts_text(("1000.000001", [pv("1000.000001", "U2", status=Status.NOT_COUNTED, reason=Reason.LATE_TAG)]))
    assert count_moved_pairs(base, cur) == 1


def test_moved_pairs_blocked_by_change():
    base = _verdicts_text(("2000.000001", [pv("2000.000001", "U2", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by="1000.000000")]))
    cur = _verdicts_text(("2000.000001", [pv("2000.000001", "U2", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by="1500.000000")]))
    assert count_moved_pairs(base, cur) == 1


def test_moved_pairs_selfie_flip_only():
    # status/reason unchanged, only the selfie point flips -> must be visible.
    base = _verdicts_text(("1000.000001", [pv("1000.000001", "U2", selfie=False)]))
    cur = _verdicts_text(("1000.000001", [pv("1000.000001", "U2", selfie=True)]))
    assert count_moved_pairs(base, cur) == 1


def test_moved_pairs_vanished_counts_new_does_not():
    base = _verdicts_text(("1000.000001", [pv("1000.000001", "U2")]))
    cur = _verdicts_text(
        ("1000.000001", []),  # U2 vanished
        ("2000.000002", [pv("2000.000002", "U9")]),  # brand-new
    )
    assert count_moved_pairs(base, cur) == 1


def test_moved_pairs_unchanged_is_zero():
    text = _verdicts_text(("1000.000001", [pv("1000.000001", "U2")]))
    assert count_moved_pairs(text, text) == 0


# --- integrity checks -------------------------------------------------------


def test_integrity_clean_passes():
    rows = [faceless("1000.000001"), full_faces("2000.000002")]
    verdicts = [
        mv("1000.000001", pairs=(pv("1000.000001", "U2"),)),
        mv("2000.000002", pairs=(pv("2000.000002", "U2", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by="1000.000001"),)),
    ]
    check_integrity(rows, verdicts)  # no raise


def test_integrity_cooldown_anchor_passes():
    """A COOLDOWN pair may anchor to an earlier row that itself carries only a
    COOLDOWN pair (reset-mode cannot be excluded config-free), so the chain
    COUNTED <- COOLDOWN <- COOLDOWN passes (20 §7.3 check 3)."""
    rows = [faceless("1000.000001"), faceless("2000.000002"), faceless("3000.000003")]
    verdicts = [
        mv("1000.000001", pairs=(pv("1000.000001", "U2"),)),
        mv("2000.000002", pairs=(pv("2000.000002", "U2", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by="1000.000001"),)),
        mv("3000.000003", pairs=(pv("3000.000003", "U2", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by="2000.000002"),)),
    ]
    check_integrity(rows, verdicts)  # no raise


def test_integrity_cooldown_anchor_not_counted_trips():
    """A COOLDOWN pair whose blocked_by points at a row carrying only a NOT_COUNTED
    pair is not a valid cooldown anchor and fails closed (20 §7.3 check 3)."""
    rows = [faceless("1000.000001"), faceless("2000.000002")]
    verdicts = [
        mv("1000.000001", status=Status.NOT_COUNTED, pairs=(pv("1000.000001", "U2", status=Status.NOT_COUNTED, reason=Reason.LATE_TAG),)),
        mv("2000.000002", pairs=(pv("2000.000002", "U2", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by="1000.000001"),)),
    ]
    with pytest.raises(LedgerIntegrityError):
        check_integrity(rows, verdicts)


def test_integrity_duplicate_ts_trips():
    rows = [faceless("1000.000001"), faceless("1000.000001")]
    with pytest.raises(LedgerIntegrityError) as ei:
        check_integrity(rows, [])
    assert "duplicate" in str(ei.value)


def test_integrity_non_increasing_trips():
    rows = [faceless("2000.000002"), faceless("1000.000001")]
    with pytest.raises(LedgerIntegrityError):
        check_integrity(rows, [])


def test_integrity_cooldown_without_blocked_by_trips():
    rows = [faceless("1000.000001")]
    verdicts = [mv("1000.000001", pairs=(pv("1000.000001", "U2", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by=None),))]
    with pytest.raises(LedgerIntegrityError):
        check_integrity(rows, verdicts)


def test_integrity_blocked_by_not_earlier_trips():
    rows = [faceless("1000.000001"), faceless("2000.000002")]
    verdicts = [mv("1000.000001", pairs=(pv("1000.000001", "U2", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by="2000.000002"),))]
    with pytest.raises(LedgerIntegrityError):
        check_integrity(rows, verdicts)


def test_integrity_blocked_by_missing_row_trips():
    rows = [faceless("2000.000002")]
    verdicts = [mv("2000.000002", pairs=(pv("2000.000002", "U2", status=Status.COOLDOWN, reason=Reason.COOLDOWN, blocked_by="1000.000000"),))]
    with pytest.raises(LedgerIntegrityError):
        check_integrity(rows, verdicts)


def test_integrity_non_cooldown_with_blocked_by_trips():
    rows = [faceless("2000.000002"), faceless("1000.000000")]
    verdicts = [mv("2000.000002", pairs=(pv("2000.000002", "U2", status=Status.COUNTED, reason=Reason.COUNTED, blocked_by="1000.000000"),))]
    with pytest.raises(LedgerIntegrityError):
        check_integrity(rows, verdicts)


# --- state.json -------------------------------------------------------------


def test_state_round_trip(tmp_path):
    p = tmp_path / "state.json"
    st = State(
        version=1,
        watermark="1758210000.000199",
        fingerprints={"rules": "a" * 64, "players": "b" * 64, "semesters": "c" * 64, "groups": "d" * 64},
        opted_out={"U03EEE": 1758210042000000},
    )
    save_state(p, st)
    back = load_state(p)
    assert back == st


def test_state_canonical_bytes(tmp_path):
    p = tmp_path / "state.json"
    save_state(p, State(watermark=None, fingerprints={}, opted_out={}))
    text = p.read_bytes().decode("utf-8")
    assert text.endswith("\n")
    obj = json.loads(text)
    assert obj == {"version": 1, "watermark": None, "fingerprints": {}, "opted_out": {}}
    # sort_keys=True, indent=2
    assert '\n  "fingerprints"' in text


def test_state_missing_file_is_first_run(tmp_path):
    st = load_state(tmp_path / "nope.json")
    assert st == State()
    assert st.watermark is None and st.fingerprints == {} and st.opted_out == {}


def test_state_wrong_version_fails(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 2, "watermark": None, "fingerprints": {}, "opted_out": {}}), encoding="utf-8")
    with pytest.raises(MalformedLedgerError):
        load_state(p)


def test_state_malformed_fails(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "watermark": "bad-ts", "fingerprints": {}, "opted_out": {}}), encoding="utf-8")
    with pytest.raises(MalformedLedgerError):
        load_state(p)


def test_state_opted_out_float_fails(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "watermark": None, "fingerprints": {}, "opted_out": {"U1": 1.5}}), encoding="utf-8")
    with pytest.raises(MalformedLedgerError):
        load_state(p)
