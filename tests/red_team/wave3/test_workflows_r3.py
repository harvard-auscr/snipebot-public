"""Wave-3 (INVARIANTS) red-team probes against the repo infra files:
README.md, the two Actions workflows, the Slack manifest, requirements*.txt,
pyproject.toml and .gitignore.

Each test in this module is a *breaker*: it encodes the behaviour the spec/plan
require and is expected to FAIL against the current tree, proving a defect. A
test that passes here is not a finding and must be deleted.

No network is touched; nothing here imports cv2 or the real Slack transport.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _readme_quickstart_snipebot_commands() -> list[list[str]]:
    """Every `python -m snipebot ...` invocation inside the README's fenced
    'Running it' code block, returned as the argv that follows `python -m
    snipebot` (so `python -m snipebot sync --dry-run` -> ['sync', '--dry-run']).
    `--help` invocations are skipped (they legitimately exit 0)."""
    text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```\n(.*?)```", text, re.S)
    cmds: list[list[str]] = []
    for block in blocks:
        for line in block.splitlines():
            line = line.strip()
            m = re.match(r"python -m snipebot\s+(.*)$", line)
            if not m:
                continue
            argv = m.group(1).split()
            if "--help" in argv or "-h" in argv:
                continue
            cmds.append(argv)
    return cmds


def test_readme_quickstart_commands_are_valid_cli():
    """spec/40-config-cli.md section 4.2 pins the `sync` flags exactly:
    "Flags | `--no-react` (skip step 7 reactions), `--no-post` (skip step 9
    digests), `--reevaluate` (section 3.2)". `--dry-run` is a `backfill`-only
    flag (section 4.2 backfill row: "`--dry-run` (write nothing)"). Every
    command the README hands a new operator must therefore be accepted by the
    real CLI parser; README.md line 40 documents `python -m snipebot sync
    --dry-run`, which the parser rejects with exit 2 ("unrecognized arguments:
    --dry-run"). Invariant: the documented quickstart is runnable."""
    from snipebot.cli import _build_parser

    commands = _readme_quickstart_snipebot_commands()
    # Guard: the README must actually document at least the two quickstart cmds,
    # or the extraction (not the code) is at fault.
    assert commands, "no `python -m snipebot` commands found in README quickstart"

    rejected: list[tuple[list[str], int]] = []
    for argv in commands:
        parser = _build_parser()
        try:
            parser.parse_args(argv)
        except SystemExit as exc:  # argparse exits 2 on an unknown flag
            code = exc.code if isinstance(exc.code, int) else 1
            if code != 0:
                rejected.append((argv, code))

    assert not rejected, (
        "README documents snipebot commands the CLI parser rejects "
        f"(spec section 4.2 gives `sync` no --dry-run): {rejected}"
    )
