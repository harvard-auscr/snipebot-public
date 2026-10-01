"""Wave 4 rulings: regression tests for the config.py and ledger.py repairs.

Offline: every config and data file is written under tmp_path.
"""

from __future__ import annotations

import calendar
import json
from pathlib import Path

import pytest

from snipebot.config import (
    ConfigError,
    DuplicateKeyError,
    FingerprintGuardError,
    InvalidValueError,
    compute_fingerprints,
    fingerprint_guard,
    load_config,
)
from snipebot.ledger import (
    MalformedLedgerError,
    State,
    dumps_row,
    dumps_state,
    load_ledger,
    load_state,
    save_state,
)
from snipebot.parse import Candidate
from snipebot.ts import US_PER_SECOND

SNIPER = "U0AAA001"
TARGET = "U0AAA002"
TARGET2 = "U0AAA003"
SIG = "0" * 64

_REST = """\
timezone: {tz}
rules: {{}}
consent: {{veto: {{emoji: x}}}}
feedback: {{reactions: {{}}}}
"""


def _config_text(*, channel: str = "C0MAIN01", tz: str = "America/New_York",
                 semesters: str = "[{name: fall, start: 2026-09-01, end: 2026-12-20}]",
                 players: str = "{extras: [U0AAA001, U0AAA002]}") -> str:
    return (f"slack: {{channel: {channel}}}\n" + _REST.format(tz=tz)
            + f"semesters: {semesters}\n" + f"players: {players}\n")


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _us(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> int:
    return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0)) * US_PER_SECOND


def _row(ts: str, targets: tuple[str, ...]) -> Candidate:
    return Candidate(
        ts=ts, sender=SNIPER, subtype=None, thread_ts=None, targets=targets,
        live_images=1, live_image_ids=("F0FILE001",), live_videos=0, linked_images=0,
        last_edit_ts=None, file_sigs=(SIG,), vetoes=(), missing_runs=0,
        first_seen_targets=frozenset(targets), first_sight_edited=False,
        target_edited_in=(),
    )


# --------------------------------------------------------------------------- E-W4-5


def test_repeated_nested_key_is_a_duplicate_key_error_naming_key_and_line(tmp_path):
    """E-W4-5: a mapping key repeated anywhere in config.yaml raises DuplicateKeyError (a
    ConfigError) whose message names the key and its 1-based line."""
    text = _config_text(players="\n  groups:\n    reds: [U0AAA001]\n    reds: [U0AAA002]")
    path = _write(tmp_path, text)
    line = text.splitlines().index("    reds: [U0AAA002]") + 1
    with pytest.raises(DuplicateKeyError) as info:
        load_config(path)
    assert isinstance(info.value, ConfigError)
    assert "'reds'" in str(info.value)
    assert f"line {line}" in str(info.value)


def test_repeated_top_level_and_flow_keys_are_refused(tmp_path):
    """E-W4-5: the rule holds at the top level and inside a flow mapping."""
    top = _config_text() + "players: {extras: [U0AAA003]}\n"
    with pytest.raises(DuplicateKeyError, match="'players'"):
        load_config(_write(tmp_path, top))

    flow = _config_text(semesters="[{name: fall, start: 2026-09-01, end: 2026-12-20, end: 2026-12-21}]")
    with pytest.raises(DuplicateKeyError, match="'end'"):
        load_config(_write(tmp_path, flow))


def test_config_without_repeated_keys_still_loads(tmp_path):
    """E-W4-5 control: the duplicate check leaves a well-formed config loading."""
    config = load_config(_write(tmp_path, _config_text()))
    assert config.channel == "C0MAIN01"
    assert set(config.roster.entries) == {SNIPER, TARGET}


# --------------------------------------------------------------------------- E-W4-9


def test_semester_end_is_next_local_midnight_minus_one_us(tmp_path):
    """E-W4-9: end_us is the instant the local day after `end` begins, minus 1 us."""
    config = load_config(_write(tmp_path, _config_text()))
    (sem,) = config.semesters
    assert sem.end_us == _us(2026, 12, 21, 5) - 1          # 2026-12-21 00:00 EST
    assert sem.start_us == _us(2026, 9, 1, 4)              # 2026-09-01 00:00 EDT


def test_semester_end_covers_an_hour_repeated_by_a_midnight_fall_back(tmp_path):
    """E-W4-9: America/Santiago falls back at 2026-04-05 00:00 -03 to 2026-04-04 23:00 -04,
    so 23:00-23:59 on the end date occurs twice; both occurrences are inside the
    semester and the next local day (00:00 -04) is not."""
    text = _config_text(tz="America/Santiago",
                        semesters="[{name: autumn, start: 2026-03-01, end: 2026-04-04}]")
    (sem,) = load_config(_write(tmp_path, text)).semesters
    assert sem.end_us == _us(2026, 4, 5, 4) - 1
    assert sem.contains(_us(2026, 4, 5, 2, 30))            # first 23:30
    assert sem.contains(_us(2026, 4, 5, 3, 30))            # repeated 23:30
    assert not sem.contains(_us(2026, 4, 5, 4))            # 2026-04-05 00:00 -04


def test_semester_end_before_a_midnight_spring_forward_gap(tmp_path):
    """E-W4-9: America/Santiago springs forward at 2026-09-06 00:00 -04 to 01:00 -03; the
    nonexistent midnight resolves (fold=0) to 04:00 UTC, the instant the next day begins."""
    text = _config_text(tz="America/Santiago",
                        semesters="[{name: winter, start: 2026-06-01, end: 2026-09-05}]")
    (sem,) = load_config(_write(tmp_path, text)).semesters
    assert sem.end_us == _us(2026, 9, 6, 4) - 1


def test_adjacent_semesters_still_do_not_overlap(tmp_path):
    """E-W4-9: with end_us one microsecond before the next local midnight, a semester that
    starts the day after another ends is adjacent, not overlapping."""
    text = _config_text(semesters=(
        "[{name: fall, start: 2026-09-01, end: 2026-12-20},"
        " {name: winter, start: 2026-12-21, end: 2027-01-10}]"))
    fall, winter = load_config(_write(tmp_path, text)).semesters
    assert winter.start_us == fall.end_us + 1


# --------------------------------------------------------------------------- E-W4-10


def test_ledger_line_with_repeated_target_is_malformed(tmp_path):
    """E-W4-10: load_ledger rejects a row whose `targets` repeat an ID, naming the line."""
    good = dumps_row(_row("1758200000.000001", (TARGET,)))
    obj = json.loads(dumps_row(_row("1758200001.000001", (TARGET, TARGET2))))
    obj["targets"] = [TARGET, TARGET2, TARGET]
    bad = json.dumps(obj, separators=(",", ":"))
    path = tmp_path / "ledger.jsonl"
    path.write_bytes((good.rstrip("\n") + "\n" + bad + "\n").encode("utf-8"))
    with pytest.raises(MalformedLedgerError, match="line 2"):
        load_ledger(path)


def test_ledger_line_with_distinct_targets_loads(tmp_path):
    """E-W4-10 control: distinct targets still load unchanged."""
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(dumps_row(_row("1758200001.000001", (TARGET, TARGET2))).encode("utf-8"))
    (row,) = load_ledger(path)
    assert row.targets == (TARGET, TARGET2)


# --------------------------------------------------------------------------- E-W4-17


def test_state_fingerprints_at_defaults_none_and_round_trips(tmp_path):
    """E-W4-17: State.fingerprints_at defaults to None, is written to state.json once
    recorded (an unrecorded one stays absent, which loads as None) and loads back
    unchanged."""
    assert State().fingerprints_at is None
    assert load_state(tmp_path / "missing.json").fingerprints_at is None

    state = State(watermark="1758210123.000000", fingerprints={"rules": "a" * 64},
                  fingerprints_at="1758209000.000000", opted_out={SNIPER: 5})
    path = tmp_path / "state.json"
    save_state(path, state)
    assert json.loads(path.read_text(encoding="utf-8"))["fingerprints_at"] == "1758209000.000000"
    assert load_state(path) == state

    assert "fingerprints_at" not in json.loads(dumps_state(State()))


def test_state_written_before_fingerprints_at_loads_as_none(tmp_path):
    """E-W4-17: a state.json with no `fingerprints_at` key (written before the field
    existed) loads with fingerprints_at None; a non-ts value is malformed."""
    path = tmp_path / "state.json"
    old = {"version": 1, "watermark": None, "fingerprints": {}, "opted_out": {}}
    path.write_text(json.dumps(old), encoding="utf-8")
    assert load_state(path).fingerprints_at is None

    path.write_text(json.dumps({**old, "fingerprints_at": 12}), encoding="utf-8")
    with pytest.raises(MalformedLedgerError, match="fingerprints_at"):
        load_state(path)


def test_fingerprint_guard_recomputes_at_the_given_h(tmp_path):
    """E-W4-17: fingerprint_guard recomputes at the H it is given (state.json
    `fingerprints_at`), so fingerprints stored at H1 still match after the ledger gained a
    row past a dated roster addition; without an H it recomputes at the newest row (the
    old behaviour) and refuses."""
    players = "{extras: [U0AAA001, U0AAA002, {id: U0AAA003, from: 2026-09-10}]}"
    config = load_config(_write(tmp_path, _config_text(players=players)))
    h1 = _us(2026, 9, 8, 14)
    h2 = _us(2026, 9, 11, 14)
    stored = compute_fingerprints(config, h1)
    assert compute_fingerprints(config, h2)["players"] != stored["players"]

    fingerprint_guard(config, [h1, h2], stored, h_us=h1)          # converges
    with pytest.raises(FingerprintGuardError):
        fingerprint_guard(config, [h1, h2], stored)               # no H: newest row
    fingerprint_guard(config, [h1], stored)                       # no H, still H1: passes


# --------------------------------------------------------------------------- E-W4-28


def test_channel_id_may_start_with_g(tmp_path):
    """E-W4-28: a legacy private channel ID (G...) is a valid ChannelID; other prefixes
    are not."""
    load_config(_write(tmp_path, _config_text(channel="G0PRIV01")))
    for bad in ("D0DM0001", "g0priv01", "G0P"):
        with pytest.raises(InvalidValueError):
            load_config(_write(tmp_path, _config_text(channel=bad)))
