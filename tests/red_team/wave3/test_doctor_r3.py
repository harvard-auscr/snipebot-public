"""Wave 3 red-team, round 3 (INVARIANTS) against snipebot/doctor.py.

Round 3 attacks invariants that must hold no matter the input: idempotence,
determinism, parity between ``tests.fake_slack.FakeSlack`` and the real
``snipebot.slack_io.SlackWebClient`` transport, no secret/name/file-name in any
output line, and no network. Each test drives ``doctor.run`` with a tmp_path
config/data dir (and, where a Slack block is needed, a fake transport), and is
written to FAIL on the current code, proving a break. No network is touched.
"""

from __future__ import annotations

import types
from pathlib import Path

import yaml

import snipebot.doctor as doctor

_NOW = "1800000000.000000"  # safely inside every test semester


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


def _args(config_path, data_dir: Path, *, offline: bool = True, json_out: bool = False):
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
# NO SECRETS: a check detail must never carry a file name.
# --------------------------------------------------------------------------- #

def test_doc_state_parse_detail_leaks_a_file_name(tmp_path: Path, capsys) -> None:
    """doctor.py module contract: every check prints one line, 'IDs only --
    never a name, permalink, URL, **file name** or token in the detail.'
    20-sync-ledger.md section 9.2 likewise forbids '**file names**' anywhere in
    an output line (allowed values are user/channel IDs, ts, counts, enum
    values, period keys, emoji names, commit SHAs -- a file name is none of
    these). A malformed `state.json` makes `load_state` raise
    MalformedLedgerError('state.json: unsupported version 2'); `_check_state_parse`
    pipes `_clip(str(exc))` into the DOC-STATE-PARSE detail, so the literal file
    name `state.json` is printed to stdout -- unlike `load_ledger`, whose errors
    say only 'line N: ...' with no file name."""
    config_path = _write_config(tmp_path, _cfg())
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "state.json").write_text(
        '{"version": 2, "watermark": null, "fingerprints": {}, "opted_out": {}}',
        encoding="utf-8",
    )
    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    out = "\n".join(_lines(capsys))
    # sanity: it really is the DOC-STATE-PARSE FAIL path.
    assert rc == doctor.DOCTOR_FAILED
    assert _find(out.splitlines(), "DOC-STATE-PARSE").startswith("DOC-STATE-PARSE FAIL")
    # the break: a file name is leaked into a doctor output line.
    assert "state.json" not in out
