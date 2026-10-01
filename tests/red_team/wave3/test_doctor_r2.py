"""Wave 3 red-team, round 2 (hostile input / failure injection) vs snipebot/doctor.py.

Round 1 (test_doctor_r1.py) attacked spec conformance of the individual checks.
Round 2 attacks robustness: what a ``doctor`` prints and returns when it is fed a
missing config file or a data directory whose bytes have drifted only in their
line endings. Each test drives ``doctor.run`` with a tmp_path config/data dir
(and, where a Slack block is needed, a ``tests.fake_slack.FakeSlack`` double) and
is written to FAIL on the current code, proving a break. No network is touched.
"""

from __future__ import annotations

import json as json_lib
import types
from pathlib import Path

import yaml

import snipebot.doctor as doctor
from snipebot.config import load_config
from snipebot.ledger import dumps_verdicts, load_ledger, load_state
from snipebot.rules import evaluate

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
# DOC-CONFIG-PARSE: an unreadable/missing config must not leak its file path.
# --------------------------------------------------------------------------- #

def test_doc_config_parse_leaks_config_file_path(tmp_path: Path, capsys) -> None:
    """doctor.py's module contract: 'IDs only -- never a name, permalink, URL,
    **file name** or token in the detail'; 20-sync-ledger.md section 9.2:
    'Forbidden anywhere in a log line: display names, permalinks/URLs ...,
    **file names**, ...'. When `--config` names a missing file, `load_config`'s
    bare `open(path, "rb")` raises FileNotFoundError whose str embeds the full
    filesystem path; `_check_config_parse` catches OSError and pipes
    `_clip(str(exc))` straight into the DOC-CONFIG-PARSE detail, so the config
    file path (here containing an unmistakable marker) is printed to stdout."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    missing_config = tmp_path / "SNIPEBOTxCONFIGxLEAKxMARKER" / "config.yaml"
    rc = doctor.run(_args(missing_config, data_dir), now_us=_ts_us(_NOW))
    out = "\n".join(_lines(capsys))
    # sanity: it is genuinely the DOC-CONFIG-PARSE FAIL path we are exercising
    assert rc == doctor.DOCTOR_FAILED
    # the break: the config file path is leaked into the detail line
    assert "SNIPEBOTxCONFIGxLEAKxMARKER" not in out


# --------------------------------------------------------------------------- #
# DOC-VERDICTS-FRESH: the comparison must be byte-equal, not newline-normalised.
# --------------------------------------------------------------------------- #

_COUNTED_ROW = {
    "ts": "1790000000.000000", "sender": "U0AAA001", "subtype": None, "thread_ts": None,
    "targets": ["U0AAA002"], "live_images": 1, "live_videos": 0, "linked_images": 0,
    "last_edit_ts": None, "file_sigs": [], "vetoes": [], "missing_runs": 0,
    "first_seen_targets": ["U0AAA002"], "first_sight_edited": False, "target_edited_in": [],
    "live_image_ids": [], "face_counts": {}, "rendition_hash": {}, "detect_attempts": 0,
    "selfie_override": None,
}


def test_doc_verdicts_fresh_crlf_passes_though_not_byte_equal(tmp_path: Path, capsys) -> None:
    """40-config-cli.md section 5.1 DOC-VERDICTS-FRESH: '`verdicts.jsonl` on disk
    is **byte-equal** to a fresh `dumps_verdicts` of `evaluate` re-run from the
    durable ledger'. `_check_ledger_and_verdicts` reads the file with
    `verdicts_path.read_text(encoding="utf-8")`, which applies universal-newline
    translation, so a CRLF-lined `verdicts.jsonl` (as git autocrlf materialises on
    the owner's Windows box) is NOT byte-equal to the LF `dumps_verdicts` output
    yet compares equal -- the staleness/corruption the check exists to catch is
    masked, so DOC-VERDICTS-FRESH PASSes and the run exits 0 instead of 10."""
    config_path = _write_config(tmp_path, _cfg())
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "ledger.jsonl").write_text(
        json_lib.dumps(_COUNTED_ROW) + "\n", encoding="utf-8")

    # Reproduce exactly the bytes doctor will compute as "fresh".
    config = load_config(config_path)
    rows = load_ledger(data_dir / "ledger.jsonl")
    state = load_state(data_dir / "state.json")
    verdicts = evaluate(
        rows, config.rules, config.roster, set(state.opted_out),
        config.semesters, config.tz,
    )
    fresh = dumps_verdicts(verdicts)
    assert fresh and "\n" in fresh  # guard: a non-empty, multi-line verdict set

    # Same content, CRLF line endings -> genuinely NOT byte-equal to `fresh`.
    on_disk = fresh.replace("\n", "\r\n").encode("utf-8")
    (data_dir / "verdicts.jsonl").write_bytes(on_disk)
    assert on_disk != fresh.encode("utf-8")  # guard: really byte-different

    rc = doctor.run(_args(config_path, data_dir), now_us=_ts_us(_NOW))
    line = _find(_lines(capsys), "DOC-VERDICTS-FRESH")
    # the break: a byte-different verdicts.jsonl is reported fresh
    assert line.startswith("DOC-VERDICTS-FRESH FAIL")
    assert rc == doctor.DOCTOR_FAILED
