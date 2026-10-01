"""Wave 4 red-team, round 1, surface "doctor" (snipebot/doctor.py and the
transport seams it reads).

Each test drives ``doctor.run`` with a tmp_path config/data dir and an offline
Slack double, and asserts a rule from spec/40-config-cli.md section 5 or a
real Slack API behaviour that the existing doubles do not model. Every test
fails on the current code for the reason in its docstring. No network.
"""

from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import yaml

import snipebot.doctor as doctor
from snipebot.slack_io import _error_for
from tests.controls import slack_fixtures
from tests.fake_slack import FakeSlack, FakeUser

_NOW = "1800000000.000000"  # safely inside every test semester
_CHANNEL = "C0MAIN01"
_BOT = "U0BOT01"

def _ts_us(ts: str) -> int:
    from snipebot.ts import parse_ts

    return parse_ts(ts)


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


def _args(config_path: Path, data_dir: Path, *, offline: bool = True):
    return types.SimpleNamespace(
        config=str(config_path), data_dir=str(data_dir), offline=offline, json=False,
    )


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def _find(lines: list[str], check_id: str) -> str:
    for line in lines:
        if line.startswith(check_id + " "):
            return line
    raise AssertionError(f"{check_id} not printed; got:\n" + "\n".join(lines))


def _users() -> dict[str, FakeUser]:
    return {
        "U0AAA001": FakeUser(id="U0AAA001"),
        "U0AAA002": FakeUser(id="U0AAA002"),
        "U0AAA009": FakeUser(id="U0AAA009"),
        _BOT: FakeUser(id=_BOT, is_bot=True),
    }


class _Slack(FakeSlack):
    """FakeSlack plus doctor's duck-typed extra (auth_scopes)."""

    def __init__(self, *, scopes, **kwargs) -> None:
        super().__init__(**kwargs)
        self._scopes = frozenset(scopes)

    def auth_scopes(self):
        return self._scopes


def _slack(**kwargs) -> _Slack:
    base = dict(
        now=_NOW,
        bot_user_id=_BOT,
        channels=[_CHANNEL],
        bot_member_of=[_CHANNEL],
        channel_members={_CHANNEL: ["U0AAA001", "U0AAA002", "U0AAA009", _BOT]},
        users=_users(),
        scopes=slack_fixtures.full_granted_scopes(),
    )
    base.update(kwargs)
    return _Slack(**base)


# --------------------------------------------------------------------------- #
# 1. DOC-SCOPES passes when conversations.info itself answers missing_scope.
# --------------------------------------------------------------------------- #

class _PrivateChannelWithoutGroupsRead(_Slack):
    """The real API: a bot token without `groups:read` calling conversations.info
    on a private channel gets ok:false `missing_scope` (needed: groups:read) --
    it never sees an `is_private: true` body. G2 feed item 6 observed the same
    missing_scope for a private-channel read with channels:* only."""

    def channel_info(self, channel):
        if channel == _CHANNEL:
            raise _error_for("missing_scope")
        return super().channel_info(channel)


def test_doc_scopes_passes_when_private_channel_info_is_missing_scope(
    tmp_path: Path, capsys,
) -> None:
    """40-config-cli.md section 5.2 DOC-SCOPES: 'a private channel requires
    groups:history and groups:read among the granted scopes -- otherwise a FAIL
    naming the channel ID'. Against the real API a bot holding only the
    manifest's channels:* scopes cannot read a private channel's
    conversations.info at all: Slack answers `missing_scope`. doctor's
    `_first_private_channel_missing_scopes` swallows every SlackError (incl.
    MissingScope) with `continue`, so the private-channel arm never fires for
    exactly the misconfiguration it exists for and DOC-SCOPES prints PASS while
    a scope is missing (the FakeSlack double hands back is_private without
    enforcing the scope, which is why the module test passes)."""
    config_path = _write_config(tmp_path, _cfg())
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _PrivateChannelWithoutGroupsRead(
        now=_NOW, bot_user_id=_BOT, channels=[_CHANNEL], bot_member_of=[_CHANNEL],
        channel_members={_CHANNEL: ["U0AAA001", "U0AAA002", "U0AAA009", _BOT]},
        users=_users(), scopes=slack_fixtures.full_granted_scopes(),
        channel_meta={_CHANNEL: {"is_private": True}},
    )
    assert "groups:read" not in slack.auth_scopes()  # sanity: the scope really is absent
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    line = _find(_lines(capsys), "DOC-SCOPES")
    assert line.startswith("DOC-SCOPES FAIL"), line


# --------------------------------------------------------------------------- #
# 3. DOC-FACES-MODEL trusts a sidecar next to any configured model_path.
# --------------------------------------------------------------------------- #

def test_doc_faces_model_passes_a_foreign_model_with_its_own_sidecar(
    tmp_path: Path, capsys,
) -> None:
    """40-config-cli.md section 5.1 DOC-FACES-MODEL: the model at
    `faces.model_path` must hash to 'the committed sidecar
    `snipebot/models/face_detection_yunet_2023mar.onnx.sha256` (section 6.2)'.
    doctor instead compares against `<model_path>.sha256`, a file that travels
    with whatever path the config names. A config pointing `faces.model_path` at
    a different file that carries its own matching `.sha256` therefore PASSes,
    although its bytes are not the vendored YuNet model the face counts (and so
    the selfie bonus) are pinned to."""
    foreign = tmp_path / "other.onnx"
    foreign.write_bytes(b"not the vendored detector")
    Path(f"{foreign}.sha256").write_text(
        hashlib.sha256(foreign.read_bytes()).hexdigest() + "\n", encoding="utf-8",
    )
    cfg = _cfg(rules={"selfie_bonus": True}, faces={"model_path": str(foreign)})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    line = _find(_lines(capsys), "DOC-FACES-MODEL")
    assert line.startswith("DOC-FACES-MODEL FAIL"), line


# --------------------------------------------------------------------------- #
# 4. DOC-FACES-MODEL crashes when model_path names a directory.
# --------------------------------------------------------------------------- #

def test_doc_faces_model_crashes_when_model_path_is_a_directory(
    tmp_path: Path, capsys,
) -> None:
    """40-config-cli.md section 5 / module contract: every check prints one
    `DOC-XXX PASS|WARN|FAIL` line and a failed FAIL check exits 10. With
    `faces.model_path` set to a directory (e.g. the plausible slip
    `snipebot/models`, which config load accepts as a non-empty path)
    `_check_faces_model` sees `exists()` true and calls `read_bytes()`, which
    raises IsADirectoryError/PermissionError uncaught: doctor dies with a
    traceback (carrying local paths) instead of printing
    'DOC-FACES-MODEL FAIL' and exiting 10."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    Path(f"{model_dir}.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
    cfg = _cfg(rules={"selfie_bonus": True}, faces={"model_path": str(model_dir)})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    try:
        rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    except OSError as exc:  # the break
        raise AssertionError(f"doctor crashed instead of failing a check: {type(exc).__name__}")
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-FACES-MODEL").startswith("DOC-FACES-MODEL FAIL")


# --------------------------------------------------------------------------- #
# 5. DOC-FACES-IMPORT prints the ImportError text, which carries a path.
# --------------------------------------------------------------------------- #

def test_doc_faces_import_detail_leaks_a_path(tmp_path: Path, capsys, monkeypatch) -> None:
    """40-config-cli.md section 5.2: 'The <detail> carries IDs and counts only,
    never file names or paths' (module contract: never a file name). A
    pillow_heif install that is present but broken raises an ImportError whose
    text Python builds with the module's file path ("cannot import name 'x'
    from 'pkg' (<path>)"); on Linux a missing shared library reads
    '<lib>.so.N: cannot open shared object file'. `_check_faces_import` prints
    `_clip(str(exc))` verbatim, so the path -- on the owner's box one under the
    OS user's home directory -- reaches stdout."""
    shim = tmp_path / "shim"
    pkg = shim / "pillow_heif"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from ._lib import open_heif\n", encoding="utf-8")
    (pkg / "_lib.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(shim))
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] == "pillow_heif"}
    for key in saved:
        del sys.modules[key]
    try:
        cfg = _cfg(rules={"selfie_bonus": True})
        config_path = _write_config(tmp_path, cfg)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    finally:
        for key in [k for k in sys.modules if k.split(".")[0] == "pillow_heif"]:
            del sys.modules[key]
        sys.modules.update(saved)
    line = _find(_lines(capsys), "DOC-FACES-IMPORT")
    assert line.startswith("DOC-FACES-IMPORT FAIL"), line  # sanity: the broken-import path
    assert str(shim) not in line and "_lib.py" not in line, line
