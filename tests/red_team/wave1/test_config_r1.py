"""Round 1 spec-conformance breaks against snipebot.config.load_config.

Each test builds a config document by hand and asserts the spec-required
failure. A test that PASSES against the current code is not a finding; every
test here is written to FAIL on the code as it stands, pinpointing one defect.

Spec references are to spec/40-config-cli.md and spec/00-data.md.
"""

from __future__ import annotations

import copy
import datetime
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from snipebot.config import (
    InvalidValueError,
    UnknownKeyError,
    load_config,
)


# ---------------------------------------------------------------------------
# Helpers (copied in, per the round rules; independent of tests/conftest.py).
# ---------------------------------------------------------------------------

def _base() -> dict:
    """A minimal, valid config document (deep-copied per call)."""
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


def _load(cfg: dict):
    """Write cfg to a temp YAML file and load it."""
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "config.yaml")
    Path(path).write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return load_config(path)


# ---------------------------------------------------------------------------
# consent-optout-messages-wrong-type-accepted
# ---------------------------------------------------------------------------

def test_optout_messages_mapping_silently_accepted() -> None:
    """40-config-cli.md section 1.7: `consent.optout_messages` is
    `list<SlackTs>`, and section 1 states 'A key with the wrong scalar type
    raises InvalidValueError.'

    A mapping is not a list; `consent.optout_messages: {}` must raise
    InvalidValueError. The code evaluates `raw.get("optout_messages", []) or []`,
    so any falsy non-list value (an empty mapping, an empty string, 0, false) is
    silently coerced to an empty list and accepted.
    """
    cfg = _base()
    cfg["consent"]["optout_messages"] = {}
    with pytest.raises(InvalidValueError):
        _load(cfg)


# ---------------------------------------------------------------------------
# consent-opted-out-wrong-type-accepted
# ---------------------------------------------------------------------------

def test_opted_out_mapping_silently_accepted() -> None:
    """40-config-cli.md section 1.7: `consent.opted_out` is `list<UserID>`, and
    section 1 states 'A key with the wrong scalar type raises InvalidValueError.'

    A mapping is not a list; `consent.opted_out: {}` must raise
    InvalidValueError. The code evaluates `raw.get("opted_out", []) or []`, so a
    falsy non-list value is silently coerced to an empty list and accepted.
    """
    cfg = _base()
    cfg["consent"]["opted_out"] = {}
    with pytest.raises(InvalidValueError):
        _load(cfg)


# ---------------------------------------------------------------------------
# rules-bare-mapping-effective-from-not-rejected
# ---------------------------------------------------------------------------

def test_bare_rules_mapping_effective_from_not_rejected() -> None:
    """40-config-cli.md section 1: 'Unknown keys are rejected at every level
    (top level, each section, and inside each dated rules entry) with
    UnknownKeyError.' Section 1.5: 'Either one mapping of the keys below, or a
    list of entries, each of which is that mapping plus a required
    effective_from.'

    `effective_from` is defined only for the dated-list form; in the single
    (undated) mapping form it is not one of 'the keys below', so it is an unknown
    key and must raise UnknownKeyError. The code shares one allowed-key set
    (`_RULE_KEYS | {"effective_from"}`) for both forms, so a bare mapping
    carrying `effective_from` is accepted and the date silently ignored.
    """
    cfg = _base()
    cfg["rules"] = {"effective_from": "2026-10-01", "cooldown": {"minutes": 30}}
    with pytest.raises(UnknownKeyError):
        _load(cfg)


# ---------------------------------------------------------------------------
# datetime-seconds-silently-truncated
# ---------------------------------------------------------------------------

def test_member_from_with_seconds_silently_truncated() -> None:
    """40-config-cli.md section 1 scalar table: `DateTime` is `YYYY-MM-DD` or
    `YYYY-MM-DD HH:MM` / `YYYY-MM-DDTHH:MM`, violation -> InvalidValueError.

    The format is minute precision. A `from:` value carrying seconds is out of
    format and must raise InvalidValueError; the string spelling
    ('2026-09-05 12:30:45') does raise. But a YAML native timestamp with seconds
    reaches `_parse_datetime`'s datetime branch, which drops the seconds without
    error, so the identical value silently truncates to the minute instead of
    being rejected -- inconsistent with the string form and with the format.
    """
    cfg = _base()
    cfg["players"] = {
        "extras": [{"id": "U0AAA001", "from": datetime.datetime(2026, 9, 5, 12, 30, 45)}]
    }
    with pytest.raises(InvalidValueError):
        _load(cfg)
