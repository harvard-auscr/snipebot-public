"""Red-team wave 4, round 2, surface "ledger": the facts-only ledger format and the
commands that rewrite it.

Each test reproduces one violation and FAILS on the current code for exactly the reason in
its docstring. Offline: files persistence under tmp_path, no Slack, no git.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.ledger import MalformedLedgerError, dumps_row, load_ledger, save_ledger
from snipebot.parse import Candidate, TargetEdit
from snipebot.rules import Reason, Status, evaluate
from snipebot.ts import US_PER_SECOND

from tests._helpers_sync import SEMESTER, TZ, mkts, roster_of, rule, us
from snipebot.config import DatedRules

SNIPER = "U0AAA001"
TARGET = "U0AAA002"
TARGET2 = "U0AAA003"
ADMIN = "U0AAA009"
SIG = "0" * 64


def _row(ts: str, *, targets=(TARGET,), first_seen=None, edited_in=()) -> Candidate:
    return Candidate(
        ts=ts, sender=SNIPER, subtype=None, thread_ts=None, targets=tuple(targets),
        live_images=1, live_image_ids=("F0FILE001",), live_videos=0, linked_images=0,
        last_edit_ts=None, file_sigs=(SIG,), vetoes=(), missing_runs=0,
        first_seen_targets=frozenset(targets if first_seen is None else first_seen),
        first_sight_edited=False, target_edited_in=tuple(edited_in),
    )


# --------------------------------------------------------------------------- load


def test_load_accepts_a_line_whose_targets_repeat_a_user(tmp_path: Path):
    """Claim: load_ledger accepts a line whose `targets` array repeats a user ID, so a
    corrupted line loads silently and changes the score of that snipe instead of failing
    closed with MalformedLedgerError naming the line.

    00-data §3 (key 5): `targets` is "current real-user targets, first-appearance order,
    **deduped**"; 00-data §2 repeats it ("deduped"); 00-data §3 Load and 20 §7.1: ANY
    malformed line aborts the run with MalformedLedgerError (exit 4) because a partial or
    corrupt ledger would flip verdicts. With the row loaded, `evaluate` counts T twice: under
    `max_targets_per_message: 1` the single-target snipe is gated TOO_MANY_TARGETS (a lost
    point, and check_integrity passes, so nothing ever flags it)."""
    ts = mkts(2026, 9, 10, 12)
    obj = json.loads(dumps_row(_row(ts)))
    obj["targets"] = [TARGET, TARGET]
    path = tmp_path / "ledger.jsonl"
    path.write_bytes((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))

    # The consequence, pinned so the claim is exact: the loaded row is voided.
    try:
        rows = load_ledger(path)
    except MalformedLedgerError:
        rows = None
    if rows is not None:
        rules = DatedRules(entries=(rule(max_targets=1),))
        roster = roster_of({SNIPER: None, TARGET: None})
        (mv,) = evaluate(rows, rules, roster, set(), (SEMESTER,), TZ)
        assert mv.reason is Reason.TOO_MANY_TARGETS
        assert mv.status is not Status.COUNTED

    with pytest.raises(MalformedLedgerError, match="line 1"):
        load_ledger(path)


# --------------------------------------------------------------------------- purge


def _config_dict() -> dict:
    return {
        "enabled": True,
        "persistence": "files",
        "slack": {"channel": "C0MAIN01"},
        "timezone": "UTC",
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-18"}],
        "rules": {
            "cooldown": {"minutes": 15, "scope": "pair", "rejected_attempts_reset": False},
            "multi_tag": "per_target",
            "max_targets_per_message": None,
            "edit_grace_minutes": 10,
            "max_snipes_per_target_per_day": None,
            "allow_self": False,
            "allow_bots": False,
            "count_thread_replies": False,
            "count_image_links": False,
            "allow_video": False,
            "selfie_bonus": False,
        },
        "players": {"count_intra_group": True, "groups": {},
                    "extras": [SNIPER, TARGET, TARGET2]},
        "consent": {
            "veto": {"emoji": "no_entry_sign", "by": ["admins"]},
            "optout_messages": [],
            "opted_out": [],
        },
        "admins": [ADMIN],
        "feedback": {
            "reactions": {
                "counted": "white_check_mark",
                "cooldown": "hourglass_flowing_sand",
                "untagged": None,
                "not_counted": "x",
                "selfie": None,
            },
            "review": {"min_targets": 5, "emoji": "question"},
        },
        "reports": [],
    }


def test_purge_leaves_the_erased_user_id_in_first_seen_and_edited_in_facts(
    tmp_path: Path, monkeypatch
):
    """Claim: `purge --user U` keeps every row where U is no longer a CURRENT target, so U's
    Slack ID stays in the live ledger (and, through the same predicate in the history
    rewrite, in every snapshot) whenever U was tagged at posting and the tag was later
    edited out (`first_seen_targets`, key 13) or was edited in and then removed again
    (`target_edited_in[].user`, key 15). `_cmd_purge` and `_names_user` both test only
    `sender == U or U in targets`.

    40 §4.2 `purge`: "genuine erasure" that "removes every row where the user is sender
    **or** target"; PLAN §6/§8: `purge --rewrite-history` is the answer to "a genuine
    erasure request". A user tagged at posting was a target of that message (00-data §2
    first-seen semantics: "a target here is tagged at posting"), and the ledger holds user
    IDs only, so the ID left in those keys is exactly the personal data the erasure had to
    remove."""
    monkeypatch.setattr(cli, "_now_us", lambda: us(2026, 9, 20, 12))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(_config_dict(), sort_keys=False), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    rows = [
        # TARGET2 tagged at posting, tag later edited out.
        _row(mkts(2026, 9, 10, 12), targets=(TARGET,), first_seen=(TARGET, TARGET2)),
        # TARGET2 edited in after posting, then edited out again.
        _row(mkts(2026, 9, 11, 12), targets=(TARGET,), first_seen=(TARGET,),
             edited_in=(TargetEdit(user=TARGET2, edit_ts=mkts(2026, 9, 11, 13)),)),
    ]
    save_ledger(data / "ledger.jsonl", rows)

    rc = main(["purge", "--user", TARGET2, "--yes",
               "--config", str(cfg), "--data-dir", str(data)])
    assert rc == int(Exit.OK)
    text = (data / "ledger.jsonl").read_bytes()
    assert TARGET2.encode("ascii") not in text
