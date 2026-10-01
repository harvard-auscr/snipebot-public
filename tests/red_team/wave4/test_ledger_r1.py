"""Red-team wave 4, round 1, surface "ledger": the facts-only ledger format, fail-closed
loading, the config-free integrity checks, and how merge carries facts across runs.

Each test reproduces one violation and FAILS on the current code for exactly the reason in
its docstring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snipebot.faces import FakeFaceDetector
from snipebot.ledger import (
    LedgerIntegrityError,
    MalformedLedgerError,
    check_integrity,
    dumps_ledger,
    dumps_row,
    load_ledger,
    load_state,
)
from snipebot.parse import Candidate, Veto, VetoSource
from snipebot.rules import MessageVerdict, PairVerdict, Reason, SelfieClass, Status
from snipebot.sync import run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import CHANNEL, image_file, make_config, mkts, roster_of

SNIPER = "U0AAA001"
TARGET = "U0AAA002"
TARGET2 = "U0AAA003"
ADMIN = "U0AAA009"
BOT = "U0BOT01"
SIG = "0" * 64


def _row(ts: str, *, targets=(TARGET,), vetoes=(), last_edit_ts=None) -> Candidate:
    return Candidate(
        ts=ts, sender=SNIPER, subtype=None, thread_ts=None, targets=tuple(targets),
        live_images=1, live_image_ids=("F0FILE001",), live_videos=0, linked_images=0,
        last_edit_ts=last_edit_ts, file_sigs=(SIG,), vetoes=tuple(vetoes), missing_runs=0,
        first_seen_targets=frozenset(targets), first_sight_edited=False,
        target_edited_in=(),
    )


def _mv(ts, status, pairs):
    reason = Reason.COOLDOWN if status is Status.COOLDOWN else Reason.COUNTED
    return MessageVerdict(ts=ts, status=status, reason=reason,
                          selfie=SelfieClass.NOT_APPLICABLE, pairs=tuple(pairs))


def _pv(ts, target, status, blocked_by=None):
    reason = Reason.COOLDOWN if status is Status.COOLDOWN else Reason.COUNTED
    return PairVerdict(ts=ts, target=target, status=status, reason=reason,
                       blocked_by=blocked_by, selfie=False)


# --------------------------------------------------------------------------- merge


def test_reaction_veto_on_row_refetched_below_scan_floor_is_wiped(tmp_path: Path):
    """A REACTION veto is silently erased from a row that a pending-miss (or gap-recovery,
    or backfill) fetch pulls in from BELOW the scan floor, so a photo an admin vetoed
    starts to COUNT.

    00-data §2 "Veto ownership (merge rule)": on each merge the REACTION vetoes are dropped
    and "rebuilt from the eligible reactors currently on the message". `sync._replace_facts`
    drops them for every returned row, but step 5 (`_observe_vetoes`) rebuilds them only for
    rows at or above `scan_floor_us`. 20 §3 makes the fetch range reach below the scan
    floor in normal operation: a deleted row keeps `missing_runs >= 1`, so the run where it
    ages past the floor sets `oldest` to its ts and every message posted between it and the
    floor is returned and replaced. Their admin veto is still on the message (the payload
    carries it) yet the rebuilt set is empty, and from then on the row is outside the window
    for good: the veto is lost permanently and the pairs score. Whether a given row falls in
    that slice depends on the sync schedule, so this also breaks 00-data §3's guarantee that
    key 11 (`vetoes`, outside every carve-out) is byte-identical across sync schedules.
    """
    roster = roster_of({SNIPER: "fam-a", TARGET: "fam-b", ADMIN: None})
    cfg = make_config(roster=roster, admins=(ADMIN,), scan_days=1)
    users = {BOT: FakeUser(id=BOT, is_bot=True)}
    for u in (SNIPER, TARGET, ADMIN):
        users[u] = FakeUser(id=u)
    slack = FakeSlack(now=mkts(2026, 9, 18, 9), bot_user_id=BOT, users=users)

    def run(now: str):
        slack.as_of(now)
        d = tmp_path / "data"
        d.mkdir(exist_ok=True)
        return run_sync(
            slack, cfg, detector=FakeFaceDetector({}),
            ledger_path=d / "ledger.jsonl", state_path=d / "state.json",
            now_us=parse_ts(now), no_post=True,
        )

    # A: a snipe that will later be deleted. B: 20 minutes later, vetoed by the admin.
    ts_a = slack.post(at=mkts(2026, 9, 18, 10, 0), user=SNIPER, channel=CHANNEL,
                      text=f"<@{TARGET}>", files=[image_file("F0FILE001", b"photo-1")])
    ts_b = slack.post(at=mkts(2026, 9, 18, 10, 20), user=SNIPER, channel=CHANNEL,
                      text=f"<@{TARGET}>", files=[image_file("F0FILE002", b"photo-2")])
    slack.react(at=mkts(2026, 9, 18, 10, 25), ts=ts_b, channel=CHANNEL, user=ADMIN,
                name="no_entry_sign")
    assert run(mkts(2026, 9, 18, 12)).exit_code == 0

    led = tmp_path / "data" / "ledger.jsonl"
    before = {r.ts: r for r in load_ledger(led)}
    assert [(v.by, v.source) for v in before[ts_b].vetoes] == [(ADMIN, VetoSource.REACTION)]

    # A is deleted; two complete fetches confirm it (missing_runs 1 -> 2).
    slack.delete_message(at=mkts(2026, 9, 18, 13), ts=ts_a, channel=CHANNEL)
    assert run(mkts(2026, 9, 18, 14)).exit_code == 0
    assert run(mkts(2026, 9, 18, 15)).exit_code == 0

    # Next day: the scan floor (now - 1 day = 10:30) has passed both A and B. A's pending
    # miss pulls `oldest` back to A's ts, so B is returned and merged again.
    assert run(mkts(2026, 9, 19, 10, 30)).exit_code == 0

    after = {r.ts: r for r in load_ledger(led)}
    assert ts_b in after
    # The admin's veto is still on the message in Slack; it must still be in the ledger.
    assert [(v.by, v.source) for v in after[ts_b].vetoes] == [(ADMIN, VetoSource.REACTION)]


# --------------------------------------------------------------------------- load


def test_duplicate_json_key_in_a_ledger_line_is_accepted_and_drops_a_veto(tmp_path: Path):
    """A ledger line that repeats a key loads silently with the LAST value winning, so a
    corrupted line can drop a durable CLI veto (the only copy of it is the ledger) and the
    vetoed photo scores.

    20 §7.1 / 00-data §3 Load: every line must have "exactly the 20 keys, no more, no fewer"
    and ANY malformed line aborts with MalformedLedgerError (fail closed). `json.loads`
    collapses duplicate keys before `_parse_row` compares the key set, so a 21-key line
    (`"vetoes"` twice) passes the key-set check.
    """
    row = _row(mkts(2026, 9, 18, 10), vetoes=(Veto(ADMIN, VetoSource.CLI),))
    line = dumps_row(row)
    assert '"vetoes":[{"by":"U0AAA009","source":"cli"}]' in line
    corrupt = line[:-1] + ',"vetoes":[]}'
    p = tmp_path / "ledger.jsonl"
    p.write_bytes((corrupt + "\n").encode("utf-8"))
    with pytest.raises(MalformedLedgerError):
        load_ledger(p)


# --------------------------------------------------------------------------- integrity


def test_check_integrity_passes_a_row_the_loader_rejects(tmp_path: Path):
    """`check_integrity` does not verify check 1 (loadable) for the rows it is given, so at
    end of sync (step 8, rows built in memory, never loaded) a row the loader will reject is
    persisted, and every later run then fails closed with exit 4 until someone repairs the
    file by hand.

    20 §7.3: step 8 runs checks 1-5 "before any write or push", and the table says
    "`check_integrity` covers checks 1-4"; check 1 is "every line well-formed (§7.1)". At
    step 8 the rows were merged from fresh parses (parse.py never validates `thread_ts` or
    `edited.ts`), so loadability is not implied. Here `last_edit_ts` is not a Slack ts; the
    loader rejects the serialized row, but check_integrity accepts it.
    """
    bad = _row(mkts(2026, 9, 18, 10), last_edit_ts="1790200376")
    p = tmp_path / "ledger.jsonl"
    p.write_bytes(dumps_ledger([bad]).encode("utf-8"))
    with pytest.raises(MalformedLedgerError):
        load_ledger(p)  # precondition: the persisted row is unloadable
    verdict = _mv(bad.ts, Status.COUNTED, [_pv(bad.ts, TARGET, Status.COUNTED)])
    with pytest.raises((LedgerIntegrityError, MalformedLedgerError)):
        check_integrity([bad], [verdict])


def test_check3_accepts_a_blocked_by_anchor_on_a_different_target():
    """Check 3 accepts a COOLDOWN pair whose `blocked_by` row carries a COUNTED pair only
    for a DIFFERENT target, which can never be that pair's cooldown anchor.

    20 §7.3 check 3: "the row at `blocked_by` is the cooldown anchor in the same scope".
    00-data §5 keys every scope on the target (`PAIR` -> (sender, target), `TARGET` ->
    (target,)), so the anchor row must carry a COUNTED (or, under reset, a rejected) pair
    for the same target whatever the config. `check_integrity` only asks that the anchor
    row carry ANY COUNTED/COOLDOWN pair.
    """
    a, b = mkts(2026, 9, 18, 10), mkts(2026, 9, 18, 10, 5)
    rows = [_row(a, targets=(TARGET2,)), _row(b, targets=(TARGET,))]
    verdicts = [
        _mv(a, Status.COUNTED, [_pv(a, TARGET2, Status.COUNTED)]),
        _mv(b, Status.COOLDOWN, [_pv(b, TARGET, Status.COOLDOWN, blocked_by=a)]),
    ]
    with pytest.raises(LedgerIntegrityError):
        check_integrity(rows, verdicts)
