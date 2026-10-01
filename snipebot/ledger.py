"""Durable-file layer: `ledger.jsonl`, `verdicts.jsonl`, `state.json`.

Owns the canonical serialization (00-data §3, §4, §7), fail-closed loading, atomic
saves, the movement diff (`count_moved_pairs`) and the config-free integrity checks
(20-sync-ledger §7). Slack ts values are carried as strings and ordered with
`snipebot.ts.parse_ts`; a float never appears in any output.

Shared names homed here (00-data §10): `dumps_row`, `dumps_ledger`,
`dumps_verdict_row`, `dumps_verdicts`, `count_moved_pairs`, `State`,
`load_ledger`, `load_state`, `save_ledger`, `save_verdicts`, `save_state`,
`check_integrity`, `LedgerIntegrityError`. `MalformedLedgerError` is contracted in
00-data §3 and defined here as the module's fail-closed load error.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from snipebot.parse import Candidate, SelfieOverride, TargetEdit, Veto, VetoSource
from snipebot.rules import MessageVerdict, Status
from snipebot.ts import TsFormatError, parse_ts

__all__ = [
    "MalformedLedgerError",
    "LedgerIntegrityError",
    "State",
    "dumps_row",
    "dumps_ledger",
    "dumps_verdict_row",
    "dumps_verdicts",
    "count_moved_pairs",
    "load_ledger",
    "load_state",
    "save_ledger",
    "save_verdicts",
    "save_state",
    "check_integrity",
]


class MalformedLedgerError(ValueError):
    """A durable file (ledger, verdicts, or state) failed validation. Loading fails
    closed: the caller writes nothing (00-data §3 Load; 20 §7.1)."""


class LedgerIntegrityError(ValueError):
    """An integrity invariant (20 §7.3) is violated. Raised on the first violation."""


# --- shared constants -------------------------------------------------------

# The 20 ledger keys in canonical order (00-data §3). The order is fixed by the
# writer, not by the dataclass field order (live_image_ids is field 6 on the
# dataclass but key 16 here).
_LEDGER_KEYS = (
    "ts",
    "sender",
    "subtype",
    "thread_ts",
    "targets",
    "live_images",
    "live_videos",
    "linked_images",
    "last_edit_ts",
    "file_sigs",
    "vetoes",
    "missing_runs",
    "first_seen_targets",
    "first_sight_edited",
    "target_edited_in",
    "live_image_ids",
    "face_counts",
    "rendition_hash",
    "detect_attempts",
    "selfie_override",
)

_VALID_SOURCES = {s.value for s in VetoSource}  # {"reaction", "cli"}
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z", re.ASCII)

# json.dumps kwargs shared by every canonical row (00-data §3).
_DUMP = dict(ensure_ascii=True, separators=(",", ":"), sort_keys=False, allow_nan=False)


# --- State (00-data §7) -----------------------------------------------------


@dataclass
class State:
    """Runtime mirror of state.json (00-data §7). Not frozen: sync sets watermark,
    folds new IDs into opted_out and rewrites fingerprints and fingerprints_at at
    persist. The defaults are the first-run state (a missing file)."""

    version: int = 1
    watermark: str | None = None
    fingerprints: dict[str, str] = field(default_factory=dict)
    fingerprints_at: str | None = None     # SlackTs H the fingerprints cover (E-W4-17)
    opted_out: dict[str, int] = field(default_factory=dict)


# --- serialization: ledger.jsonl (00-data §3) -------------------------------


def dumps_row(candidate: Candidate) -> str:
    """One canonical ledger.jsonl line for `candidate` (no trailing newline).

    Enforces the canonical array/dict orders regardless of the in-memory order, so
    a frozenset or an unsorted map still serializes byte-stably (00-data §3)."""
    c = candidate
    ov = c.selfie_override
    obj = {
        "ts": c.ts,
        "sender": c.sender,
        "subtype": c.subtype,
        "thread_ts": c.thread_ts,
        "targets": list(c.targets),
        "live_images": c.live_images,
        "live_videos": c.live_videos,
        "linked_images": c.linked_images,
        "last_edit_ts": c.last_edit_ts,
        "file_sigs": sorted(c.file_sigs),
        "vetoes": [
            {"by": v.by, "source": v.source.value}
            for v in sorted(c.vetoes, key=lambda v: (v.by, v.source.value))
        ],
        "missing_runs": c.missing_runs,
        "first_seen_targets": sorted(c.first_seen_targets),
        "first_sight_edited": c.first_sight_edited,
        "target_edited_in": [
            {"user": t.user, "edit_ts": t.edit_ts}
            for t in sorted(c.target_edited_in, key=lambda t: t.user)
        ],
        "live_image_ids": sorted(c.live_image_ids),
        "face_counts": {k: c.face_counts[k] for k in sorted(c.face_counts)},
        "rendition_hash": {k: c.rendition_hash[k] for k in sorted(c.rendition_hash)},
        "detect_attempts": c.detect_attempts,
        "selfie_override": (
            None
            if ov is None
            else {"value": ov.value, "by": ov.by, "source": ov.source.value}
        ),
    }
    return json.dumps(obj, **_DUMP)


def dumps_ledger(rows: Sequence[Candidate]) -> str:
    """The whole ledger file: rows pre-sorted by parse_ts(ts) ascending, each row via
    dumps_row followed by one LF. A zero-length ledger is the empty string."""
    ordered = sorted(rows, key=lambda r: parse_ts(r.ts))
    return "".join(dumps_row(r) + "\n" for r in ordered)


# --- serialization: verdicts.jsonl (00-data §4) -----------------------------


def dumps_verdict_row(mv: MessageVerdict) -> str:
    """One canonical verdicts.jsonl line for `mv` (no trailing newline)."""
    obj = {
        "ts": mv.ts,
        "status": mv.status.value,
        "reason": mv.reason.value,
        "selfie": mv.selfie.value,
        "pairs": [
            {
                "target": p.target,
                "status": p.status.value,
                "reason": p.reason.value,
                "blocked_by": p.blocked_by,
                "selfie": p.selfie,
            }
            for p in mv.pairs
        ],
    }
    return json.dumps(obj, **_DUMP)


def dumps_verdicts(verdicts: Sequence[MessageVerdict]) -> str:
    """The whole verdicts file: `verdicts` pre-sorted by parse_ts(ts) ascending, each
    row via dumps_verdict_row followed by one LF; "" for an empty set."""
    ordered = sorted(verdicts, key=lambda v: parse_ts(v.ts))
    return "".join(dumps_verdict_row(v) + "\n" for v in ordered)


def count_moved_pairs(baseline: str, current: str) -> int:
    """The large-movement measure (plan §8; 00-data §4). Parse both verdicts.jsonl
    texts into {(ts, target): (status, reason, blocked_by, selfie)} and count the
    pairs that MOVED: present in `baseline` but changed or absent in `current`. A
    pair present only in `current` (brand-new) never counts. An empty `baseline`
    ("") yields 0."""
    base = _index_pairs(baseline)
    cur = _index_pairs(current)
    moved = 0
    for key, value in base.items():
        if key not in cur or cur[key] != value:
            moved += 1
    return moved


def _index_pairs(text: str) -> dict[tuple[str, str], tuple]:
    """{(ts, target): (status, reason, blocked_by, selfie)} for a serialized
    verdicts.jsonl text. Message-gated / untagged rows (pairs == []) contribute
    nothing."""
    out: dict[tuple[str, str], tuple] = {}
    if not text:
        return out
    for line in text.split("\n"):
        if line == "":
            continue
        obj = json.loads(line)
        ts = obj["ts"]
        for p in obj["pairs"]:
            out[(ts, p["target"])] = (
                p["status"],
                p["reason"],
                p["blocked_by"],
                p["selfie"],
            )
    return out


# --- loading: fail closed ---------------------------------------------------


def load_ledger(path: Path) -> list[Candidate]:
    """Read data/ledger.jsonl. A missing file is an empty ledger ([]). Every line is
    validated per 00-data §3; ANY malformed line aborts with MalformedLedgerError
    naming the line number, and the caller writes nothing. Returns rows sorted
    ascending by parse_ts(ts)."""
    text = _read_text(path)
    if text is None or text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the file's single trailing newline
    rows: list[Candidate] = []
    seen_ts: set[str] = set()
    for n, line in enumerate(lines, start=1):
        if line == "":
            raise MalformedLedgerError(f"line {n}: blank line")
        rows.append(_parse_row(line, n, seen_ts))
    rows.sort(key=lambda r: parse_ts(r.ts))
    return rows


def _reject_dup_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """json object_pairs_hook: a repeated name at any nesting level is malformed
    (json.loads would otherwise silently keep only the last value)."""
    d: dict[str, object] = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _parse_row(line: str, n: int, seen_ts: set[str]) -> Candidate:
    def bad(msg: str) -> MalformedLedgerError:
        return MalformedLedgerError(f"line {n}: {msg}")

    try:
        obj = json.loads(line, object_pairs_hook=_reject_dup_keys)
    except (ValueError, TypeError, RecursionError):
        raise bad("invalid JSON")
    if not isinstance(obj, dict):
        raise bad("not a JSON object")
    keys = set(obj)
    if keys != set(_LEDGER_KEYS):
        missing = set(_LEDGER_KEYS) - keys
        extra = keys - set(_LEDGER_KEYS)
        raise bad(f"key mismatch (missing={sorted(missing)}, extra={sorted(extra)})")

    ts = _req_ts(obj["ts"], "ts", bad, allow_null=False)
    if ts in seen_ts:
        raise bad(f"duplicate ts {ts!r}")
    seen_ts.add(ts)

    sender = _req_str(obj["sender"], "sender", bad)
    subtype = _opt_str(obj["subtype"], "subtype", bad)
    thread_ts = _req_ts(obj["thread_ts"], "thread_ts", bad, allow_null=True)
    targets = _req_str_list(obj["targets"], "targets", bad)
    if len(set(targets)) != len(targets):
        raise bad("targets repeat a user ID")          # deduped by definition (E-W4-10)
    live_images = _req_nonneg_int(obj["live_images"], "live_images", bad)
    live_videos = _req_nonneg_int(obj["live_videos"], "live_videos", bad)
    linked_images = _req_nonneg_int(obj["linked_images"], "linked_images", bad)
    last_edit_ts = _req_ts(obj["last_edit_ts"], "last_edit_ts", bad, allow_null=True)
    file_sigs = _req_str_list(obj["file_sigs"], "file_sigs", bad)
    for sig in file_sigs:
        if len(sig) != 64 or any(ch not in "0123456789abcdef" for ch in sig):
            raise bad("file_sigs entries must be 64 lowercase hex characters")
    vetoes = _parse_vetoes(obj["vetoes"], bad)
    missing_runs = _req_nonneg_int(obj["missing_runs"], "missing_runs", bad)
    first_seen_targets = _req_str_list(obj["first_seen_targets"], "first_seen_targets", bad)
    first_sight_edited = _req_bool(obj["first_sight_edited"], "first_sight_edited", bad)
    target_edited_in = _parse_target_edits(obj["target_edited_in"], bad)
    live_image_ids = _req_str_list(obj["live_image_ids"], "live_image_ids", bad)
    face_counts = _parse_face_counts(obj["face_counts"], bad)
    rendition_hash = _parse_rendition_hash(obj["rendition_hash"], bad)
    detect_attempts = _req_nonneg_int(obj["detect_attempts"], "detect_attempts", bad)
    selfie_override = _parse_selfie_override(obj["selfie_override"], bad)

    return Candidate(
        ts=ts,
        sender=sender,
        subtype=subtype,
        thread_ts=thread_ts,
        targets=tuple(targets),
        live_images=live_images,
        live_image_ids=tuple(sorted(live_image_ids)),
        live_videos=live_videos,
        linked_images=linked_images,
        last_edit_ts=last_edit_ts,
        file_sigs=tuple(sorted(file_sigs)),
        vetoes=tuple(vetoes),
        missing_runs=missing_runs,
        first_seen_targets=frozenset(first_seen_targets),
        first_sight_edited=first_sight_edited,
        target_edited_in=tuple(sorted(target_edited_in, key=lambda t: t.user)),
        face_counts=dict(face_counts),
        rendition_hash=dict(rendition_hash),
        detect_attempts=detect_attempts,
        selfie_override=selfie_override,
    )


def load_state(path: Path) -> State:
    """Read data/state.json (00-data §7). A missing file is the first-run state
    (watermark=None, empty fingerprints, fingerprints_at=None, empty opted_out). A
    file written before `fingerprints_at` existed (no such key) loads it as None. A
    malformed or wrong-version file aborts (fail closed) with MalformedLedgerError."""
    text = _read_text(path)
    if text is None:
        return State()

    def bad(msg: str) -> MalformedLedgerError:
        return MalformedLedgerError(f"state.json: {msg}")

    try:
        obj = json.loads(text, object_pairs_hook=_reject_dup_keys)
    except (ValueError, TypeError, RecursionError):
        raise bad("invalid JSON")
    if not isinstance(obj, dict):
        raise bad("not a JSON object")
    if set(obj) - {"fingerprints_at"} != {"version", "watermark", "fingerprints", "opted_out"}:
        raise bad(f"key mismatch (keys={sorted(obj)})")
    if not _is_int(obj["version"]) or obj["version"] != 1:
        raise bad(f"unsupported version {obj['version']!r}")

    watermark = _req_ts(obj["watermark"], "watermark", bad, allow_null=True)

    fps = obj["fingerprints"]
    if not isinstance(fps, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in fps.items()
    ):
        raise bad("fingerprints must be an object of str -> str")
    fingerprints_at = _req_ts(obj.get("fingerprints_at"), "fingerprints_at", bad, allow_null=True)

    opts = obj["opted_out"]
    if not isinstance(opts, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in opts.items()
    ):
        raise bad("opted_out must be an object of str -> int")

    return State(
        version=1,
        watermark=watermark,
        fingerprints=dict(fps),
        fingerprints_at=fingerprints_at,
        opted_out=dict(opts),
    )


# --- saving: atomic temp file + os.replace ----------------------------------


def save_ledger(path: Path, rows: Sequence[Candidate]) -> None:
    """Write dumps_ledger(rows) atomically (temp file in the SAME directory, fsync,
    os.replace). A crash leaves either the old or the new file, never a partial one."""
    _atomic_write_text(path, dumps_ledger(rows))


def save_verdicts(path: Path, verdicts: Sequence[MessageVerdict]) -> None:
    """Write dumps_verdicts(verdicts) the same atomic way. Output only; an empty
    verdict set is a zero-byte file."""
    _atomic_write_text(path, dumps_verdicts(verdicts))


def dumps_state(state: State) -> str:
    """state.json in the canonical form of 00-data §7 (sort_keys=True, indent=2,
    trailing LF, UTF-8 no BOM). Split out from `save_state` so a caller can compare
    the would-be bytes against what is on disk without writing (the no-op guard)."""
    obj = {
        "version": state.version,
        "watermark": state.watermark,
        "fingerprints": dict(state.fingerprints),
        "opted_out": dict(state.opted_out),
    }
    # Written once recorded; an unrecorded H stays absent (load reads that as null), so
    # a state.json from before the field keeps its bytes until sync records one (E-W4-17).
    if state.fingerprints_at is not None:
        obj["fingerprints_at"] = state.fingerprints_at
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n"


def save_state(path: Path, state: State) -> None:
    """Write state.json in the canonical form of 00-data §7 (sort_keys=True,
    indent=2, trailing LF, UTF-8 no BOM), atomically."""
    _atomic_write_text(path, dumps_state(state))


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    data = text.encode("utf-8")
    directory = path.parent
    # A fresh data branch tracks no empty dirs, so data/ may not exist yet.
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leave the destination untouched and drop the partial temp file.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- integrity checks (20 §7.3, checks 2-3) ---------------------------------


def check_integrity(rows: Sequence[Candidate], verdicts: Sequence[MessageVerdict]) -> None:
    """Raise LedgerIntegrityError on the first violation of the config-free
    invariants (20 §7.3):

      check 1  every row round-trips through the loader's own line validator
               (rows built in memory were never loaded);
      check 2  rows are strictly increasing and unique by parse_ts(ts);
      check 3  every COOLDOWN pair has a blocked_by that is an EARLIER ledger row
               whose verdict carries a COUNTED or COOLDOWN pair for the same
               target, and no other pair carries a blocked_by.

    check 4 (no float) is enforced by the load validator; check 5 (verdicts
    staleness) and the not-admin override WARN need the resolved config and are
    applied by sync/doctor, not here (20 §7.3)."""
    for n, r in enumerate(rows, 1):
        try:
            # Per-row seen set: cross-row uniqueness is check 2's to report.
            _parse_row(dumps_row(r), n, set())
        except MalformedLedgerError as exc:
            raise LedgerIntegrityError(f"row {n} not loadable (check 1)") from exc
    prev: int | None = None
    seen: set[str] = set()
    for r in rows:
        t = parse_ts(r.ts)
        if r.ts in seen:
            raise LedgerIntegrityError(f"duplicate ts {r.ts!r}")
        if prev is not None and t <= prev:
            raise LedgerIntegrityError(f"ts not strictly increasing at {r.ts!r}")
        seen.add(r.ts)
        prev = t

    row_ts = {r.ts for r in rows}
    by_ts = {mv.ts: mv for mv in verdicts}
    for mv in verdicts:
        mv_us = parse_ts(mv.ts)
        for p in mv.pairs:
            if p.status is Status.COOLDOWN:
                if p.blocked_by is None:
                    raise LedgerIntegrityError(
                        f"cooldown pair ({mv.ts}, {p.target}) has no blocked_by"
                    )
                if parse_ts(p.blocked_by) >= mv_us:
                    raise LedgerIntegrityError(
                        f"blocked_by {p.blocked_by!r} not earlier than {mv.ts!r}"
                    )
                if p.blocked_by not in row_ts:
                    raise LedgerIntegrityError(
                        f"blocked_by {p.blocked_by!r} is not a ledger row"
                    )
                # The row at blocked_by must be the cooldown anchor for this
                # scope: it must carry a COUNTED pair (a valid anchor under any
                # config) or, accepting conservatively since reset-mode cannot be
                # excluded config-free, a COOLDOWN pair. A NOT_COUNTED/LATE_TAG-only
                # or absent-verdict anchor fails closed (20 §7.3 check 3).
                anchor = by_ts.get(p.blocked_by)
                anchors = anchor is not None and any(
                    ap.target == p.target
                    and ap.status in (Status.COUNTED, Status.COOLDOWN)
                    for ap in anchor.pairs
                )
                if not anchors:
                    raise LedgerIntegrityError(
                        f"blocked_by {p.blocked_by!r} is not the cooldown anchor "
                        f"for ({mv.ts}, {p.target})"
                    )
            elif p.blocked_by is not None:
                raise LedgerIntegrityError(
                    f"non-cooldown pair ({mv.ts}, {p.target}) carries blocked_by"
                )


# --- low-level readers / validators -----------------------------------------


def _read_text(path: Path) -> str | None:
    """The file's UTF-8 text, or None if it does not exist. A decode error is
    malformed."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise MalformedLedgerError(f"{p.name}: not valid UTF-8")
    except OSError as exc:
        # A path that exists but cannot be read as a file (a directory, no permission)
        # fails closed like any unreadable ledger.
        raise MalformedLedgerError(f"{p.name}: cannot be read ({type(exc).__name__})") from exc


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _req_str(v, name, bad):
    if not isinstance(v, str):
        raise bad(f"{name} must be a string")
    return v


def _opt_str(v, name, bad):
    if v is None:
        return None
    if not isinstance(v, str):
        raise bad(f"{name} must be a string or null")
    return v


def _req_bool(v, name, bad):
    if not isinstance(v, bool):
        raise bad(f"{name} must be a boolean")
    return v


def _req_nonneg_int(v, name, bad):
    if not _is_int(v):
        raise bad(f"{name} must be an integer (a float is malformed)")
    if v < 0:
        raise bad(f"{name} must be >= 0")
    return v


def _req_ts(v, name, bad, *, allow_null):
    if v is None:
        if allow_null:
            return None
        raise bad(f"{name} must not be null")
    if not isinstance(v, str):
        raise bad(f"{name} must be a Slack ts string")
    try:
        parse_ts(v)
    except TsFormatError:
        raise bad(f"{name} is not a valid Slack ts: {v!r}")
    return v


def _req_str_list(v, name, bad):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise bad(f"{name} must be an array of strings")
    return v


def _parse_vetoes(v, bad):
    if not isinstance(v, list):
        raise bad("vetoes must be an array")
    out: list[Veto] = []
    for item in v:
        if not isinstance(item, dict) or set(item) != {"by", "source"}:
            raise bad("each veto must be an object {by, source}")
        by = _req_str(item["by"], "veto.by", bad)
        source = item["source"]
        if source not in _VALID_SOURCES:
            raise bad(f"veto.source must be one of {sorted(_VALID_SOURCES)}")
        out.append(Veto(by=by, source=VetoSource(source)))
    return out


def _parse_target_edits(v, bad):
    if not isinstance(v, list):
        raise bad("target_edited_in must be an array")
    out: list[TargetEdit] = []
    for item in v:
        if not isinstance(item, dict) or set(item) != {"user", "edit_ts"}:
            raise bad("each target_edited_in entry must be an object {user, edit_ts}")
        user = _req_str(item["user"], "target_edited_in.user", bad)
        edit_ts = _req_ts(item["edit_ts"], "target_edited_in.edit_ts", bad, allow_null=True)
        out.append(TargetEdit(user=user, edit_ts=edit_ts))
    return out


def _parse_face_counts(v, bad):
    if not isinstance(v, dict):
        raise bad("face_counts must be an object")
    out: dict[str, int] = {}
    for k, val in v.items():
        if not isinstance(k, str):
            raise bad("face_counts keys must be strings")
        if not _is_int(val) or val < 0:
            raise bad("face_counts values must be non-negative integers")
        out[k] = val
    return out


def _parse_rendition_hash(v, bad):
    if not isinstance(v, dict):
        raise bad("rendition_hash must be an object")
    out: dict[str, str] = {}
    for k, val in v.items():
        if not isinstance(k, str):
            raise bad("rendition_hash keys must be strings")
        if not isinstance(val, str) or _HEX64.match(val) is None:
            raise bad("rendition_hash values must be 64 lowercase hex chars")
        out[k] = val
    return out


def _parse_selfie_override(v, bad):
    if v is None:
        return None
    if not isinstance(v, dict) or set(v) != {"value", "by", "source"}:
        raise bad("selfie_override must be null or {value, by, source}")
    if not isinstance(v["value"], bool):
        raise bad("selfie_override.value must be a boolean")
    by = _req_str(v["by"], "selfie_override.by", bad)
    source = v["source"]
    if source not in _VALID_SOURCES:
        raise bad(f"selfie_override.source must be one of {sorted(_VALID_SOURCES)}")
    if source == VetoSource.REACTION.value and v["value"] is False:
        # A reaction never writes False (00 §2); on disk that can only be corruption.
        raise bad("selfie_override from a reaction cannot carry value false")
    return SelfieOverride(value=v["value"], by=by, source=VetoSource(source))
