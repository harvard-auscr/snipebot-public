"""E-W4-43: `reactions: false` is a config switch that stops step 7 for EVERY command.

Why: a deployment that wants pure stats can run its scheduled sync with --no-react, but
an admin `sync --reevaluate` has no such flag and would react on every message. A config
switch closes every path at once, whatever flags a workflow passes.
"""

from __future__ import annotations

import dataclasses

import pytest

from snipebot.config import ReactionsValueError, load_config
from snipebot.sync import Command

from tests.red_team.wave4.test_rulings_recaps import (
    ADMIN,
    BASE,
    SNIPE_TS,
    _bot_reactions,
    _config,
    _sync,
    _with,
    _world,
    _write,
)
from tests._helpers_sync import mkts


def _cfg(*, reactions: bool):
    return dataclasses.replace(_config(recaps=False), reactions=reactions)


def test_absent_key_defaults_to_on(tmp_path):
    assert load_config(_write(tmp_path, BASE)).reactions is True


@pytest.mark.parametrize("written,expected", [("true", True), ("false", False)])
def test_plain_booleans_load(tmp_path, written, expected):
    text = _with(BASE, "enabled: true", f"enabled: true\nreactions: {written}")
    assert load_config(_write(tmp_path, text)).reactions is expected


@pytest.mark.parametrize("written", ["yes", "off", "0", '"false"'])
def test_anything_but_true_or_false_is_refused(tmp_path, written):
    text = _with(BASE, "enabled: true", f"enabled: true\nreactions: {written}")
    with pytest.raises(ReactionsValueError, match="reactions"):
        load_config(_write(tmp_path, text))


def test_sync_with_reactions_off_adds_no_reaction_but_writes_the_ledger(tmp_path, capsys):
    slack = _world()
    result = _sync(slack, _cfg(reactions=False), tmp_path)
    assert result.exit_code == 0 and result.ledger_written
    assert _bot_reactions(slack) == []
    assert result.reactions_added == 0 and result.reactions_removed == 0
    assert "reactions skipped" in capsys.readouterr().err


@pytest.mark.parametrize("kw", [
    {"reevaluate": True},
    {"command": Command.VETO, "veto_ts": SNIPE_TS, "veto_by": ADMIN},
    {"command": Command.BACKFILL, "no_react": False},
])
def test_every_command_honours_reactions_off(tmp_path, kw):
    """The reacting run this ruling comes from was an admin `sync --reevaluate`."""
    slack = _world()
    _sync(slack, _cfg(reactions=False), tmp_path)
    later = mkts(2026, 9, 18, 21, 40)
    slack.as_of(later)
    result = _sync(slack, _cfg(reactions=False), tmp_path, now=later, **kw)
    assert result.exit_code == 0
    assert _bot_reactions(slack) == []


def test_reactions_on_is_the_positive_control(tmp_path):
    slack = _world()
    _sync(slack, _cfg(reactions=True), tmp_path)
    assert ("react", SNIPE_TS, "white_check_mark") in _bot_reactions(slack)
