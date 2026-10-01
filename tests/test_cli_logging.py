"""`-v` scopes DEBUG to the program's own logger (40 section 4: logs carry IDs
only). Raising the root logger instead would make slack_sdk log every request and
response, headers and query parameters included.

Observed in a subprocess: under pytest the root logger already has handlers, so
`logging.basicConfig` is a no-op there and the levels could not be seen."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PROBE = (
    "import logging, sys\n"
    "from snipebot.cli import main\n"
    "main(['-v', 'report', '--by', 'day', '--config', 'does-not-exist.yaml'])\n"
    "print(logging.getLogger('slack_sdk').getEffectiveLevel(),"
    " logging.getLogger('snipebot').getEffectiveLevel(),"
    " logging.getLogger('snipebot.sync').getEffectiveLevel())\n"
)


def test_verbose_raises_only_the_program_logger() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    levels = proc.stdout.strip().split()
    assert levels == ["20", "10", "10"], (proc.stdout, proc.stderr[-500:])


def test_without_verbose_the_program_logger_stays_at_info() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.replace("'-v', ", "")],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    levels = proc.stdout.strip().split()
    assert levels == ["20", "20", "20"], (proc.stdout, proc.stderr[-500:])
