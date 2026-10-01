"""Wave 4 red-team, round 2, surface "doctor" (snipebot/doctor.py and its CLI
wiring).

Every test runs doctor offline against a tmp_path config and data dir. Each
test fails on the current code for the reason in its docstring.
"""

from __future__ import annotations

import types
from pathlib import Path

import yaml

import snipebot.cli as cli
import snipebot.doctor as doctor

_CHANNEL = "C0MAIN01"
_NOW_US = 1_800_000_000 * 1_000_000


def _cfg(**overrides) -> dict:
    base = {
        "slack": {"channel": _CHANNEL},
        "timezone": "America/New_York",
        "semesters": [{"name": "fall", "start": "2020-01-01", "end": "2035-12-20"}],
        "rules": {"selfie_bonus": False},
        "players": {"extras": ["U0AAA001", "U0AAA002"]},
        "consent": {"veto": {"emoji": "x"}},
        "admins": ["U0AAA009"],
        "feedback": {"reactions": {}},
    }
    base.update(overrides)
    return base


def _write_config(tmp_path: Path, cfg: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 1. DOC-RULES-RESOLVE crashes on a first dated rule before 1970 (Windows).
# --------------------------------------------------------------------------- #

def test_doc_rules_resolve_crashes_on_a_pre_1970_first_effective_from(
    tmp_path: Path, capsys,
) -> None:
    """40-config-cli.md section 5.1 DOC-RULES-RESOLVE: 'dated rules resolve with
    a first entry <= the first semester start; detail is <N> dated rule(s),
    first effective <YYYY-MM-DD>'. A first entry dated before 1970 (an owner's
    'since always' date such as 1900-01-01) is valid and load_config accepts it,
    but _check_rules_resolve formats it with datetime.fromtimestamp on a
    negative POSIX second, which raises OSError [Errno 22] on Windows -- the box
    where the owner runs the L8 go-live preflight. doctor.run raises instead of
    printing the check, so the CLI prints 'unexpected error' and exits 1, and
    every other doctor check is lost. (On Linux the same call succeeds.)"""
    cfg = _cfg(rules=[{"effective_from": "1900-01-01", "selfie_bonus": False}])
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    args = types.SimpleNamespace(
        config=str(config_path), data_dir=str(data_dir), offline=True, json=False,
    )

    code = doctor.run(args, now_us=_NOW_US)
    out = capsys.readouterr().out

    line = next(line for line in out.splitlines() if line.startswith("DOC-RULES-RESOLVE "))
    assert line == "DOC-RULES-RESOLVE PASS", line
    assert int(code) == int(cli.Exit.OK), out
