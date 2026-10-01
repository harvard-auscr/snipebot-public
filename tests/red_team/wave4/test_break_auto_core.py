"""Breaker tests on the automatic roster in the scoring core (E-W4-42): `rules.evaluate`,
`config` validation and the `players` fingerprint / guard. Each test is one defect; its
docstring is the claim. Offline only: config files under tmp_path, no Slack.
"""

from __future__ import annotations

import calendar
from pathlib import Path

import pytest
import yaml

from snipebot.config import (
    FingerprintGuardError,
    compute_fingerprints,
    fingerprint_guard,
    load_config,
)
from snipebot.parse import Candidate
from snipebot.rules import Reason, evaluate
from snipebot.ts import parse_ts

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SIB1 = "U0AAA001"      # grouped
SIB2 = "U0AAA002"      # grouped
LOOSE = "U0AAA004"     # an ungrouped human who has been playing under auto
ADMIN = "U0AAA009"

BOTS_MAP = {BOT: True, SIB1: False, SIB2: False, LOOSE: False, ADMIN: False}


def _ts(y, mo, d, h=0, mi=0, s=0) -> str:
    return f"{calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))}.000000"


def _config_dict(groups: dict) -> dict:
    return {
        "enabled": True,
        "persistence": "files",
        "slack": {"channel": CHANNEL},
        "timezone": "UTC",
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}],
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
        "players": {"mode": "auto", "count_intra_group": True, "groups": groups},
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


def _load(tmp_path: Path, name: str, groups: dict):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(_config_dict(groups), sort_keys=False), encoding="utf-8")
    return load_config(path, is_bot=BOTS_MAP)


def _cand(ts: str, sender: str, *targets: str) -> Candidate:
    return Candidate(
        ts=ts, sender=sender, subtype=None, thread_ts=None, targets=tuple(targets),
        live_images=1, live_image_ids=(f"F{ts}",), live_videos=0, linked_images=0,
        last_edit_ts=None, file_sigs=(), vetoes=(), missing_runs=0,
        first_seen_targets=frozenset(targets), first_sight_edited=False,
        target_edited_in=(),
    )


def _judge(config, facts):
    return [
        (mv.ts, mv.status, mv.reason, tuple((p.target, p.status, p.reason) for p in mv.pairs))
        for mv in evaluate(facts, config.rules, config.roster, frozenset(),
                           config.semesters, config.tz)
    ]


def test_future_dated_grouping_of_an_active_auto_player_rejudges_past_rows_unguarded(tmp_path):
    """Under players.mode auto, adding an already-playing ungrouped human to a group with a
    `from:` dated after H re-judges existing rows (their earlier snipes, sent and received,
    flip from COUNTED to SENDER_OFF_ROSTER / TARGET_OFF_ROSTER) while the `players`
    fingerprint excludes the entry (join_us > H) and the guard passes, so the next sync
    silently wipes that person's past points. 00 section 7: the guard trips whenever a
    change would re-judge existing rows; under auto a dated grouping is not future-only."""
    facts = [
        _cand(_ts(2026, 9, 10, 9), LOOSE, SIB1),
        _cand(_ts(2026, 9, 11, 9), SIB2, LOOSE),
    ]
    h_us = max(parse_ts(c.ts) for c in facts)
    before = _load(tmp_path, "before.yaml", {"sib": [SIB1, SIB2]})
    after = _load(tmp_path, "after.yaml",
                  {"sib": [SIB1, SIB2, {"id": LOOSE, "from": "2026-10-01"}]})
    assert after.roster.entries[LOOSE].join_us > h_us   # a purely "future" dated addition

    rejudged = _judge(before, facts) != _judge(after, facts)
    stored = compute_fingerprints(before, h_us)
    if rejudged:
        with pytest.raises(FingerprintGuardError):
            fingerprint_guard(after, [h_us], stored, h_us)


def test_auto_grouped_human_is_never_off_roster_before_their_join(tmp_path):
    """Under players.mode auto a user is a player at ANY ts iff they are not a bot and not
    USLACKBOT, and SENDER_OFF_ROSTER / TARGET_OFF_ROSTER never fire for humans (owner
    design E-W4-42); a dated `from:` should only date their group membership. Yet a human
    grouped with a later `from:` is judged off-roster for every snipe before that date,
    on both sides, although the same person ungrouped would have counted."""
    cfg = _load(tmp_path, "c.yaml",
                {"sib": [SIB1, SIB2, {"id": LOOSE, "from": "2026-09-20"}]})
    sent, received = _ts(2026, 9, 10, 9), _ts(2026, 9, 11, 9)
    got = {row[0]: row for row in _judge(cfg, [_cand(sent, LOOSE, SIB1),
                                               _cand(received, SIB2, LOOSE)])}
    assert got[sent][2] is not Reason.SENDER_OFF_ROSTER
    assert got[received][3][0][2] is not Reason.TARGET_OFF_ROSTER
