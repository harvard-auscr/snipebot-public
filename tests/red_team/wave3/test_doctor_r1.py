"""Wave 3 red-team, round 1 (spec conformance) against snipebot/doctor.py.

Every test drives ``doctor.run`` with a tmp_path config/data dir and a
``tests.fake_slack.FakeSlack`` double, and asserts a rule from
``spec/40-config-cli.md`` section 5 (offline + Slack checks) or the module's own
stated invariant. Each test is written to FAIL on the current code, proving a
break. No network is touched; every Slack call goes through the fake.
"""

from __future__ import annotations

import hashlib
import types
from pathlib import Path

import yaml

import snipebot.doctor as doctor
from tests.controls import slack_fixtures
from tests.fake_slack import FakeSlack, FakeUser

_NOW = "1800000000.000000"  # safely inside every test semester


class _DoctorFakeSlack(FakeSlack):
    """FakeSlack plus the two duck-typed extras doctor calls (auth_scopes,
    emoji_list); mirrors tests/test_doctor.py's own extension."""

    def __init__(self, *, scopes=slack_fixtures.FULL_BOT_SCOPES, emoji=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._scopes = frozenset(scopes)
        self._emoji = dict(emoji) if emoji is not None else {}

    def auth_scopes(self):
        return self._scopes

    def emoji_list(self):
        return self._emoji


def _ts_us(ts: str) -> int:
    from snipebot.ts import parse_ts

    return parse_ts(ts)


def _cfg(**overrides) -> dict:
    base = {
        "slack": {"channel": "C0MAINAA"},
        "timezone": "America/New_York",
        "semesters": [{"name": "fall", "start": "2020-01-01", "end": "2035-12-20"}],
        "rules": {"selfie_bonus": False},
        "players": {"extras": ["U0AAA001", "U0AAA002"]},
        "consent": {"veto": {"emoji": "x"}},
        "admins": ["U0ADMIN1"],
        "feedback": {"reactions": {}},
    }
    base.update(overrides)
    return base


def _write_config(tmp_path: Path, cfg: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def _args(config_path: Path, data_dir: Path, *, offline: bool = True, json_out: bool = False):
    return types.SimpleNamespace(
        config=str(config_path), data_dir=str(data_dir), offline=offline, json=json_out,
    )


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def _find(lines: list[str], check_id: str) -> str:
    for line in lines:
        if line.startswith(check_id + " "):
            return line
    raise AssertionError(f"{check_id} not printed; got:\n" + "\n".join(lines))


def _roster_slack(**overrides) -> _DoctorFakeSlack:
    kwargs = dict(
        now=_NOW,
        channels=["C0MAINAA"],
        bot_member_of=["C0MAINAA"],
        channel_members={"C0MAINAA": ["U0AAA001", "U0AAA002", "U0BOT"]},
        users={
            "U0AAA001": FakeUser(id="U0AAA001"),
            "U0AAA002": FakeUser(id="U0AAA002"),
            "U0BOT": FakeUser(id="U0BOT", is_bot=True),
        },
    )
    kwargs.update(overrides)
    return _DoctorFakeSlack(**kwargs)


# --------------------------------------------------------------------------- #
# DOC-AUTH: the token must be a BOT token, not just a working token.
# --------------------------------------------------------------------------- #

def test_doc_auth_accepts_a_non_bot_user_token(tmp_path: Path, capsys) -> None:
    """40-config-cli.md section 5.2 DOC-AUTH passes when '`auth.test` succeeds
    and the token is a **bot** token'. A user (xoxp-) token's `auth.test` carries
    no `bot_id` (slack_io.auth_identity: `bot_id=str(data.get("bot_id", ""))`),
    so `AuthIdentity.bot_id == ""`. doctor's `_check_auth` only catches
    `SlackError` and never inspects `bot_id`, so a user token wrongly PASSes."""
    config_path = _write_config(tmp_path, _cfg())
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _roster_slack()
    slack.bot_id = ""  # a user token: auth.test returns no bot_id
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-AUTH").startswith("DOC-AUTH FAIL")


# --------------------------------------------------------------------------- #
# DOC-SCOPES: a detail line must carry IDs only, never a file name.
# --------------------------------------------------------------------------- #

def test_doc_scopes_manifest_error_leaks_a_file_path(tmp_path: Path, capsys) -> None:
    """doctor.py module contract (and 20-sync-ledger.md section 9.2, user IDs
    only): every check prints one line 'IDs only -- never a name, permalink,
    URL, **file name** or token in the detail.' When the manifest is unreadable
    `_check_scopes` emits `f"manifest unreadable: {_clip(exc)}"`, and the
    OSError text embeds the manifest path, leaking a file name into stdout."""
    config_path = _write_config(tmp_path, _cfg())
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _roster_slack()
    bogus_manifest = tmp_path / "MANIFESTLEAKMARKER.yaml"  # does not exist
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW), manifest_path=bogus_manifest,
    )
    out = "\n".join(_lines(capsys))
    assert "MANIFESTLEAKMARKER" not in out


# --------------------------------------------------------------------------- #
# DOC-FACES-MODEL: an empty sidecar must FAIL the check, not crash the run.
# --------------------------------------------------------------------------- #

def test_doc_faces_model_empty_sidecar_crashes_instead_of_failing(tmp_path: Path, capsys) -> None:
    """40-config-cli.md section 5.1 DOC-FACES-MODEL is a FAIL check and 4.4 maps
    a doctor FAIL to exit 10. With `selfie_bonus` on, a present model plus an
    empty sidecar file (exists, so 'sidecar sha256 missing' does not fire) makes
    `_check_faces_model` run `sidecar.read_text().strip().split()[0]` on '' ->
    IndexError, an uncaught crash (mapped to exit 1 UNEXPECTED) rather than a
    clean DOC-FACES-MODEL FAIL / exit 10."""
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"pretend-onnx-bytes")
    (tmp_path / "model.onnx.sha256").write_text("", encoding="utf-8")  # empty sidecar
    cfg = _cfg(rules={"selfie_bonus": True}, faces={"model_path": str(model_path)})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-FACES-MODEL").startswith("DOC-FACES-MODEL FAIL")


# unused import guard (kept for parity with sibling suites)
_ = hashlib
