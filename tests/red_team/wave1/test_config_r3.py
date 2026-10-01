"""Round 3 red-team: config invariants and cross-key consistency.

Each test builds a raw config document by hand and drives it through
``load_config``. A test asserts the specification-mandated behaviour; it is a
finding only while it FAILS against the current code.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from snipebot.config import InvalidValueError, load_config

_MINIMAL = """\
slack: {channel: C0MAINAA}
timezone: America/New_York
semesters: [{name: fall, start: 2026-09-01, end: 2026-12-20}]
rules: {}
players: {extras: [U0AAA001]}
consent: {veto: {emoji: x}}
feedback: {reactions: {}}
"""


def _write(text: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_faces_null_rejected_as_wrong_type() -> None:
    """40-config-cli.md section 1.1 (lines 50-64) types ``faces`` as a
    ``mapping`` whose Error column is ``InvalidValueError``, and the section
    intro (line 27) pins the invariant that binds every key uniformly: "A key
    with the wrong scalar type raises `InvalidValueError`." A present
    ``faces: null`` is a null scalar where a mapping is required, so it must
    raise. Every other top-level value behaves this way -- ``admins: null``,
    ``reports: null``, ``enabled: null`` and ``persistence: null`` all raise --
    but ``faces: null`` is silently coerced to an all-default ``FacesConfig``
    (config.py:911-913, ``if raw is None: raw = {}``), breaking the
    across-keys uniformity of null handling.
    """
    path = _write(_MINIMAL + "faces: null\n")
    with pytest.raises(InvalidValueError):
        load_config(path)
