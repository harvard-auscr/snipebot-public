"""Round 2 hostile-input breaks against snipebot.config.load_config.

Each test builds a config document by hand and asserts the spec-required
failure on a malformed / hostile value. A test that PASSES against the current
code is not a finding; every test here is written to FAIL on the code as it
stands, pinpointing one defect.

Spec references are to spec/40-config-cli.md and spec/00-data.md.
"""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from snipebot.config import (
    InvalidValueError,
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


def _load_dict(cfg: dict):
    """Write a config dict to a temp YAML file and load it."""
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "config.yaml")
    Path(path).write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return load_config(path)


def _load_text(text: str):
    """Write raw YAML text to a temp file and load it (for values a dict
    round-trip cannot express, e.g. a block scalar's trailing newline)."""
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "config.yaml")
    Path(path).write_text(text, encoding="utf-8")
    return load_config(path)


_BASE_TEXT = """\
enabled: true
slack:
  channel: C0MAINAA
timezone: America/New_York
semesters:
  - name: fall
    start: 2026-09-01
    end: 2026-12-20
rules: {}
players:
  extras: [U0AAA001]
consent:
  veto:
    emoji: x
feedback:
  reactions: {}
"""


# ---------------------------------------------------------------------------
# config-channel-id-trailing-newline-accepted
# ---------------------------------------------------------------------------

def test_channel_id_trailing_newline_accepted() -> None:
    """40-config-cli.md section 1 scalar table (ChannelID): "`ChannelID` |
    `^C[A-Z0-9]{6,}$` (public/private channel ID; never a name) |
    `InvalidValueError`", and the section 1 DECISION: "a typo (`c012...`
    lower-case, a name instead of an ID, `9:00` without a leading zero) must
    fail loudly at config load, not silently mis-route at runtime."

    A YAML block scalar gives `slack.channel` a trailing newline
    ('C0MAINAA\\n'). That value does not match `^C[A-Z0-9]{6,}$` as a whole and
    must raise InvalidValueError. `_channel` uses `re.match(pattern, value)`,
    whose `$` matches just before a terminal newline, so the newline-bearing ID
    is accepted verbatim and stored in `Config.channel`, exactly the silent
    mis-route the spec forbids.
    """
    text = _BASE_TEXT.replace(
        "  channel: C0MAINAA\n",
        "  channel: |\n    C0MAINAA\n",
    )
    with pytest.raises(InvalidValueError):
        _load_text(text)


# ---------------------------------------------------------------------------
# config-report-at-clock-trailing-newline-accepted
# ---------------------------------------------------------------------------

def test_report_at_clock_trailing_newline_accepted() -> None:
    """40-config-cli.md section 1 scalar table (Clock): "`Clock` | `HH:MM`,
    24-hour, `00:00`-`23:59` | `InvalidValueError`"; section 1.9 types a
    report's `at` as `Clock`. The section 1 DECISION requires such a typo to
    "fail loudly at config load, not silently mis-route at runtime".

    A block scalar gives `at` a trailing newline ('09:00\\n'). It does not match
    `^(?:[01]\\d|2[0-3]):[0-5]\\d$` as a whole and must raise InvalidValueError.
    The code's `_RE_CLOCK.match(at)` accepts it (its `$` matches before the
    terminal newline), then `int(part)` silently strips the newline off the
    minute, yielding at_hour=9 / at_minute=0 from a malformed value.
    """
    text = _BASE_TEXT + (
        "reports:\n"
        "  - name: r\n"
        "    every: 1d\n"
        "    at: |\n"
        "      09:00\n"
        "    sections: [day]\n"
    )
    with pytest.raises(InvalidValueError):
        _load_text(text)


# ---------------------------------------------------------------------------
# config-reports-null-coerced-to-empty
# ---------------------------------------------------------------------------

def test_reports_null_coerced_to_empty() -> None:
    """40-config-cli.md section 1.1: "`reports` | list | default `[]` | see
    section 1.9 | `InvalidValueError`", and section 1: "A key with the wrong
    scalar type raises `InvalidValueError`."

    A present-but-null `reports:` is not a list; it is a wrong-typed value and
    must raise InvalidValueError, exactly as the sibling list key `admins:`
    (section 1.1, same shape) does on null. Instead `load_config` calls
    `_resolve_reports(root.get("reports"))`, which returns `()` for a `None`
    argument -- it cannot tell an absent key from an explicit null -- so a
    null value is silently coerced to no reports.
    """
    cfg = _base()
    cfg["reports"] = None
    with pytest.raises(InvalidValueError):
        _load_dict(cfg)
