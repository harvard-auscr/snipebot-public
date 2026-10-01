"""Schedule-replay harness (50 section 4).

One ground-truth timeline of authoring events is replayed under several sync schedules
against a ``FakeSlack`` that answers history as of a simulated ``now``; within the plan
section 2 bound the final ``ledger.jsonl`` must be byte-identical across schedules.

The comparison drives ``snipebot.sync.run_sync`` and the ``FakeSlack`` authoring API,
neither of which exists in this build yet. Everything here that touches them imports
lazily, so the schedule generators (pure functions of the event list) are usable now and
``test_schedule_replay.py`` skips the rest cleanly until sync lands.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, AbstractSet, Any, Sequence

from snipebot.ts import parse_ts

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snipebot.config import Config

SCAN_DAYS = 14
DAY_US = 86_400_000_000
HOUR_US = 3_600_000_000
MINUTE_US = 60_000_000

# The bot's own user id in a replayed world (never a config player).
BOT_USER_ID = "U0BOTREPLAY"

# Minute-cadence head for the `every_minute` reference (see `every_minute`). The span
# itself is the full 50 §4.2 window (`last_event + scan_days`); this only bounds how far
# past the last event the reference emits *minute* ticks before falling back to a single
# span-end tick, so `every_minute` stays tractable without shrinking the span that the
# out-of-bound generators (`three_day_outage`, the day-10-14 cases) depend on.
EVERY_MINUTE_HEAD_US = 7 * HOUR_US


@dataclass(frozen=True)
class AuthoringEvent:
    at: str                      # Slack ts string
    kind: str                    # post / edit / delete_message / delete_file / react / unreact / reply
    payload: dict[str, Any]


@dataclass(frozen=True)
class Timeline:
    config: "Config"
    events: tuple[AuthoringEvent, ...]   # sorted by parse_ts(at); Slack ts strings only
    channels: tuple[str, ...]            # watched + any post_to
    horizon_days: int | None


def _sorted_events(events: Sequence[AuthoringEvent]) -> list[AuthoringEvent]:
    return sorted(events, key=lambda e: parse_ts(e.at))


def _span_us(tl: Timeline) -> tuple[int, int]:
    """[first_event, last_event + scan_days]; each generator spans this so every change
    is observable at least once."""
    evs = _sorted_events(tl.events)
    first = parse_ts(evs[0].at)
    last = parse_ts(evs[-1].at)
    return first, last + SCAN_DAYS * DAY_US


def build_fake(tl: Timeline):
    """Construct a FakeSlack from the timeline and apply every authoring event.

    The fake starts at the first event's clock (each sync overrides it via ``as_of``) and
    knows every roster player plus the bot, so `history`/`users_list` answer consistently.
    """
    from tests.fake_slack import FakeSlack, FakeUser  # lazy: test infra
    from snipebot.ts import format_ts

    users: dict[str, "FakeUser"] = {BOT_USER_ID: FakeUser(id=BOT_USER_ID, is_bot=True)}
    for uid in tl.config.roster.entries:
        users[uid] = FakeUser(id=uid, display_name=uid)

    first = min(parse_ts(e.at) for e in tl.events)
    fake = FakeSlack(
        now=format_ts(first),
        bot_user_id=BOT_USER_ID,
        users=users,
        channels=tl.channels,
        bot_member_of=tl.channels,
        horizon_days=tl.horizon_days,
    )
    for ev in _sorted_events(tl.events):
        handler = getattr(fake, ev.kind)
        handler(at=ev.at, **ev.payload)
    return fake


# --------------------------------------------------------------------------- #
# Schedule generators: each returns a sequence of `now` instants (Slack ts strings).
# Pure functions of the event list; usable without sync or the fake.
# --------------------------------------------------------------------------- #

def _ticks(start_us: int, end_us: int, step_us: int) -> list[int]:
    ticks: list[int] = []
    t = start_us
    while t <= end_us:
        ticks.append(t)
        t += step_us
    return ticks


def _fmt(ticks_us: Sequence[int]) -> list[str]:
    from snipebot.ts import format_ts
    return [format_ts(t) for t in ticks_us]


def every_minute(tl: Timeline) -> list[str]:
    """Finest reference cadence. The span is the full `last_event + scan_days` window, so a
    literal 60 s step would be ~20k syncs; every in-bound change is observed within
    `EVERY_MINUTE_HEAD_US` of the last event, so we emit minute ticks over that head and a
    single tick at the span end -- tractable, without shrinking the span (`_span_us`)."""
    start, end = _span_us(tl)
    evs = _sorted_events(tl.events)
    last_event = parse_ts(evs[-1].at)
    head_end = min(end, last_event + EVERY_MINUTE_HEAD_US)
    ticks = _ticks(start, head_end, MINUTE_US)
    if not ticks or ticks[-1] < end:
        ticks.append(end)
    return _fmt(ticks)


def six_hourly(tl: Timeline) -> list[str]:
    start, end = _span_us(tl)
    return _fmt(_ticks(start, end, 6 * HOUR_US))


def single_final(tl: Timeline) -> list[str]:
    evs = _sorted_events(tl.events)
    return _fmt([parse_ts(evs[-1].at) + 1])


def jitter_dropped(tl: Timeline, seed: int) -> list[str]:
    """Actions-style: nominal hourly ticks shifted by +5..+20 min, ~15% dropped."""
    import random

    rng = random.Random(seed)
    start, end = _span_us(tl)
    out: list[int] = []
    for base in _ticks(start, end, HOUR_US):
        if rng.random() < 0.15:
            continue
        out.append(base + rng.randint(5, 20) * MINUTE_US)
    if not out:  # never emit an empty schedule
        out.append(end)
    return _fmt(sorted(out))


def three_day_outage(tl: Timeline, at: str) -> list[str]:
    """Hourly, but no tick inside a 3-day window starting at `at` (a gap at the bound)."""
    start, end = _span_us(tl)
    gap_start = parse_ts(at)
    gap_end = gap_start + 3 * DAY_US
    ticks = [t for t in _ticks(start, end, HOUR_US) if not (gap_start <= t < gap_end)]
    return _fmt(ticks)


def replay(tl: Timeline, schedule: Sequence[str]) -> bytes:
    """Run `run_sync` at each `now` against a `FakeSlack` that answers history as of that
    simulated clock, over a fresh files-store data dir; return the final canonical
    `dumps_ledger(rows)` bytes.

    Reactions and digests are suppressed: neither touches `ledger.jsonl`, so leaving them
    off keeps the replay to the ledger-convergence claim (50 §4.3) and much faster.
    """
    import hashlib
    import tempfile

    from snipebot.faces import FakeFaceDetector
    from snipebot.ledger import dumps_ledger, load_ledger
    from snipebot.sync import run_sync

    # Seed the detector from the timeline so that any rendition sync actually fetches (a
    # sib-tagged selfie-bonus snipe, 20 §5) has a face count -- a bare FakeFaceDetector({})
    # raises KeyError on the first live image and the faces carve-out (keys 17-19) could
    # never be exercised. Deterministic: one face per distinct image, keyed by the sha-256
    # of the bytes FakeSlack serves for it, exactly as `faces.count_faces` hashes them.
    detector_counts: dict[str, int] = {}
    for ev in tl.events:
        for f in ev.payload.get("files", []) or ():
            data = f.get("_bytes")
            if data is None:
                continue
            if str(f.get("mimetype", "")).startswith("image/"):
                detector_counts[hashlib.sha256(data).hexdigest()] = 1

    fake = build_fake(tl)
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.jsonl"
        state_path = Path(tmp) / "state.json"
        for now in schedule:
            fake.as_of(now)
            run_sync(
                fake, tl.config,
                detector=FakeFaceDetector(detector_counts),
                ledger_path=ledger_path, state_path=state_path,
                now_us=parse_ts(now), no_post=True, no_react=True,
            )
        return dumps_ledger(load_ledger(ledger_path)).encode("utf-8")


def _verdicts_from_ledger(tl: Timeline, ledger_bytes: bytes) -> bytes:
    """`dumps_verdicts(evaluate(...))` over already-computed ledger bytes: exactly what the
    schedule would have written to `verdicts.jsonl`. Pure function of the ledger + dated
    config."""
    import tempfile

    from snipebot.ledger import dumps_verdicts, load_ledger
    from snipebot.rules import evaluate

    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.jsonl"
        ledger_path.write_bytes(ledger_bytes)
        rows = load_ledger(ledger_path)
    verdicts = evaluate(
        rows, tl.config.rules, tl.config.roster, set(),
        tl.config.semesters, tl.config.tz,
    )
    return dumps_verdicts(verdicts).encode("utf-8")


def replay_both(tl: Timeline, schedule: Sequence[str]) -> tuple[bytes, bytes]:
    """Run the schedule once and return `(ledger_bytes, verdicts_bytes)` side by side.

    R6: a schedule replay checks BOTH artifacts. The ledger convergence (50 §4.3) is
    asserted on the masked ledger (`mask_ledger`), while the verdict convergence (§4.4) is
    asserted on `verdicts.jsonl` modulo the late_tag carve-out
    (`verdicts_equal_modulo_carveout`). The ledger is computed only once here, so a caller
    that needs both never re-runs the (many-sync) replay.
    """
    ledger_bytes = replay(tl, schedule)
    return ledger_bytes, _verdicts_from_ledger(tl, ledger_bytes)


def replay_verdicts(tl: Timeline, schedule: Sequence[str]) -> bytes:
    """The `verdicts.jsonl` a schedule's final ledger implies.

    The COUNTED/LATE_TAG split of the §4.4 late_tag observation exception lives in
    `verdicts.jsonl`, never in `ledger.jsonl` (00-data §3-§4: the ledger is facts-only, the
    deciding fields `first_seen_targets`/`target_edited_in` are the masked keys 13/15). So a
    schedule-dependent verdict divergence is invisible to a masked-ledger comparison and is
    surfaced only here.
    """
    return _verdicts_from_ledger(tl, replay(tl, schedule))


# --------------------------------------------------------------------------- #
# R6: cross-schedule comparison helpers.
#
# The masked-ledger equality (50 §4.3) drops the schedule-dependent, observation-order
# keys; the verdict equality (§4.4) tolerates exactly the late_tag carve-out.
# --------------------------------------------------------------------------- #

# Ledger keys that are schedule-dependent (recorded at first sight / from the observed run
# count, never re-derived) and are masked before a cross-schedule ledger comparison:
#   12  missing_runs                          (how many complete fetches missed the row)
#   13-15 first_seen_targets / first_sight_edited / target_edited_in   (late_tag evidence)
#   17-19 face_counts / rendition_hash / detect_attempts               (face-fact evidence)
#   20  selfie_override                        (admin selfie reaction, first-seen reactor)
# Key 16 (live_image_ids) is NOT masked: it converges. (00-data §10 homes these names.)
MASKED_KEYS = frozenset({
    "missing_runs",
    "first_seen_targets", "first_sight_edited", "target_edited_in",
    "face_counts", "rendition_hash", "detect_attempts",
    "selfie_override",
})


def mask_ledger(ledger_bytes: bytes) -> list[dict]:
    """Parse canonical ledger bytes into rows with the schedule-dependent carve-out keys
    (`MASKED_KEYS`) dropped, so two schedules' ledgers can be compared on the keys that must
    converge (50 §4.3)."""
    import json

    rows: list[dict] = []
    for line in ledger_bytes.decode("utf-8").splitlines():
        if not line:
            continue
        obj = json.loads(line)
        rows.append({k: v for k, v in obj.items() if k not in MASKED_KEYS})
    return rows


def late_tag_carveout_pairs(rules: Any, *ledger_bytes: bytes) -> set[tuple[str, str]]:
    """The `(ts, target)` pairs eligible for the §4.4 late_tag carve-out: a row carries a
    `target_edited_in` entry for `target` (absent at first sight, dated) whose edit ts is
    strictly outside the edit grace of the rule in force at the row. Only such a pair may be
    COUNTED under one schedule and LATE_TAG under another.

    `target_edited_in` is itself a masked (schedule-dependent) key, so eligibility is taken
    over the UNION of the ledgers passed -- a pair is eligible if any schedule recorded the
    late edit-in.
    """
    import json

    from snipebot.config import NoRuleInForceError

    out: set[tuple[str, str]] = set()
    for lb in ledger_bytes:
        for line in lb.decode("utf-8").splitlines():
            if not line:
                continue
            obj = json.loads(line)
            ts = obj["ts"]
            ts_us = parse_ts(ts)
            try:
                grace = rules.in_force_at(ts_us).edit_grace_us
            except NoRuleInForceError:
                continue
            first_seen = set(obj.get("first_seen_targets", []))
            for te in obj.get("target_edited_in", []):
                target = te["user"]
                edit_ts = te["edit_ts"]
                if target in first_seen or edit_ts is None:
                    continue
                if parse_ts(edit_ts) > ts_us + grace:
                    out.add((ts, target))
    return out


def _verdict_rows(verdicts_bytes: bytes) -> dict[str, dict]:
    import json

    out: dict[str, dict] = {}
    for line in verdicts_bytes.decode("utf-8").splitlines():
        if not line:
            continue
        obj = json.loads(line)
        out[obj["ts"]] = obj
    return out


def verdicts_equal_modulo_carveout(
    a_verdicts: bytes, b_verdicts: bytes, carveout_pairs: "AbstractSet[tuple[str, str]]",
) -> bool:
    """True when two schedules' `verdicts.jsonl` agree except for the §4.4 late_tag carve-out.

    Every message must appear in both. A message whose full verdict object matches is fine.
    A message that differs is tolerated ONLY when the difference is one or more carve-out
    pairs each flipping between COUNTED and LATE_TAG (status not_counted): the pair must be
    in `carveout_pairs`, the two statuses must be exactly {counted, not_counted}, and the
    not_counted side's reason must be `late_tag`. The message-level status/reason/selfie
    rollup that follows from such a flip is tolerated too, but a message that differs with no
    explaining carve-out flip is a real divergence.
    """
    a = _verdict_rows(a_verdicts)
    b = _verdict_rows(b_verdicts)
    if a.keys() != b.keys():
        return False
    carve = set(carveout_pairs)
    for ts in a:
        oa, ob = a[ts], b[ts]
        if oa == ob:
            continue
        pa = {p["target"]: p for p in oa["pairs"]}
        pb = {p["target"]: p for p in ob["pairs"]}
        if pa.keys() != pb.keys():
            return False
        flips = 0
        for target in pa:
            if pa[target] == pb[target]:
                continue
            if (ts, target) not in carve:
                return False
            if {pa[target]["status"], pb[target]["status"]} != {"counted", "not_counted"}:
                return False
            nc = pa[target] if pa[target]["status"] == "not_counted" else pb[target]
            if nc["reason"] != "late_tag":
                return False
            flips += 1
        if flips == 0:
            return False  # message differs with nothing but a carve-out flip to explain it
    return True
