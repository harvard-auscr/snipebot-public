"""The controls registry is a live positive-control gate (50 section 1.3).

For every control that carries a wired-up `patch`, the same tests must pass with
no break applied and turn red once the break is installed. Each control is driven
in its own `pytest` subprocess: the break is applied by `tests/_control_plugin.py`
(registered with `-p`) when `SNIPEBOT_CONTROL` names the control. A control whose
`patch` is still `None` records only the wave that will wire it up, so it is
skipped here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.controls.registry import CONTROLS, Control

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_PATCHED = sorted(name for name, c in CONTROLS.items() if c.patch is not None)


def _run(node_ids: tuple[str, ...], control: str | None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(PROJECT_ROOT), env.get("PYTHONPATH", "")) if p
    )
    env.pop("SNIPEBOT_CONTROL", None)
    if control is not None:
        env["SNIPEBOT_CONTROL"] = control
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "tests._control_plugin",
        "-p",
        "no:cacheprovider",
        "-q",
        *node_ids,
    ]
    return subprocess.run(
        cmd, cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True
    )


def test_registry_has_all_twelve_controls() -> None:
    # The twelve section 1.3 names, each with a layer and a non-empty red set.
    assert len(CONTROLS) == 12
    for name, control in CONTROLS.items():
        assert isinstance(control, Control)
        assert control.layer.startswith("L")
        assert control.expected_red, f"{name} names no expected-red tests"


def test_some_controls_are_wired() -> None:
    assert _PATCHED, "no control carries an executable patch"


@pytest.mark.parametrize("name", _PATCHED)
def test_control_baseline_green(name: str) -> None:
    # Without the break, the named tests pass.
    result = _run(CONTROLS[name].expected_red, control=None)
    assert result.returncode == 0, (
        f"{name}: expected-red tests failed WITHOUT the break\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("name", _PATCHED)
def test_control_turns_red(name: str) -> None:
    # With the break applied, the named tests turn red.
    result = _run(CONTROLS[name].expected_red, control=name)
    assert result.returncode != 0, (
        f"{name}: expected-red tests stayed green WITH the break applied\n"
        f"{result.stdout}\n{result.stderr}"
    )
