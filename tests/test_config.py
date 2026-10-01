"""Config load/validation tests (test-matrix L8 rows)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from snipebot.config import (
    Cadence,
    Config,
    DuplicateGroupMemberError,
    DuplicateNameError,
    EmojiCollisionError,
    EmptyGroupError,
    EmptyValueError,
    FingerprintGuardError,
    InvalidValueError,
    MissingRequiredKeyError,
    OverlappingSemestersError,
    Persistence,
    ReviewFlag,
    RulesEffectiveFromError,
    Scope,
    Section,
    UnknownKeyError,
    VetoActor,
    Weekday,
    compute_fingerprints,
    fingerprint_guard,
    load_config,
)

_DAY_US = 86_400_000_000


def _base() -> dict:
    """A minimal, valid config as a plain dict (deep-copied per call)."""
    return copy.deepcopy(
        {
            "slack": {"channel": "C0MAINAA"},
            "timezone": "America/New_York",
            "semesters": [{"name": "fall", "start": "2026-09-01", "end": "2026-12-20"}],
            "rules": {},
            "players": {"extras": ["U0AAA001"]},
            "consent": {"veto": {"emoji": "x"}},
            "feedback": {"reactions": {}},
        }
    )


def _write(tmp_path: Path, cfg: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def _load(tmp_path: Path, cfg: dict, **kwargs) -> Config:
    return load_config(_write(tmp_path, cfg), **kwargs)


# --------------------------------------------------------------------------
# The example config is a real, loadable document.
# --------------------------------------------------------------------------

def test_example_config_loads() -> None:
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = load_config(example)
    assert isinstance(config, Config)
    assert config.channel == "C0MAINAA"
    assert config.persistence is Persistence.GIT
    assert config.faces.score_threshold == "0.9"
    assert isinstance(config.faces.score_threshold, str)


def test_faces_score_threshold_range(tmp_path: Path) -> None:
    """faces.score_threshold must lie in [0.5, 1.0]; anything else is InvalidValueError
    naming faces.score_threshold (E12)."""
    for good in ("0.5", "0.9", "1.0"):
        cfg = _base()
        cfg["faces"] = {"score_threshold": good}
        assert _load(tmp_path, cfg).faces.score_threshold == good
    for bad in ("0.0", "0.49"):
        cfg = _base()
        cfg["faces"] = {"score_threshold": bad}
        with pytest.raises(InvalidValueError) as excinfo:
            _load(tmp_path, cfg)
        assert "faces.score_threshold" in str(excinfo.value)


# --------------------------------------------------------------------------
# L8-PF-DOC-CONFIG-PARSE: every section-1 validation error fires on its input.
# Positive control: a clean base config loads and unknown keys are refused.
# --------------------------------------------------------------------------

def test_L8_PF_DOC_CONFIG_PARSE(tmp_path: Path) -> None:
    # positive control: the base loads.
    assert isinstance(_load(tmp_path, _base()), Config)

    # UnknownKeyError — top level and inside a section.
    cfg = _base()
    cfg["bogus"] = 1
    with pytest.raises(UnknownKeyError):
        _load(tmp_path, cfg)
    cfg = _base()
    cfg["rules"] = {"nope": 1}
    with pytest.raises(UnknownKeyError):
        _load(tmp_path, cfg)

    # MissingRequiredKeyError — a whole required section, and a required leaf.
    cfg = _base()
    del cfg["slack"]
    with pytest.raises(MissingRequiredKeyError):
        _load(tmp_path, cfg)
    cfg = _base()
    cfg["slack"] = {}
    with pytest.raises(MissingRequiredKeyError):
        _load(tmp_path, cfg)

    # InvalidValueError — bad zone, bad channel, bad user, out-of-range int.
    cfg = _base()
    cfg["timezone"] = "Not/AZone"
    with pytest.raises(InvalidValueError):
        _load(tmp_path, cfg)
    cfg = _base()
    cfg["slack"]["channel"] = "c0main"
    with pytest.raises(InvalidValueError):
        _load(tmp_path, cfg)
    cfg = _base()
    cfg["players"] = {"extras": ["not-a-user"]}
    with pytest.raises(InvalidValueError):
        _load(tmp_path, cfg)
    cfg = _base()
    cfg["sync"] = {"interval_minutes": 0}
    with pytest.raises(InvalidValueError):
        _load(tmp_path, cfg)

    # EmptyValueError — empty required list.
    cfg = _base()
    cfg["semesters"] = []
    with pytest.raises(EmptyValueError):
        _load(tmp_path, cfg)

    # DuplicateNameError — two semesters share a name.
    cfg = _base()
    cfg["semesters"] = [
        {"name": "fall", "start": "2026-09-01", "end": "2026-10-01"},
        {"name": "fall", "start": "2026-10-02", "end": "2026-11-01"},
    ]
    with pytest.raises(DuplicateNameError):
        _load(tmp_path, cfg)

    # DuplicateGroupMemberError — one user in two groups.
    cfg = _base()
    cfg["players"] = {"groups": {"a": ["U0AAA001"], "b": ["U0AAA001"]}}
    with pytest.raises(DuplicateGroupMemberError):
        _load(tmp_path, cfg)

    # EmptyGroupError — a group with no members.
    cfg = _base()
    cfg["players"] = {"groups": {"a": []}}
    with pytest.raises(EmptyGroupError):
        _load(tmp_path, cfg)

    # OverlappingSemestersError — two closed intervals overlap.
    cfg = _base()
    cfg["semesters"] = [
        {"name": "fall", "start": "2026-09-01", "end": "2026-10-15"},
        {"name": "winter", "start": "2026-10-10", "end": "2026-11-01"},
    ]
    with pytest.raises(OverlappingSemestersError):
        _load(tmp_path, cfg)

    # RulesEffectiveFromError — first dated entry after the first semester start.
    cfg = _base()
    cfg["rules"] = [{"effective_from": "2026-09-05", "cooldown": {"minutes": 15}}]
    with pytest.raises(RulesEffectiveFromError):
        _load(tmp_path, cfg)

    # BadEmojiError — an emoji name that fails the pattern.
    from snipebot.config import BadEmojiError

    cfg = _base()
    cfg["consent"]["veto"]["emoji"] = "X!"
    with pytest.raises(BadEmojiError):
        _load(tmp_path, cfg)

    # EmojiCollisionError — the veto emoji equals a feedback emoji.
    cfg = _base()
    cfg["feedback"] = {"reactions": {"counted": "x"}}
    with pytest.raises(EmojiCollisionError):
        _load(tmp_path, cfg)


# --------------------------------------------------------------------------
# L8-PF-DOC-RULES-RESOLVE: dated rules resolve; first entry <= first semester
# start; later entries patch. Positive control: first entry after the semester
# start is refused.
# --------------------------------------------------------------------------

def test_L8_PF_DOC_RULES_RESOLVE(tmp_path: Path) -> None:
    cfg = _base()
    cfg["rules"] = [
        {"effective_from": "2026-09-01", "cooldown": {"minutes": 15, "scope": "target"}},
        {"effective_from": "2026-10-01", "cooldown": {"minutes": 30}},
    ]
    config = _load(tmp_path, cfg)
    entries = config.rules.entries
    assert len(entries) == 2
    # sorted ascending by effective_from_us
    assert entries[0].effective_from_us < entries[1].effective_from_us
    # first entry keeps its explicit scope
    assert entries[0].cooldown.scope is Scope.TARGET
    assert entries[0].cooldown.microseconds == 15 * 60_000_000
    # later entry patches cooldown.minutes but inherits the scope key-by-key
    assert entries[1].cooldown.microseconds == 30 * 60_000_000
    assert entries[1].cooldown.scope is Scope.TARGET

    # in_force_at selects the last entry <= the ts.
    first_us = entries[0].effective_from_us
    second_us = entries[1].effective_from_us
    assert config.rules.in_force_at(first_us) is entries[0]
    assert config.rules.in_force_at(second_us - 1) is entries[0]
    assert config.rules.in_force_at(second_us) is entries[1]

    # a bare mapping resolves to one entry anchored at INT_MIN_TS.
    cfg = _base()
    cfg["rules"] = {"cooldown": {"minutes": 20}}
    solo = _load(tmp_path, cfg).rules
    assert len(solo.entries) == 1
    assert solo.entries[0].cooldown.microseconds == 20 * 60_000_000

    # non-monotonic dated entries are refused.
    cfg = _base()
    cfg["rules"] = [
        {"effective_from": "2026-09-01"},
        {"effective_from": "2026-09-01"},
    ]
    with pytest.raises(RulesEffectiveFromError):
        _load(tmp_path, cfg)

    # positive control: a first dated entry after the semester start is refused.
    cfg = _base()
    cfg["rules"] = [{"effective_from": "2026-09-02"}]
    with pytest.raises(RulesEffectiveFromError):
        _load(tmp_path, cfg)


# --------------------------------------------------------------------------
# L8-PF-review-config: review defaults, nullables, collisions, persistence,
# veto.by default, max_targets cap.
# --------------------------------------------------------------------------

def test_L8_PF_review_config(tmp_path: Path) -> None:
    # review absent -> ReviewFlag(5, "question")
    config = _load(tmp_path, _base())
    assert config.review == ReviewFlag(5, "question")
    # veto.by defaults to [admins]
    assert config.consent.veto_by == (VetoActor.ADMINS,)
    # persistence defaults to git
    assert config.persistence is Persistence.GIT

    # min_targets: null and emoji: null accepted
    cfg = _base()
    cfg["feedback"] = {"review": {"min_targets": None, "emoji": None}}
    config = _load(tmp_path, cfg)
    assert config.review == ReviewFlag(None, None)

    # review.emoji equal to the veto emoji -> EmojiCollisionError
    cfg = _base()
    cfg["feedback"] = {"review": {"emoji": "x"}}
    with pytest.raises(EmojiCollisionError):
        _load(tmp_path, cfg)

    # review.emoji equal to a status emoji -> EmojiCollisionError
    cfg = _base()
    cfg["feedback"] = {"review": {"emoji": "white_check_mark"}}
    with pytest.raises(EmojiCollisionError):
        _load(tmp_path, cfg)

    # max_targets_per_message: null -> no cap
    cfg = _base()
    cfg["rules"] = {"max_targets_per_message": None}
    assert _load(tmp_path, cfg).rules.entries[0].max_targets_per_message is None

    # max_targets_per_message: 0 -> InvalidValueError
    cfg = _base()
    cfg["rules"] = {"max_targets_per_message": 0}
    with pytest.raises(InvalidValueError):
        _load(tmp_path, cfg)

    # persistence: any other string -> InvalidValueError
    cfg = _base()
    cfg["persistence"] = "sqlite"
    with pytest.raises(InvalidValueError):
        _load(tmp_path, cfg)

    # a selfie emoji equal to a status emoji is also refused (award invisible).
    cfg = _base()
    cfg["feedback"] = {"reactions": {"selfie": "white_check_mark"}}
    with pytest.raises(EmojiCollisionError):
        _load(tmp_path, cfg)


# --------------------------------------------------------------------------
# L8-PF-fingerprint-scope: a future-dated rule/roster addition (> H), or a
# purely future semester, does not trip the guard; a past-affecting edit does.
# Positive control: the past edit both changes the fingerprint and refuses.
# --------------------------------------------------------------------------

def _scope_base() -> dict:
    cfg = _base()
    cfg["semesters"] = [{"name": "fall", "start": "2026-09-01", "end": "2026-12-20"}]
    cfg["rules"] = [{"effective_from": "2026-09-01", "cooldown": {"minutes": 15}}]
    cfg["players"] = {"groups": {"reds": ["U0AAA001", "U0AAA002"]}, "extras": ["U0BBB001"]}
    return cfg


def test_L8_PF_fingerprint_scope(tmp_path: Path) -> None:
    base = _load(tmp_path, _scope_base())
    # H sits mid-semester, ~30 days after the first semester start.
    h_us = base.semesters[0].start_us + 30 * _DAY_US
    stored = compute_fingerprints(base, h_us)

    # --- rules: a future-dated entry (> H) does not change the fingerprint.
    cfg = _scope_base()
    cfg["rules"] = [
        {"effective_from": "2026-09-01", "cooldown": {"minutes": 15}},
        {"effective_from": "2026-12-01", "cooldown": {"minutes": 30}},
    ]
    future_rule = _load(tmp_path, cfg)
    assert compute_fingerprints(future_rule, h_us)["rules"] == stored["rules"]

    # a past-affecting edit (an in-force entry) does change it.
    cfg = _scope_base()
    cfg["rules"] = [{"effective_from": "2026-09-01", "cooldown": {"minutes": 99}}]
    past_rule = _load(tmp_path, cfg)
    assert compute_fingerprints(past_rule, h_us)["rules"] != stored["rules"]

    # --- players: a future-dated addition (> H) does not change the fingerprint.
    cfg = _scope_base()
    cfg["players"]["extras"] = ["U0BBB001", {"id": "U0CCC001", "from": "2026-12-05"}]
    future_roster = _load(tmp_path, cfg)
    assert compute_fingerprints(future_roster, h_us)["players"] == stored["players"]

    # a past-dated addition (<= H) does change it.
    cfg = _scope_base()
    cfg["players"]["extras"] = ["U0BBB001", {"id": "U0CCC001", "from": "2026-09-05"}]
    past_roster = _load(tmp_path, cfg)
    assert compute_fingerprints(past_roster, h_us)["players"] != stored["players"]

    # --- semesters: a purely future semester does not change the fingerprint.
    cfg = _scope_base()
    cfg["semesters"] = [
        {"name": "fall", "start": "2026-09-01", "end": "2026-12-20"},
        {"name": "spring", "start": "2027-01-05", "end": "2027-05-01"},
    ]
    future_sem = _load(tmp_path, cfg)
    assert compute_fingerprints(future_sem, h_us)["semesters"] == stored["semesters"]

    # moving an in-range semester's dates changes it.
    cfg = _scope_base()
    cfg["semesters"] = [{"name": "fall", "start": "2026-09-02", "end": "2026-12-20"}]
    past_sem = _load(tmp_path, cfg)
    assert compute_fingerprints(past_sem, h_us)["semesters"] != stored["semesters"]

    # --- the guard: future additions pass, a past edit refuses (exit 3).
    row_ts = [h_us]
    fingerprint_guard(future_rule, row_ts, stored)      # no raise
    fingerprint_guard(future_roster, row_ts, stored)    # no raise
    fingerprint_guard(future_sem, row_ts, stored)       # no raise
    with pytest.raises(FingerprintGuardError):
        fingerprint_guard(past_rule, row_ts, stored)

    # first run (empty stored / empty ledger) never trips.
    fingerprint_guard(past_rule, row_ts, {})
    fingerprint_guard(past_rule, [], stored)


# --------------------------------------------------------------------------
# A weekly report needs its weekday and its week section; a daily one must not
# carry a week section. (section 1.9 period/cadence matching.)
# --------------------------------------------------------------------------

def test_report_period_matches_cadence(tmp_path: Path) -> None:
    cfg = _base()
    cfg["reports"] = [
        {
            "name": "weekly",
            "every": "1w",
            "at": "09:00",
            "weekday": "mon",
            "sections": ["week", "top_snipers"],
        }
    ]
    config = _load(tmp_path, cfg)
    spec = config.reports[0]
    assert spec.cadence is Cadence.WEEKLY
    assert spec.weekday is Weekday.MON
    assert spec.at_hour == 9 and spec.at_minute == 0
    assert Section.WEEK in spec.sections

    # a daily report carrying a week section is refused.
    cfg = _base()
    cfg["reports"] = [
        {"name": "daily", "every": "1d", "at": "09:00", "sections": ["week"]}
    ]
    with pytest.raises(InvalidValueError):
        _load(tmp_path, cfg)

    # a weekly report missing its weekday is refused.
    cfg = _base()
    cfg["reports"] = [
        {"name": "weekly", "every": "1w", "at": "09:00", "sections": ["week"]}
    ]
    with pytest.raises(InvalidValueError):
        _load(tmp_path, cfg)

    # an empty sections list is refused.
    cfg = _base()
    cfg["reports"] = [{"name": "d", "every": "1d", "at": "09:00", "sections": []}]
    with pytest.raises(EmptyValueError):
        _load(tmp_path, cfg)
