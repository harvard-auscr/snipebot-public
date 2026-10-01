"""A pytest plugin that installs one control's break for a subprocess run.

Registered with `-p tests._control_plugin`. When the environment names a control
in `SNIPEBOT_CONTROL`, its `patch` (from `tests.controls.registry`) is applied to
a session-scoped `MonkeyPatch` at configure time and undone at unconfigure. With
no env var, or a control whose patch is not yet wired up, the plugin is inert.
"""

from __future__ import annotations

import os

from _pytest.monkeypatch import MonkeyPatch

from tests.controls.registry import CONTROLS

_ENV = "SNIPEBOT_CONTROL"
_ATTR = "_snipebot_control_mp"


def pytest_configure(config) -> None:  # type: ignore[no-untyped-def]
    name = os.environ.get(_ENV)
    if not name:
        return
    control = CONTROLS[name]
    if control.patch is None:
        return
    mp = MonkeyPatch()
    control.patch(mp)
    setattr(config, _ATTR, mp)


def pytest_unconfigure(config) -> None:  # type: ignore[no-untyped-def]
    mp = getattr(config, _ATTR, None)
    if mp is not None:
        mp.undo()
        setattr(config, _ATTR, None)
