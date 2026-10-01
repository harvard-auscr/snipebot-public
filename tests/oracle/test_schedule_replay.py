"""L2-SR-*: schedule-replay equality within the plan section 2 bound (50 section 4).

One ground-truth timeline is replayed under several sync schedules against a `FakeSlack`
that answers history as of a simulated `now`. R6 splits the convergence claim in two:

* the final `ledger.jsonl` is byte-identical across schedules on the keys that converge --
  1-11 and 16 (`live_image_ids`) -- while the schedule-dependent, observation-order keys
  12 (`missing_runs`), 13-15 (late_tag evidence), 17-19 (face-fact evidence) and 20
  (`selfie_override`) are masked (`replay.mask_ledger`) before the comparison; and
* the derived `verdicts.jsonl` is equal across schedules *modulo the section 4.4 late_tag
  carve-out* (`replay.verdicts_equal_modulo_carveout`): a pair may be COUNTED under one
  schedule and LATE_TAG under another, but only when its row carries a `target_edited_in`
  entry outside the edit grace.

The placeholder timeline has no late edit-in, no tombstone and no admin selfie reaction, so
its carve-out keys in fact agree too; the contract is nonetheless asserted the way the spec
states it -- masked, and modulo the carve-out. The dedicated timelines below each exercise
one masked key (a deleted row for 12, an admin selfie reaction for 20) or the verdict
carve-out (a tag edited in outside grace).
"""
from __future__ import annotations

import functools
import json

import pytest

from tests.oracle import replay

# run_sync is the load-bearing dependency; skip the whole module until sync exists.
pytest.importorskip("snipebot.sync", reason="sync.run_sync not implemented yet")
pytest.importorskip("tests.fake_slack", reason="FakeSlack authoring API not implemented yet")

from tests._helpers_sync import image_file, make_config, mkts, roster_of  # noqa: E402

CHANNEL = "C0MAIN01"
A, B, C, D = "U0A", "U0B", "U0C", "U0D"
ADMIN = "U0ADMIN"


def _placeholder_timeline() -> replay.Timeline:
    """An in-bound ground-truth timeline: two independent counted snipes with images,
    posted five minutes apart on 2026-09-18. No late tag edit and no file tombstone, so
    every schedule converges within the plan §2 bound."""
    roster = roster_of({A: "fam", B: "fam", C: "fam", D: "fam"})
    config = make_config(roster=roster, reports=(), selfie_bonus=False)
    events = (
        replay.AuthoringEvent(
            at=mkts(2026, 9, 18, 12, 0), kind="post",
            payload={"user": A, "channel": CHANNEL, "text": f"<@{B}>",
                     "files": [image_file("F01", b"snap-a")]},
        ),
        replay.AuthoringEvent(
            at=mkts(2026, 9, 18, 12, 5), kind="post",
            payload={"user": C, "channel": CHANNEL, "text": f"<@{D}>",
                     "files": [image_file("F02", b"snap-b")]},
        ),
    )
    return replay.Timeline(
        config=config, events=events, channels=(CHANNEL,), horizon_days=90,
    )


@pytest.fixture(scope="module")
def timeline() -> replay.Timeline:
    return _placeholder_timeline()


@pytest.fixture(scope="module")
def reference_both(timeline) -> tuple[bytes, bytes]:
    """B = every_minute(tl): the reference (ledger, verdicts) every other schedule matches."""
    return replay.replay_both(timeline, replay.every_minute(timeline))


def test_reference_captured_every_snipe(reference_both):
    # Sanity: the reference actually observed both events (two ledger rows), so the
    # equality below is meaningful and not a match of two empty ledgers.
    ref_ledger, _ = reference_both
    assert len(replay.mask_ledger(ref_ledger)) == 2


# All four non-reference schedules (50 §4.2). jitter_dropped and three_day_outage take an
# extra arg, bound here: a fixed seed for reproducible cron jitter/dropped ticks (plan §9 L2)
# and an `at` three days into the 14-day span so the outage's >3-day gap spans no change and
# equality still holds (50 §4.3, the in-bound row).
_OUTAGE_AT = mkts(2026, 9, 21, 12, 0)


@pytest.mark.parametrize(
    "generator",
    [
        replay.six_hourly,
        replay.single_final,
        functools.partial(replay.jitter_dropped, seed=0),
        functools.partial(replay.three_day_outage, at=_OUTAGE_AT),
    ],
    ids=["six_hourly", "single_final", "jitter_dropped", "three_day_outage"],
)
def test_schedules_agree_within_bound(timeline, reference_both, generator):
    ref_ledger, ref_verdicts = reference_both
    schedule = generator(timeline)
    assert schedule, "generator produced an empty schedule"
    got_ledger, got_verdicts = replay.replay_both(timeline, schedule)

    # Ledger: keys 1-11 and 16 are byte-identical across cadences; 12-15/17-20 are masked.
    assert replay.mask_ledger(got_ledger) == replay.mask_ledger(ref_ledger)

    # Verdicts: this timeline carries no late edit-in, so the carve-out set is empty and the
    # verdicts must be exactly equal across schedules.
    carve = replay.late_tag_carveout_pairs(timeline.config.rules, got_ledger, ref_ledger)
    assert carve == set()
    assert replay.verdicts_equal_modulo_carveout(got_verdicts, ref_verdicts, carve)


def _statuses(verdicts_bytes: bytes) -> list[tuple[str, str]]:
    """(ts, message status) pairs from a serialized verdicts.jsonl text."""
    out: list[tuple[str, str]] = []
    for line in verdicts_bytes.decode("utf-8").splitlines():
        if not line:
            continue
        obj = json.loads(line)
        out.append((obj["ts"], obj["status"]))
    return out


# --------------------------------------------------------------------------- #
# The §4.4 late_tag carve-out: the one verdict divergence a schedule may carry.
# --------------------------------------------------------------------------- #

def _late_edit_in_timeline() -> replay.Timeline:
    """A tag edited in *outside* edit_grace (50 §4.4): posted untagged at 12:00, the target
    edited in 30 min later (grace is 10 min). A schedule that polled the untagged window
    reaches LATE_TAG; one whose first sight is already tagged reaches COUNTED."""
    roster = roster_of({A: "fam", B: "fam", C: "fam", D: "fam"})
    config = make_config(roster=roster, reports=(), selfie_bonus=False)
    events = (
        replay.AuthoringEvent(
            at=mkts(2026, 9, 18, 12, 0), kind="post",
            payload={"user": A, "channel": CHANNEL, "text": "hello",
                     "files": [image_file("F01", b"snap-a")]},
        ),
        replay.AuthoringEvent(
            at=mkts(2026, 9, 18, 12, 30), kind="edit",     # +30 min > edit_grace (10 min)
            payload={"ts": mkts(2026, 9, 18, 12, 0), "channel": CHANNEL,
                     "user": A, "text": f"<@{B}>"},
        ),
    )
    return replay.Timeline(
        config=config, events=events, channels=(CHANNEL,), horizon_days=90,
    )


def test_late_edit_in_outside_grace_carveout_is_only_diff():
    """50 §4.4: for a tag edited in outside edit_grace the verdict is schedule-dependent by
    design, and that split is the ONLY thing allowed to differ across schedules.

    single_final (first sight already tagged) -> COUNTED; six_hourly (saw the untagged
    window) -> LATE_TAG. The masked ledgers still agree (the deciding fields are masked keys
    13/15), the raw verdicts differ, and the difference is exactly the late_tag carve-out on
    (post_ts, B) -- with the carve-out disallowed the verdicts are no longer equal, which is
    what makes it the sole divergence."""
    tl = _late_edit_in_timeline()
    counted_l, counted_v = replay.replay_both(tl, replay.single_final(tl))
    late_l, late_v = replay.replay_both(tl, replay.six_hourly(tl))

    # The masked-ledger equality path still holds: the divergence hides in keys 13-15.
    assert replay.mask_ledger(counted_l) == replay.mask_ledger(late_l)

    # The raw verdicts DO differ, and at message level it is the documented COUNTED/LATE_TAG.
    assert counted_v != late_v
    counted = dict(_statuses(counted_v))
    late = dict(_statuses(late_v))
    (post_ts,) = counted.keys()
    assert counted[post_ts] == "counted"          # first sight already tagged
    assert late[post_ts] == "not_counted"         # saw the untagged window -> LATE_TAG

    # The carve-out is precisely (post_ts, B), taken over the union of both ledgers (the
    # late edit-in is a masked, schedule-dependent fact, recorded only by six_hourly).
    carve = replay.late_tag_carveout_pairs(tl.config.rules, counted_l, late_l)
    assert carve == {(post_ts, B)}

    # Modulo that carve-out the verdicts are equal; disallow it and they are not -- so the
    # carve-out is the only difference.
    assert replay.verdicts_equal_modulo_carveout(counted_v, late_v, carve)
    assert not replay.verdicts_equal_modulo_carveout(counted_v, late_v, set())


# --------------------------------------------------------------------------- #
# Masked keys 12 (missing_runs) and 20 (selfie_override): schedule-dependent facts that
# R6 folds into the mask so the ledgers still converge under it.
# --------------------------------------------------------------------------- #

def _deleted_row_timeline() -> replay.Timeline:
    """A counted snipe posted at 12:00 and deleted one minute later. Every schedule that
    stored it before the delete then observes the absence a schedule-dependent number of
    times, so `missing_runs` (key 12) differs across cadences. `scan_days=30` keeps the
    tombstoned row inside the scan window at the span end so both schedules retain it (a
    deleted row below the scan floor is dropped, 20 §5)."""
    roster = roster_of({A: "fam", B: "fam", C: "fam", D: "fam"})
    config = make_config(roster=roster, reports=(), selfie_bonus=False, scan_days=30)
    events = (
        replay.AuthoringEvent(
            at=mkts(2026, 9, 18, 12, 0), kind="post",
            payload={"user": A, "channel": CHANNEL, "text": f"<@{B}>",
                     "files": [image_file("F01", b"snap-a")]},
        ),
        replay.AuthoringEvent(
            at=mkts(2026, 9, 18, 12, 1), kind="delete_message",
            payload={"ts": mkts(2026, 9, 18, 12, 0), "channel": CHANNEL},
        ),
        # A surviving text-only post (never stored) keeps every later complete fetch
        # non-empty: a fetch returning zero messages infers no misses (E-W4-16).
        replay.AuthoringEvent(
            at=mkts(2026, 9, 18, 12, 5), kind="post",
            payload={"user": C, "channel": CHANNEL, "text": "hello"},
        ),
    )
    return replay.Timeline(
        config=config, events=events, channels=(CHANNEL,), horizon_days=90,
    )


def test_deleted_row_missing_runs_masked():
    """A deleted row makes `missing_runs` (key 12) schedule-dependent. six_hourly and
    three_day_outage both store the row at 12:00 (both fire at the span start) and both
    observe the absence thereafter, but a different number of times: the raw ledgers differ
    on key 12, yet mask it and they are byte-identical."""
    tl = _deleted_row_timeline()
    six = replay.replay(tl, replay.six_hourly(tl))
    outage = replay.replay(tl, replay.three_day_outage(tl, at=mkts(2026, 9, 22, 0, 0)))

    assert six != outage                                        # differ on missing_runs
    # Both retained the tombstoned row (not a match of two empty ledgers).
    assert len(replay.mask_ledger(six)) == 1
    assert {json.loads(l)["missing_runs"] for l in six.decode().splitlines() if l} != \
           {json.loads(l)["missing_runs"] for l in outage.decode().splitlines() if l}
    assert replay.mask_ledger(six) == replay.mask_ledger(outage)  # ...but equal under mask


def _admin_selfie_late_timeline() -> replay.Timeline:
    """A counted snipe posted on 2026-09-18, then an admin selfie reaction landing ~13 days
    later, near the 14-day scan-window edge. A schedule polling the short window in which the
    row is both still in range and already reacted records `selfie_override` (key 20); one
    whose ticks miss that window never does. `selfie_bonus` is off, so the override never
    changes a verdict -- it is a pure ledger differ."""
    roster = roster_of({A: "fam", B: "fam", C: "fam", D: "fam"})
    config = make_config(roster=roster, reports=(), selfie_bonus=False,
                         selfie_emoji="camera", admins=(ADMIN,), scan_days=14)
    events = (
        replay.AuthoringEvent(
            at=mkts(2026, 9, 18, 12, 0), kind="post",
            payload={"user": A, "channel": CHANNEL, "text": f"<@{B}>",
                     "files": [image_file("F01", b"snap-a")]},
        ),
        replay.AuthoringEvent(
            at=mkts(2026, 10, 1, 12, 0), kind="react",     # ~13 days on, near the window edge
            payload={"ts": mkts(2026, 9, 18, 12, 0), "channel": CHANNEL,
                     "user": ADMIN, "name": "camera"},
        ),
    )
    return replay.Timeline(
        config=config, events=events, channels=(CHANNEL,), horizon_days=90,
    )


def test_admin_selfie_late_reaction_selfie_override_masked():
    """A late admin selfie reaction makes `selfie_override` (key 20) schedule-dependent.
    six_hourly polls the in-range-and-reacted window and records the override; a
    three_day_outage whose gap straddles that window never does. The raw ledgers differ on
    key 20; mask it and they are byte-identical."""
    tl = _admin_selfie_late_timeline()
    six = replay.replay(tl, replay.six_hourly(tl))
    outage = replay.replay(tl, replay.three_day_outage(tl, at=mkts(2026, 9, 30, 0, 0)))

    assert six != outage                                        # differ on selfie_override
    overrides = {
        name: {json.loads(l)["selfie_override"] is not None
               for l in led.decode().splitlines() if l}
        for name, led in (("six", six), ("outage", outage))
    }
    assert overrides["six"] == {True} and overrides["outage"] == {False}
    assert replay.mask_ledger(six) == replay.mask_ledger(outage)  # ...but equal under mask
