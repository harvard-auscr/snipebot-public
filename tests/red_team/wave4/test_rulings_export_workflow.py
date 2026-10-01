"""Regression tests for ruling E-W4-40: the weekly `export.yml` workflow (40 §7.4) and the
example config's `recaps: false` (40 §1.1, E-W4-39).

The workflow is read-only: it checks out the code and the `data` branch, refreshes the
runner-local users.json with `roster` (stdout discarded: it lists display names), runs
`export`, and uploads the CSV/XLSX tables as the `standings` artifact. Nothing is committed,
pushed or posted. The layout test replays those two steps offline against a FakeSlack world.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.config import load_config

from tests.red_team.wave4.test_rulings_cli import (
    CHANNEL, TARGET3, TARGET4, _bot_target_synced, _world, fixed_clock,  # noqa: F401
)

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "export.yml"
NODE24_ACTIONS = {"actions/checkout@v5", "actions/setup-python@v6", "actions/upload-artifact@v6"}


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _doc() -> dict:
    return yaml.safe_load(_text())


def _job() -> dict:
    (job,) = _doc()["jobs"].values()
    return job


def _steps() -> list[dict]:
    return _job()["steps"]


def _step(name: str) -> dict:
    return next(s for s in _steps() if s.get("name") == name)


# --- the workflow file ----------------------------------------------------------------------

def test_export_triggers_weekly_and_on_demand_only():
    """Mondays 00:17 UTC (Sunday evening in New York, off the :00 peak) plus a manual
    dispatch with no inputs; no push/pull_request trigger."""
    doc = _doc()
    assert doc["name"] == "export"
    on = doc[True]                 # PyYAML reads the bare `on:` key as True
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert on["schedule"] == [{"cron": "17 0 * * 1"}]
    assert on["workflow_dispatch"] == {}


def test_export_is_read_only_in_its_own_concurrency_group():
    doc = _doc()
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"] == {"group": "snipebot-export", "cancel-in-progress": False}
    job = _job()
    assert "permissions" not in job
    assert job["timeout-minutes"] == 15
    assert job["runs-on"] == "ubuntu-latest"


def test_export_never_commits_pushes_or_needs_the_data_repo():
    """No step runs git, commits, pushes or posts, and nothing points the persistence layer
    at the checkout (SNIPEBOT_DATA_REPO, a git identity). Comments are dropped first: they may
    say what the workflow does not do."""
    text = yaml.safe_dump(_doc(), sort_keys=False)
    for needle in ("git ", "commit", "push", "contents: write", "SNIPEBOT_DATA_REPO",
                   "SNIPEBOT_GIT_AUTHOR", "sync", "backfill", "chat.post"):
        assert needle not in text, needle
    runs = [s["run"] for s in _steps() if "run" in s]
    assert runs and all(not re.search(r"\bgit\b", r) for r in runs), runs
    commands = [r.split("python -m snipebot", 1)[1].split()[0]
                for r in runs if "python -m snipebot" in r]
    assert commands == ["roster", "export"], commands


def test_export_checks_out_code_then_the_data_branch_shallow():
    steps = _steps()
    assert "private" in steps[0]["name"].lower()   # the visibility guard (test_public_safety)
    assert steps[1]["uses"] == "actions/checkout@v5" and "with" not in steps[1]
    data = _step("Checkout data branch")
    assert data["uses"] == "actions/checkout@v5"
    assert data["with"] == {"ref": "data", "path": "_data", "fetch-depth": 1}
    py = _step("Set up Python")
    assert py["with"] == {"python-version": "3.11", "cache": "pip"}
    assert _step("Install runtime deps")["run"] == "pip install -r requirements.txt"


def test_roster_step_discards_stdout_and_alone_holds_the_token():
    """`roster` prints display names on stdout, so the step redirects it away from the log;
    it is the only step handed the Slack token."""
    roster = _step("Refresh names")
    assert roster["run"].strip() == "python -m snipebot roster --data-dir _data/data > /dev/null"
    assert roster["env"] == {"SLACK_BOT_TOKEN": "${{ secrets.SLACK_BOT_TOKEN }}"}
    others = [s for s in _steps() if s.get("name") != "Refresh names"]
    assert all("SLACK_BOT_TOKEN" not in str(s) for s in others)
    assert _text().count("secrets.") == 1


def test_export_step_follows_roster_and_writes_exports():
    names = [s.get("name") for s in _steps()]
    assert names.index("Refresh names") < names.index("Export") < names.index("Upload standings")
    export = _step("Export")
    assert export["run"].strip() == "python -m snipebot export --data-dir _data/data --out exports"
    assert "env" not in export


def test_export_uploads_the_standings_artifact_for_90_days():
    upload = _step("Upload standings")
    assert upload["uses"] == "actions/upload-artifact@v6"
    assert upload["with"] == {"name": "standings", "path": "exports/", "retention-days": 90,
                              "if-no-files-found": "error"}
    assert _steps()[-1].get("name") == "Upload standings"


def test_every_action_is_a_node24_major():
    uses = {s["uses"] for s in _steps() if "uses" in s}
    assert uses == NODE24_ACTIONS, uses


# --- config.example.yaml --------------------------------------------------------------------

def test_example_config_loads_with_recaps_off_right_after_enabled():
    assert load_config(REPO / "config.example.yaml").recaps is False
    raw = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    keys = list(raw)
    assert keys[keys.index("enabled") + 1] == "recaps"
    assert raw["recaps"] is False


def test_example_recaps_line_flips_with_the_cli(tmp_path, capsys):
    """The example's commented `recaps:` line is one `snipebot recaps on|off` can rewrite:
    the value flips, the comment and every other byte survive."""
    cfg = tmp_path / "config.yaml"
    original = (REPO / "config.example.yaml").read_bytes()
    cfg.write_bytes(original)
    assert main(["--config", str(cfg), "recaps", "on"]) == int(Exit.OK)
    assert load_config(cfg).recaps is True
    on_text = cfg.read_text(encoding="utf-8")
    line = next(ln for ln in on_text.splitlines() if ln.startswith("recaps:"))
    assert line.startswith("recaps: true") and "export workflow" in line
    assert main(["--config", str(cfg), "recaps", "off"]) == int(Exit.OK)
    assert cfg.read_bytes() == original


# --- the runner layout, offline -------------------------------------------------------------

def _no_subprocess(*_a, **_k):
    raise AssertionError("the export workflow's commands must never run git")


def _runner_layout(tmp_path: Path) -> tuple[Path, Path]:
    """A synced data dir copied into a fresh `_data/data` the way actions/checkout lays out
    the data branch: every committed file, but no users.json (local only, never committed)."""
    (tmp_path / "synced").mkdir()
    cfg, synced = _bot_target_synced(tmp_path / "synced")
    data = tmp_path / "work" / "_data" / "data"
    data.mkdir(parents=True)
    for f in synced.iterdir():
        if f.is_file() and f.name != "users.json":
            shutil.copy2(f, data / f.name)
    assert not (data / "users.json").exists()
    return cfg, data


def _spy_export(monkeypatch) -> dict:
    seen: dict = {}
    real = cli.export_all

    def _spy(elig, roster, *rest, **kw):
        seen["is_bot"] = roster.entries[TARGET4].is_bot
        return real(elig, roster, *rest, **kw)

    monkeypatch.setattr(cli, "export_all", _spy)
    return seen


def test_roster_then_export_in_the_runner_layout(tmp_path, capsys, monkeypatch, fixed_clock):
    """The workflow's two commands in a fresh checkout: roster fills users.json with names and
    the rostered is_bot, export resolves names from it and judges the rostered bot as sync did,
    neither runs git, and nothing on stderr (the log) carries a display name."""
    cfg, data = _runner_layout(tmp_path)
    capsys.readouterr()
    slack = _world([], bot_target=True)
    seen = _spy_export(monkeypatch)
    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
    argv = ["--config", str(cfg), "--data-dir", str(data)]

    assert main(["roster", *argv], slack_factory=lambda: slack) == int(Exit.OK)
    roster_out = capsys.readouterr()
    assert "user-3" in roster_out.out            # names go to stdout, which the step discards
    out_dir = tmp_path / "work" / "exports"
    assert main(["export", "--out", str(out_dir), *argv]) == int(Exit.OK)
    export_out = capsys.readouterr()

    assert seen["is_bot"] is True
    files = sorted(p.name for p in out_dir.iterdir())
    assert "fall-2026.xlsx" in files and len([f for f in files if f.endswith(".csv")]) == 6
    csvs = "".join((out_dir / f).read_text(encoding="utf-8-sig") for f in files
                   if f.endswith(".csv"))
    assert "user-3" in csvs                      # names exist in the artifact
    for stream in (roster_out.err, export_out.err, export_out.out):
        assert not re.search(r"user-\d", stream), stream
    printed = export_out.out.splitlines()        # the only stdout export writes: its paths
    assert sorted(Path(ln).name for ln in printed) == files, printed
    assert all(Path(ln).parent == out_dir for ln in printed), printed
    assert not any(p.name == ".git" for p in (tmp_path / "work").rglob("*"))


def test_export_without_roster_is_the_positive_control(
        tmp_path, capsys, monkeypatch, fixed_clock):
    """Without the roster step a fresh checkout has no is_bot source: the rostered bot is
    judged a person. This is the gap the roster step (and its is_bot record) closes."""
    cfg, data = _runner_layout(tmp_path)
    seen = _spy_export(monkeypatch)
    out_dir = tmp_path / "work" / "exports"
    assert main(["export", "--out", str(out_dir),
                 "--config", str(cfg), "--data-dir", str(data)]) == int(Exit.OK)
    assert seen["is_bot"] is False


@pytest.mark.parametrize("cached", [{}, {TARGET4: "user-4"}])
def test_roster_records_is_bot_over_any_prior_cache(tmp_path, capsys, fixed_clock, cached):
    """roster rewrites users.json but keeps an is_bot entry for every rostered user, so a
    cache written by roster alone still marks the rostered bot (E-W4-18)."""
    import json

    cfg, data = _runner_layout(tmp_path)
    if cached:
        (data / "users.json").write_text(json.dumps(cached), encoding="utf-8")
    slack = _world([], bot_target=True)
    assert main(["roster", "--config", str(cfg), "--data-dir", str(data)],
                slack_factory=lambda: slack) == int(Exit.OK)
    cache = json.loads((data / "users.json").read_text(encoding="utf-8"))
    assert cache[TARGET4] == {"is_bot": True}
    assert cli._cached_is_bot(_ns(data))[TARGET4] is True
    assert cli._users_cache(_ns(data))[TARGET3] == "user-3"
    assert CHANNEL not in json.dumps(cache)


def _ns(data: Path):
    import argparse

    return argparse.Namespace(data_dir=str(data))
