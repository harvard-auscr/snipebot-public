"""Breaker tests for the `recaps` switch (E-W4-39; 40 §1.1, §4.2, §7.2; 20 §6.2 step 9).

Each test is one claimed defect in `snipebot recaps [on|off]`'s in-place edit of
config.yaml. Offline only: every config is written under tmp_path.
"""

from __future__ import annotations

import errno
import pathlib

import pytest

from snipebot.cli import Exit, main
from snipebot.config import load_config

from tests.red_team.wave4.test_rulings_recaps import BASE

ENABLED_LINE = "enabled: true                 # kill switch\n"


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)


def _write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / "config.yaml"
    path.write_bytes(text.encode("utf-8"))
    return path


def _recaps(cfg: pathlib.Path, state: str) -> int:
    return main(["--config", str(cfg), "recaps", state])


@pytest.mark.parametrize("header", ["---\n", "%YAML 1.1\n---\n"])
def test_off_on_a_config_with_a_document_header_and_no_enabled_line(tmp_path, header):
    """`recaps off` on a valid config that opens with a `---` document marker (or a
    `%YAML` directive) and has no top-level `enabled:` line inserts `recaps: false` at line
    0, above the header; the file then holds two documents, fails to load, and the switch
    is refused with exit 2 instead of turning recaps off."""
    text = header + BASE.replace(ENABLED_LINE, "")
    cfg = _write(tmp_path, text)
    assert load_config(cfg).recaps is True               # a valid config before the switch
    assert _recaps(cfg, "off") == Exit.OK
    assert load_config(cfg).recaps is False


@pytest.mark.parametrize("key, state", [
    ("recaps :", "off"), ("recaps :", "on"), ('"recaps":', "on"), ("'recaps':", "off"),
])
def test_switch_on_a_valid_spaced_or_quoted_recaps_key(tmp_path, key, state):
    """A top-level key written `recaps :` or `"recaps":` loads and is honoured by the
    config loader, but the rewrite only recognises the literal `recaps:` at column 0: `off`
    inserts a second key (duplicate-key refusal) and `on` leaves the old value in place
    ("did not take effect"), so a valid config cannot be switched without hand-editing."""
    current = "true" if state == "off" else "false"
    text = BASE.replace("persistence:", f"{key} {current}\npersistence:", 1)
    cfg = _write(tmp_path, text)
    assert load_config(cfg).recaps is (current == "true")
    assert _recaps(cfg, state) == Exit.OK
    assert load_config(cfg).recaps is (state == "on")


def test_a_failed_write_does_not_leave_a_truncated_config(tmp_path, monkeypatch):
    """The edit is written in place with `Path.write_bytes` (truncate, then write) rather
    than a temp file and an atomic rename as `rules bump` does; a write that fails part way
    (a full disk) leaves config.yaml truncated, and the OSError skips the rollback, so the
    live config no longer loads. The first write the command makes fails, whatever file it
    targets, so the check holds for an in-place write and for a temp file alike."""
    cfg = _write(tmp_path, BASE)
    original = cfg.read_bytes()
    real_write = pathlib.Path.write_bytes
    calls = {"n": 0}

    def _short_write(self, data):
        calls["n"] += 1
        if calls["n"] == 1:
            real_write(self, data[: len(data) // 3])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", _short_write)
    code = _recaps(cfg, "off")
    assert code != Exit.OK
    assert calls["n"] >= 1
    assert cfg.read_bytes() == original
