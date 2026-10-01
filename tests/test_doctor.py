"""Doctor tests (40-config-cli.md section 5; 50-test-matrix.md section 2.7).

`FakeSlack` is used here (and only here, plus tests/test_doctor.py's own
extension of it) because `doctor` consumes only the `SlackIO` protocol surface
-- the transport-level fault behaviour lives in test_slack_io.py instead.
"""

from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import yaml

import snipebot.doctor as doctor
from tests.controls import slack_fixtures
from tests.fake_slack import FakeSlack, FakeUser

_NOW = "1800000000.000000"  # 2027-01-15ish; safely inside every test semester


class _DoctorFakeSlack(FakeSlack):
    """FakeSlack extended with the one duck-typed extra `doctor` calls beyond
    the `SlackIO` protocol: `auth_scopes` (10-slack-io.md section 2's
    `x-oauth-scopes` header is a real-transport concern; this test double
    models the same seam). It is not part of the frozen `SlackIO` protocol, so
    adding it here does not change what `FakeSlack` itself promises."""

    def __init__(self, *, scopes=slack_fixtures.FULL_BOT_SCOPES, **kwargs) -> None:
        super().__init__(**kwargs)
        self._scopes = frozenset(scopes)

    def auth_scopes(self):
        return self._scopes


# --------------------------------------------------------------------------- #
# config / args helpers
# --------------------------------------------------------------------------- #

def _cfg(**overrides) -> dict:
    base = {
        "slack": {"channel": "C0MAINAA"},
        "timezone": "America/New_York",
        "semesters": [{"name": "fall", "start": "2020-01-01", "end": "2035-12-20"}],
        "rules": {"selfie_bonus": True},
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


# --------------------------------------------------------------------------- #
# a fully-green offline run (positive control for the offline block)
# --------------------------------------------------------------------------- #

def test_offline_run_all_pass_with_bonus_off(tmp_path: Path) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    args = _args(config_path, data_dir, offline=True)
    rc = doctor.run(args, now_us=_ts_us(_NOW))
    assert rc == doctor.OK


def test_offline_json_mode_emits_one_record_per_check(tmp_path: Path) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    args = _args(config_path, data_dir, offline=True, json_out=True)
    rc = doctor.run(args, now_us=_ts_us(_NOW))
    assert rc == doctor.OK


def test_offline_run_json_records_are_well_formed(tmp_path: Path, capsys) -> None:
    import json

    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    args = _args(config_path, data_dir, offline=True, json_out=True)
    doctor.run(args, now_us=_ts_us(_NOW))
    lines = _lines(capsys)
    assert lines
    for line in lines:
        record = json.loads(line)
        assert set(record) == {"id", "severity", "ok", "detail"}
        assert record["severity"] in ("FAIL", "WARN")


def _ts_us(ts: str) -> int:
    from snipebot.ts import parse_ts

    return parse_ts(ts)


# --------------------------------------------------------------------------- #
# DOC-RULES-RESOLVE detail (E19: '<N> dated rule(s), first effective <date>')
# --------------------------------------------------------------------------- #

def _rules_resolve_detail(capsys) -> str:
    import json

    for line in _lines(capsys):
        record = json.loads(line)
        if record["id"] == "DOC-RULES-RESOLVE":
            return record["detail"]
    raise AssertionError("DOC-RULES-RESOLVE not printed")


def test_rules_resolve_detail_single_undated_rule(tmp_path: Path, capsys) -> None:
    # The single-mapping rule form is effective "at any time"; the detail reports
    # it as effective from the first semester start (2020-01-01 here).
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir, json_out=True), now_us=_ts_us(_NOW))
    assert _rules_resolve_detail(capsys) == "1 dated rule(s), first effective 2020-01-01"


def test_rules_resolve_detail_dated_list(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules=[
        {"effective_from": "2019-12-15", "selfie_bonus": False},
        {"effective_from": "2021-06-01", "selfie_bonus": False},
    ])
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir, json_out=True), now_us=_ts_us(_NOW))
    assert _rules_resolve_detail(capsys) == "2 dated rule(s), first effective 2019-12-15"


# --------------------------------------------------------------------------- #
# DOC-CONFIG-PARSE cascades
# --------------------------------------------------------------------------- #

def test_bad_config_fails_config_parse_and_cascades(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("not: [valid, config", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    args = _args(config_path, data_dir, offline=True)
    rc = doctor.run(args)
    assert rc == doctor.DOCTOR_FAILED
    lines = _lines(capsys)
    assert _find(lines, "DOC-CONFIG-PARSE").startswith("DOC-CONFIG-PARSE FAIL")


def test_missing_config_file_fails_config_parse(tmp_path: Path) -> None:
    args = _args(tmp_path / "nope.yaml", tmp_path / "data", offline=True)
    rc = doctor.run(args)
    assert rc == doctor.DOCTOR_FAILED


# --------------------------------------------------------------------------- #
# DOC-FACES-MODEL / DOC-FACES-IMPORT (mine; both ways)
# --------------------------------------------------------------------------- #

_VENDORED_MODEL = (
    Path(doctor.__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"
)


def _write_model_and_sidecar(tmp_path: Path, *, mismatch: bool = False) -> Path:
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"pretend-onnx-bytes")
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if mismatch:
        digest = "0" * 64
    (tmp_path / "model.onnx.sha256").write_text(digest + "\n", encoding="utf-8")
    return model_path


def test_faces_model_pass_when_bonus_off_even_without_a_model(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False}, faces={"model_path": str(tmp_path / "absent.onnx")})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert _find(_lines(capsys), "DOC-FACES-MODEL") == "DOC-FACES-MODEL PASS"


def test_faces_model_fails_when_missing(tmp_path: Path, capsys) -> None:
    cfg = _cfg(faces={"model_path": str(tmp_path / "absent.onnx")})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-FACES-MODEL").startswith("DOC-FACES-MODEL FAIL")


def test_faces_model_fails_when_sidecar_mismatches(tmp_path: Path, capsys) -> None:
    model_path = _write_model_and_sidecar(tmp_path, mismatch=True)
    cfg = _cfg(faces={"model_path": str(model_path)})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-FACES-MODEL").startswith("DOC-FACES-MODEL FAIL")


def test_faces_model_passes_when_sidecar_matches(tmp_path: Path, capsys) -> None:
    """E-W4-20b: the sidecar is the one committed in the package (40 section 5.1),
    so the passing model is the vendored one."""
    model_path = _VENDORED_MODEL
    cfg = _cfg(faces={"model_path": str(model_path)})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert _find(_lines(capsys), "DOC-FACES-MODEL") == "DOC-FACES-MODEL PASS"


def test_faces_model_ignores_a_sidecar_beside_the_model_path(tmp_path: Path, capsys) -> None:
    """E-W4-20b: a `<model_path>.sha256` matching a foreign model never makes it
    pass; only the committed package sidecar counts."""
    model_path = _write_model_and_sidecar(tmp_path, mismatch=False)
    cfg = _cfg(faces={"model_path": str(model_path)})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-FACES-MODEL").startswith("DOC-FACES-MODEL FAIL")


def test_faces_import_pass_when_bonus_off(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert _find(_lines(capsys), "DOC-FACES-IMPORT") == "DOC-FACES-IMPORT PASS"


def test_faces_import_pass_when_cv2_available(tmp_path: Path, capsys) -> None:
    model_path = _write_model_and_sidecar(tmp_path)
    cfg = _cfg(faces={"model_path": str(model_path)})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert _find(_lines(capsys), "DOC-FACES-IMPORT") == "DOC-FACES-IMPORT PASS"


def test_faces_import_fails_when_cv2_unavailable(tmp_path: Path, capsys, monkeypatch) -> None:
    model_path = _write_model_and_sidecar(tmp_path)
    cfg = _cfg(faces={"model_path": str(model_path)})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setitem(sys.modules, "cv2", None)  # import cv2 -> ImportError
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-FACES-IMPORT").startswith("DOC-FACES-IMPORT FAIL")


def test_faces_import_fails_when_pillow_heif_unavailable_and_bonus_on(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    model_path = _write_model_and_sidecar(tmp_path)
    cfg = _cfg(faces={"model_path": str(model_path)})  # selfie_bonus defaults on
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # sys.modules[name] = None makes `import pillow_heif` raise ImportError; the
    # monkeypatch is restored after the test, so no other test sees it missing.
    monkeypatch.setitem(sys.modules, "pillow_heif", None)
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-FACES-IMPORT").startswith("DOC-FACES-IMPORT FAIL")


def test_faces_import_warns_when_pillow_heif_unavailable_and_bonus_off(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setitem(sys.modules, "pillow_heif", None)  # restored after the test
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.OK  # WARN never fails the run when the bonus is off
    assert _find(_lines(capsys), "DOC-FACES-IMPORT").startswith("DOC-FACES-IMPORT WARN")


# --------------------------------------------------------------------------- #
# DOC-SCOPES (mine; both ways, plus the CTL-DOCTOR-SCOPE seam)
# --------------------------------------------------------------------------- #

def test_doc_scopes_full_manifest_grants_pass(tmp_path: Path, capsys) -> None:
    """Positive control for CTL-DOCTOR-SCOPE (tests/controls/registry.py): a
    token granted every scope `tests/controls/slack_fixtures.py` mirrors from
    the real `slack-app-manifest.yaml` satisfies DOC-SCOPES. selfie_bonus is on,
    so `files:read` is required and present."""
    cfg = _cfg(rules={"selfie_bonus": True})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _DoctorFakeSlack(
        now=_NOW, scopes=slack_fixtures.full_granted_scopes(),
        channel_members={"C0MAINAA": ["U0AAA001", "U0AAA002", "U0ADMIN1", "U0BOT"]},
        users={
            "U0AAA001": FakeUser(id="U0AAA001"),
            "U0AAA002": FakeUser(id="U0AAA002"),
            "U0ADMIN1": FakeUser(id="U0ADMIN1"),
            "U0BOT": FakeUser(id="U0BOT", is_bot=True),
        },
    )
    args = _args(config_path, data_dir, offline=False)
    doctor.run(args, slack_factory=lambda: slack, now_us=_ts_us(_NOW))
    assert _find(_lines(capsys), "DOC-SCOPES") == "DOC-SCOPES PASS"


def test_doc_scopes_missing_scope_fails(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": True})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    reduced = tuple(s for s in slack_fixtures.full_granted_scopes() if s != "chat:write")
    slack = _DoctorFakeSlack(now=_NOW, scopes=reduced)
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-SCOPES").startswith("DOC-SCOPES FAIL")


def test_doc_scopes_files_read_not_required_when_bonus_off(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    without_files_read = tuple(
        s for s in slack_fixtures.full_granted_scopes() if s != "files:read"
    )
    slack = _DoctorFakeSlack(now=_NOW, scopes=without_files_read)
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-SCOPES") == "DOC-SCOPES PASS"


# --------------------------------------------------------------------------- #
# DOC-SCOPES private-channel scopes (E10: is_private -> groups:history/read)
# --------------------------------------------------------------------------- #

def test_doc_scopes_private_watched_channel_without_groups_fails(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # The watched channel is private, but the fully-granted manifest token carries
    # no groups:* scopes -- DOC-SCOPES must FAIL naming the channel ID.
    slack = _DoctorFakeSlack(
        now=_NOW, scopes=slack_fixtures.full_granted_scopes(),
        channels=["C0MAINAA"], bot_member_of=["C0MAINAA"],
        channel_meta={"C0MAINAA": {"is_private": True}},
    )
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    line = _find(_lines(capsys), "DOC-SCOPES")
    assert line.startswith("DOC-SCOPES FAIL")
    assert "C0MAINAA" in line


def test_doc_scopes_private_watched_channel_passes_with_groups_granted(
    tmp_path: Path, capsys,
) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    scopes = slack_fixtures.full_granted_scopes() + ("groups:history", "groups:read")
    slack = _DoctorFakeSlack(
        now=_NOW, scopes=scopes,
        channels=["C0MAINAA"], bot_member_of=["C0MAINAA"],
        channel_meta={"C0MAINAA": {"is_private": True}},
    )
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-SCOPES") == "DOC-SCOPES PASS"


def test_doc_scopes_public_channel_needs_no_groups(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # A public watched channel (is_private False) satisfies DOC-SCOPES with only
    # the base manifest grant -- no groups:* required.
    slack = _DoctorFakeSlack(
        now=_NOW, scopes=slack_fixtures.full_granted_scopes(),
        channels=["C0MAINAA"], bot_member_of=["C0MAINAA"],
        channel_meta={"C0MAINAA": {"is_private": False}},
    )
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-SCOPES") == "DOC-SCOPES PASS"


def test_doc_scopes_private_post_to_channel_without_groups_fails(tmp_path: Path, capsys) -> None:
    cfg = _cfg(
        rules={"selfie_bonus": False},
        reports=[{
            "name": "weekly-standings", "every": "1w", "at": "09:00", "weekday": "mon",
            "post_to": "C0POST001", "sections": ["week"],
        }],
    )
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # The watched channel is public; only the report's post_to is private.
    slack = _DoctorFakeSlack(
        now=_NOW, scopes=slack_fixtures.full_granted_scopes(),
        channels=["C0MAINAA", "C0POST001"], bot_member_of=["C0MAINAA", "C0POST001"],
        channel_meta={
            "C0MAINAA": {"is_private": False}, "C0POST001": {"is_private": True},
        },
    )
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    line = _find(_lines(capsys), "DOC-SCOPES")
    assert line.startswith("DOC-SCOPES FAIL")
    assert "C0POST001" in line


# --------------------------------------------------------------------------- #
# DOC-CHANNEL-MEMBER / DOC-POSTTO-MEMBER (mine; both ways)
# --------------------------------------------------------------------------- #

def test_channel_member_pass(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _DoctorFakeSlack(now=_NOW, channels=["C0MAINAA"], bot_member_of=["C0MAINAA"])
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-CHANNEL-MEMBER") == "DOC-CHANNEL-MEMBER PASS"


def test_channel_member_fails_when_not_a_member(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _DoctorFakeSlack(now=_NOW, channels=["C0MAINAA"], bot_member_of=[])
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-CHANNEL-MEMBER").startswith("DOC-CHANNEL-MEMBER FAIL")


def test_channel_member_fails_when_channel_not_found(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False}, slack={"channel": "C0GHOST01"})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _DoctorFakeSlack(now=_NOW, channels=["C0MAINAA"], bot_member_of=["C0MAINAA"])
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-CHANNEL-MEMBER").startswith("DOC-CHANNEL-MEMBER FAIL")


def test_postto_member_pass_and_fail(tmp_path: Path, capsys) -> None:
    cfg = _cfg(
        rules={"selfie_bonus": False},
        reports=[{
            "name": "weekly-standings", "every": "1w", "at": "09:00", "weekday": "mon",
            "post_to": "C0POST001", "sections": ["week"],
        }],
    )
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # PASS: the bot is a member of both the watched and the post_to channel.
    slack = _DoctorFakeSlack(
        now=_NOW, channels=["C0MAINAA", "C0POST001"],
        bot_member_of=["C0MAINAA", "C0POST001"],
    )
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-POSTTO-MEMBER") == "DOC-POSTTO-MEMBER PASS"

    # FAIL: the bot is a member of the watched channel only.
    slack2 = _DoctorFakeSlack(
        now=_NOW, channels=["C0MAINAA", "C0POST001"], bot_member_of=["C0MAINAA"],
    )
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack2,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-POSTTO-MEMBER").startswith("DOC-POSTTO-MEMBER FAIL")


def test_postto_member_vacuously_passes_with_no_reports(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _DoctorFakeSlack(now=_NOW, bot_member_of=["C0MAINAA"])
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-POSTTO-MEMBER") == "DOC-POSTTO-MEMBER PASS"


# --------------------------------------------------------------------------- #
# DOC-ROSTER-RESOLVE (mine; both ways)
# --------------------------------------------------------------------------- #

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


def test_roster_resolve_pass(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _roster_slack()
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-ROSTER-RESOLVE") == "DOC-ROSTER-RESOLVE PASS"


def test_roster_resolve_fails_when_not_a_channel_member(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _roster_slack(channel_members={"C0MAINAA": ["U0AAA001", "U0BOT"]})  # U0AAA002 missing
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-ROSTER-RESOLVE").startswith("DOC-ROSTER-RESOLVE FAIL")


def test_roster_resolve_fails_when_deleted(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _roster_slack(users={
        "U0AAA001": FakeUser(id="U0AAA001"),
        "U0AAA002": FakeUser(id="U0AAA002", deleted=True),
        "U0BOT": FakeUser(id="U0BOT", is_bot=True),
    })
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-ROSTER-RESOLVE").startswith("DOC-ROSTER-RESOLVE FAIL")


def test_roster_resolve_fails_when_not_in_users_list(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _roster_slack(users={
        "U0AAA001": FakeUser(id="U0AAA001"),
        "U0BOT": FakeUser(id="U0BOT", is_bot=True),
    })
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-ROSTER-RESOLVE").startswith("DOC-ROSTER-RESOLVE FAIL")


# --------------------------------------------------------------------------- #
# DOC-AUTH
# --------------------------------------------------------------------------- #

def test_auth_pass(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    slack = _roster_slack()
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-AUTH") == "DOC-AUTH PASS"


def test_auth_fails_on_transport_failure(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    class _BrokenSlack(_DoctorFakeSlack):
        def auth_identity(self):
            from snipebot.slack_io import SlackTransportError

            raise SlackTransportError("no response")

    slack = _BrokenSlack(now=_NOW)
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-AUTH").startswith("DOC-AUTH FAIL")


# --------------------------------------------------------------------------- #
# --offline skips the Slack block entirely
# --------------------------------------------------------------------------- #

def test_offline_flag_skips_slack_block(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    def _factory():
        raise AssertionError("slack_factory must not be called under --offline")

    rc = doctor.run(
        _args(config_path, data_dir, offline=True), slack_factory=_factory,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.OK
    lines = _lines(capsys)
    assert not any(line.startswith("DOC-AUTH") for line in lines)


# --------------------------------------------------------------------------- #
# DOC-STATE-PARSE / DOC-LEDGER-INTEGRITY / DOC-VERDICTS-FRESH
# --------------------------------------------------------------------------- #

def test_state_parse_fails_on_malformed_state(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "state.json").write_text("not json", encoding="utf-8")
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-STATE-PARSE").startswith("DOC-STATE-PARSE FAIL")


def test_ledger_integrity_fails_on_malformed_ledger(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "ledger.jsonl").write_text("not json\n", encoding="utf-8")
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-LEDGER-INTEGRITY").startswith("DOC-LEDGER-INTEGRITY FAIL")


def test_verdicts_fresh_fails_when_stale(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "verdicts.jsonl").write_text('{"tampered": true}\n', encoding="utf-8")
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.DOCTOR_FAILED
    assert _find(_lines(capsys), "DOC-VERDICTS-FRESH").startswith("DOC-VERDICTS-FRESH FAIL")


def test_verdicts_fresh_passes_on_empty_ledger_and_state(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert _find(_lines(capsys), "DOC-VERDICTS-FRESH") == "DOC-VERDICTS-FRESH PASS"


# --------------------------------------------------------------------------- #
# DOC-PERSISTENCE-FILES / DOC-FINGERPRINT-* (WARN)
# --------------------------------------------------------------------------- #

def test_persistence_files_warns_under_files_mode(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False}, persistence="files")
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.OK  # WARN never fails the run
    assert _find(_lines(capsys), "DOC-PERSISTENCE-FILES").startswith("DOC-PERSISTENCE-FILES WARN")


def test_persistence_files_passes_under_git_mode(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})  # persistence defaults to git
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert _find(_lines(capsys), "DOC-PERSISTENCE-FILES") == "DOC-PERSISTENCE-FILES PASS"


def test_fingerprints_pass_trivially_on_first_run(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    lines = _lines(capsys)
    for cid in (
        "DOC-FINGERPRINT-RULES", "DOC-FINGERPRINT-PLAYERS",
        "DOC-FINGERPRINT-SEMESTERS", "DOC-FINGERPRINT-GROUPS",
    ):
        assert _find(lines, cid) == f"{cid} PASS"


def test_fingerprint_rules_warns_on_mismatch(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "ledger.jsonl").write_text("", encoding="utf-8")
    (data_dir / "state.json").write_text(
        '{"version": 1, "watermark": null, '
        '"fingerprints": {"rules": "deadbeef"}, "opted_out": {}}',
        encoding="utf-8",
    )
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    assert rc == doctor.OK  # a fingerprint mismatch is WARN-only
    assert _find(_lines(capsys), "DOC-FINGERPRINT-RULES").startswith("DOC-FINGERPRINT-RULES WARN")


# --------------------------------------------------------------------------- #
# DOC-ADMIN-RESOLVE / DOC-ROSTER-BOT / DOC-OPTOUT-MSG (WARN)
# --------------------------------------------------------------------------- #

def test_admin_resolve_both_ways(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    ok_slack = _roster_slack(users={
        "U0AAA001": FakeUser(id="U0AAA001"),
        "U0AAA002": FakeUser(id="U0AAA002"),
        "U0BOT": FakeUser(id="U0BOT", is_bot=True),
        "U0ADMIN1": FakeUser(id="U0ADMIN1"),
    })
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: ok_slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-ADMIN-RESOLVE") == "DOC-ADMIN-RESOLVE PASS"

    missing_slack = _roster_slack()  # no U0ADMIN1 in users
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: missing_slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-ADMIN-RESOLVE").startswith("DOC-ADMIN-RESOLVE WARN")


def test_roster_bot_both_ways(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False, "allow_bots": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    clean_slack = _roster_slack()  # neither U0AAA001 nor U0AAA002 is a bot
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: clean_slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-ROSTER-BOT") == "DOC-ROSTER-BOT PASS"

    bot_slack = _roster_slack(users={
        "U0AAA001": FakeUser(id="U0AAA001", is_bot=True),
        "U0AAA002": FakeUser(id="U0AAA002"),
        "U0BOT": FakeUser(id="U0BOT", is_bot=True),
    })
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: bot_slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-ROSTER-BOT").startswith("DOC-ROSTER-BOT WARN")


def test_optout_msg_both_ways(tmp_path: Path, capsys) -> None:
    optout_ts = "1799999999.000001"  # a few seconds before _NOW: inside the horizon
    cfg = _cfg(
        rules={"selfie_bonus": False},
        consent={"veto": {"emoji": "x"}, "optout_messages": [optout_ts]},
    )
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    unreadable_slack = _roster_slack()  # nothing ever posted at that ts
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: unreadable_slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-OPTOUT-MSG").startswith("DOC-OPTOUT-MSG WARN")

    readable_slack = _roster_slack()
    readable_slack.post(
        at=optout_ts, user="U0AAA001", channel="C0MAINAA", text="see profile",
    )
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: readable_slack,
        now_us=_ts_us(_NOW),
    )
    assert _find(_lines(capsys), "DOC-OPTOUT-MSG") == "DOC-OPTOUT-MSG PASS"


def test_plain_slackio_fails_scopes_closed(tmp_path: Path, capsys) -> None:
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    plain = FakeSlack(
        now=_NOW, bot_member_of=["C0MAINAA"],
        channel_members={"C0MAINAA": ["U0AAA001", "U0AAA002", "U0BOT"]},
        users={
            "U0AAA001": FakeUser(id="U0AAA001"),
            "U0AAA002": FakeUser(id="U0AAA002"),
            "U0BOT": FakeUser(id="U0BOT", is_bot=True),
        },
    )
    # A plain FakeSlack has no auth_scopes; DOC-SCOPES fails closed (unavailable).
    rc = doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: plain,
        now_us=_ts_us(_NOW),
    )
    assert rc == doctor.DOCTOR_FAILED
    lines = _lines(capsys)
    assert _find(lines, "DOC-SCOPES").startswith("DOC-SCOPES FAIL")


def test_online_doctor_has_no_emoji_existence_check(tmp_path: Path, capsys) -> None:
    """E-W4-7: DOC-EMOJI-EXISTS is removed (emoji.list lists custom emoji only and
    needs a scope the manifest never grants); an online run neither prints it nor
    calls any emoji listing on the transport."""
    cfg = _cfg(rules={"selfie_bonus": False})
    config_path = _write_config(tmp_path, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    calls: list[str] = []

    class _Recording(_DoctorFakeSlack):
        def emoji_list(self, *args, **kwargs):
            calls.append("emoji_list")
            return {}

    slack = _Recording(
        now=_NOW, channels=["C0MAINAA"], bot_member_of=["C0MAINAA"],
        channel_members={"C0MAINAA": ["U0AAA001", "U0AAA002", "U0BOT"]},
        users={
            "U0AAA001": FakeUser(id="U0AAA001"),
            "U0AAA002": FakeUser(id="U0AAA002"),
            "U0BOT": FakeUser(id="U0BOT", is_bot=True),
        },
    )
    doctor.run(
        _args(config_path, data_dir, offline=False), slack_factory=lambda: slack,
        now_us=_ts_us(_NOW),
    )
    lines = _lines(capsys)
    assert not any(line.startswith("DOC-EMOJI-EXISTS") for line in lines), lines
    assert calls == []
    assert not hasattr(doctor, "_check_emoji_exists")
