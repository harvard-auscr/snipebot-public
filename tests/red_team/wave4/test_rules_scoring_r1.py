"""Wave 4, rules-scoring, round 1: dated rules, cooldown, selfie scoring, aggregation.

Candidates are built by hand; configs are written to tmp_path and loaded through the
public loader. Offline and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from snipebot.config import load_config
from snipebot.parse import Candidate
from snipebot.rules import Reason, Status, evaluate

SNIPER = "U0AAA001"
TARGET = "U0AAA002"


def _cand(ts_str: str, sender: str = SNIPER, targets=(TARGET,), **over) -> Candidate:
    base = dict(
        ts=ts_str,
        sender=sender,
        subtype=None,
        thread_ts=None,
        targets=tuple(targets),
        live_images=1,
        live_image_ids=(),
        live_videos=0,
        linked_images=0,
        last_edit_ts=None,
        file_sigs=(),
        vetoes=(),
        missing_runs=0,
        first_seen_targets=frozenset(targets),
        first_sight_edited=False,
        target_edited_in=(),
    )
    base.update(over)
    return Candidate(**base)


def _base_doc() -> dict:
    return {
        "slack": {"channel": "C0MAIN01"},
        "timezone": "America/New_York",
        "semesters": [{"name": "fall", "start": "2026-09-01", "end": "2026-12-20"}],
        "rules": {"cooldown": {"minutes": 15}},
        "players": {"extras": [SNIPER, TARGET]},
        "consent": {"veto": {"emoji": "x"}},
        "feedback": {"reactions": {}},
    }


def test_dated_rules_first_entry_does_not_cover_pre_season_rows(tmp_path: Path):
    """00-data section 6 "Dated-rule resolution" and 40-config-cli section 1.5 rule 3/5:
    in the dated-list form "the first entry applies to everything before the second",
    and `NoRuleInForceError` "cannot occur once rule 3 holds". `config._resolve_rules`
    instead gives entries[0] the literal `effective_from_us` of rules[0] (which rule 3
    only bounds to be <= the first semester start), so `in_force_at` raises for any row
    older than it.

    Deploy path: the shipped example config uses the undated mapping form; the first
    sync (no watermark) fetches `now - scan_days`, so a deploy early in the season stores
    pre-season rows (only deleted rows are ever pruned). `snipebot rules bump` then
    rewrites the mapping into exactly the list below (rules[0].effective_from = the first
    semester start). From then on every sync (20 section 2 step 6 -> exit 2) and every
    report re-evaluates the whole ledger and dies with NoRuleInForceError: the bot stops
    for the rest of the semester. The pre-season row must simply be OUT_OF_SEASON.
    """
    doc = _base_doc()
    # What `rules bump --effective-from "2026-10-01 00:00"` writes from the mapping form.
    prior = dict(doc["rules"])
    prior["effective_from"] = "2026-09-01"
    doc["rules"] = [prior, {"effective_from": "2026-10-01 00:00", "cooldown": {"minutes": 30}}]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    config = load_config(path)

    # 2026-08-30 12:00 EDT (pre-season, stored by the first sync) and 2026-09-10 12:00 EDT.
    pre_season = _cand("1788105600.000100")
    in_season = _cand("1789056000.000200")

    verdicts = evaluate(
        [pre_season, in_season],
        config.rules,
        config.roster,
        frozenset(),
        config.semesters,
        config.tz,
    )
    by_ts = {v.ts: v for v in verdicts}
    assert by_ts[pre_season.ts].reason is Reason.OUT_OF_SEASON
    assert by_ts[in_season.ts].status is Status.COUNTED
