"""Wave 4, surface "time-periods", round 1.

Day/instant boundaries crossing the CLI: `rules bump --effective-from` and
`backfill --from`. Offline: `persistence: files`, a hand-built ledger in tmp_path,
FakeSlack only where a command insists on a Slack client.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.config import load_config
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import save_ledger
from snipebot.ts import US_PER_SECOND, parse_ts

from tests._helpers_export import cand
from tests.fake_slack import FakeSlack, FakeUser

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
ADMIN = "U0AAA009"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


def _config_dict(tz: str, rules) -> dict:
    return {
        "enabled": True,
        "persistence": "files",
        "slack": {"channel": CHANNEL},
        "timezone": tz,
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}],
        "rules": rules,
        "players": {"count_intra_group": True, "groups": {"fam": [SNIPER, TARGET]}, "extras": []},
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


_RULE_MAPPING = {
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
}


def _setup(tmp_path: Path, *, tz: str = "UTC", rules=None, row_ts: str) -> tuple[Path, Path]:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(_config_dict(tz, dict(_RULE_MAPPING) if rules is None else rules),
                       sort_keys=False),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    save_ledger(data / "ledger.jsonl", [cand(row_ts, SNIPER, (TARGET,))])
    return cfg, data


def _argv(cfg: Path, data: Path) -> list[str]:
    return ["--config", str(cfg), "--data-dir", str(data)]


def _freeze_now(monkeypatch, aware: datetime) -> None:
    """Pin `datetime.now(tz)` inside the CLI to one instant."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return aware.astimezone(tz) if tz is not None else aware.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", _Frozen)


def _last_effective_from_us(cfg: Path) -> int:
    return load_config(cfg).rules.entries[-1].effective_from_us


def test_first_rules_bump_from_mapping_form_trips_the_fingerprint_guard(tmp_path):
    """The shipped config uses the single-mapping `rules:` form (effective_from_us ==
    INT_MIN_TS). A correct, future-dated `rules bump --effective-from 2026-10-01` (H is
    2026-09-18) rewrites it into a list whose prior entry is dated at the first semester start
    (2026-09-01 00:00 local). That entry is <= H and its effective_from_us changed from
    INT_MIN_TS, so the rules fingerprint over rows <= H changes and the next sync refuses with
    exit 3 -- the documented unblock path always blocks. Violates 40 section 4.2 `rules bump`
    ("the rules fingerprint over rows <= H is unchanged and the guard passes") and 40 section
    1.5 rule 3 (the first entry applies to everything before the second)."""
    from snipebot.config import compute_fingerprints, fingerprint_guard

    row_ts = _ts(2026, 9, 18, 10, 0, 0)
    cfg, data = _setup(tmp_path, row_ts=row_ts)
    h_us = parse_ts(row_ts)
    stored = compute_fingerprints(load_config(cfg), h_us)
    rc = main(["rules", "bump", "--effective-from", "2026-10-01", *_argv(cfg, data)])
    assert rc == 0
    fingerprint_guard(load_config(cfg), [h_us], stored)   # must not raise


def test_rules_bump_from_mapping_form_leaves_pre_season_row_with_no_rule(tmp_path):
    """Under the mapping form every ledger row has a rule in force (INT_MIN_TS). The ledger is
    facts-only and keeps pre-season posts (a first sync scans `scan_days` back, and an
    OUT_OF_SEASON row is still a stored row). After a correct future-dated `rules bump`, the
    prior entry is dated at the first semester start, so `in_force_at` of a 2026-08-30 row
    raises NoRuleInForceError and every later sync exits 2 until config.yaml is hand-edited.
    Violates 40 section 4.2 `rules bump` (the currently in-force resolved rule is preserved as
    the prior entry) and 40 section 1.5 rules 3 and 5 (the first entry applies to everything
    before the second; NoRuleInForceError "cannot occur once rule 3 holds")."""
    from snipebot.rules import evaluate

    pre_season = _ts(2026, 8, 30, 12, 0, 0)
    row_ts = _ts(2026, 9, 18, 10, 0, 0)
    cfg, data = _setup(tmp_path, row_ts=row_ts)
    rows = [cand(pre_season, SNIPER, (TARGET,)), cand(row_ts, SNIPER, (TARGET,))]
    save_ledger(data / "ledger.jsonl", rows)
    c0 = load_config(cfg)
    evaluate(rows, c0.rules, c0.roster, set(), c0.semesters, c0.tz)   # fine before the bump
    rc = main(["rules", "bump", "--effective-from", "2026-10-01", *_argv(cfg, data)])
    assert rc == 0
    c1 = load_config(cfg)
    verdicts = evaluate(rows, c1.rules, c1.roster, set(), c1.semesters, c1.tz)
    assert len(verdicts) == 2


def test_rules_bump_now_writes_minute_floor_at_or_before_newest_row(tmp_path, monkeypatch):
    """`rules bump --effective-from now` checks the unrounded clock (11:59:50) against H but
    writes the minute-truncated wall clock ("11:59" = 11:59:00) to config.yaml. With the
    newest ledger row at 11:59:30 the written entry is effective BEFORE H, so the new entry
    joins the rules fingerprint over rows <= H and the next sync refuses (exit 3) -- the
    command meant to unblock a rule change breaks the scheduled job instead.
    Violates 40 section 4.2 `rules bump`: exit 2 if --effective-from is not strictly later
    than H as an exact instant; the new entry must be future-dated relative to H."""
    row_ts = _ts(2026, 9, 18, 11, 59, 30)
    cfg, data = _setup(tmp_path, row_ts=row_ts)
    _freeze_now(monkeypatch, datetime(2026, 9, 18, 11, 59, 50, tzinfo=timezone.utc))
    rc = main(["rules", "bump", "--effective-from", "now", *_argv(cfg, data)])
    if rc == 0:
        assert _last_effective_from_us(cfg) > parse_ts(row_ts), (
            "rules bump wrote an effective_from at or before the newest ledger row"
        )
    else:
        assert rc == int(Exit.CONFIG_INVALID)


def test_rules_bump_now_in_fall_back_second_hour_writes_an_hour_early(tmp_path, monkeypatch):
    """In the repeated fall-back hour, `datetime.now(tz)` is the fold=1 (later) instant, but
    `rules bump --effective-from now` writes the bare wall clock "2026-11-01 01:30", which the
    config resolves with fold=0 to the EARLIER instant, one hour before now. With H at
    01:00 EST (06:00 UTC) the check (now = 06:30 UTC > H) passes while the written entry
    (05:30 UTC) is before H, so the rules fingerprint over rows <= H changes and the next
    sync refuses. Violates 40 section 4.2 `rules bump` (the new entry must be strictly after
    H as an exact instant) given 00-data section 8's fold=0 resolution of wall clocks."""
    row_ts = _ts(2026, 11, 1, 6, 0, 0)          # 01:00 EST, the second 01:00 of the day
    cfg, data = _setup(tmp_path, tz="America/New_York", row_ts=row_ts)
    _freeze_now(monkeypatch, datetime(2026, 11, 1, 6, 30, 0, tzinfo=timezone.utc))
    rc = main(["rules", "bump", "--effective-from", "now", *_argv(cfg, data)])
    if rc == 0:
        assert _last_effective_from_us(cfg) > parse_ts(row_ts), (
            "rules bump wrote an effective_from at or before the newest ledger row"
        )
    else:
        assert rc == int(Exit.CONFIG_INVALID)


def test_rules_bump_effective_from_equal_to_newest_row_is_refused(tmp_path):
    """An explicit `--effective-from 2026-09-18 11:59` equal to H (a row at exactly
    11:59:00.000000) is accepted: the check is `eff_us < h_us`, not `<=`. The new entry is
    then in force AT H, joins the rules fingerprint over rows <= H, and the next sync refuses.
    Violates 40 section 4.2 `rules bump`: exit 2 if --effective-from is not STRICTLY later
    than H as an exact instant."""
    row_ts = _ts(2026, 9, 18, 11, 59, 0)
    cfg, data = _setup(tmp_path, row_ts=row_ts)
    before = cfg.read_text(encoding="utf-8")
    rc = main(["rules", "bump", "--effective-from", "2026-09-18 11:59", *_argv(cfg, data)])
    assert rc == int(Exit.CONFIG_INVALID)
    assert cfg.read_text(encoding="utf-8") == before


def test_rules_bump_before_a_future_dated_entry_writes_an_unloadable_config(tmp_path):
    """With a dated rules list that already holds a future entry (2026-12-01), a bump for
    2026-10-01 (later than H, so the only check passes) is APPENDED after the December entry.
    effective_from is then out of order, load_config raises RulesEffectiveFromError, and every
    scheduled sync exits 2 once the admin workflow commits the file. Violates 40 section 4.2
    `rules bump` (extends the dated-list form; exit 2 on a bad --effective-from) and the
    dated-list rule that each effective_from is strictly after the previous (40 section 2,
    RulesEffectiveFromError): the command must refuse or insert in order, never write a
    config that no longer loads."""
    rules = [
        dict(_RULE_MAPPING, effective_from="2026-09-01"),
        {"effective_from": "2026-12-01 00:00", "edit_grace_minutes": 5},
    ]
    row_ts = _ts(2026, 9, 18, 10, 0, 0)
    cfg, data = _setup(tmp_path, rules=rules, row_ts=row_ts)
    load_config(cfg)                                       # the starting config is valid
    before = cfg.read_text(encoding="utf-8")
    rc = main(["rules", "bump", "--effective-from", "2026-10-01", *_argv(cfg, data)])
    if rc == 0:
        load_config(cfg)                                   # must still load
    else:
        assert rc == int(Exit.CONFIG_INVALID)
        assert cfg.read_text(encoding="utf-8") == before


def test_backfill_from_ts_with_short_fraction_exits_config_invalid(tmp_path, monkeypatch):
    """`backfill --from 1789999999.5` (a ts with fewer than six fraction digits, e.g. hand-
    shortened) matches the CLI's loose `^\\d+\\.\\d{1,6}$` pre-check, so `_resolve_from` hands
    it to `parse_ts`, whose TsFormatError is not mapped: the run exits 1 ("unexpected error")
    instead of 2. Violates 40 section 4.4 (a malformed arg is CONFIG_INVALID = 2; exit 1 is
    reserved for uncaught errors) and 00-data section 1 (parse_ts accepts exactly six digits,
    so the CLI grammar must match it)."""
    row_ts = _ts(2026, 9, 18, 10, 0, 0)
    cfg, data = _setup(tmp_path, row_ts=row_ts)
    monkeypatch.setattr(cli, "_now_us", lambda: _secs(2026, 9, 18, 12) * US_PER_SECOND)
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2"),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9"),
    }
    slack = FakeSlack(now=_ts(2026, 9, 18, 12), bot_user_id=BOT, users=users)
    rc = main(
        ["backfill", "--from", "1789999999.5", "--dry-run", "--no-post", *_argv(cfg, data)],
        slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}),
    )
    assert rc == int(Exit.CONFIG_INVALID)
