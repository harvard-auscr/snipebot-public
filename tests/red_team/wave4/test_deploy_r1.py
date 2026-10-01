"""Wave-4 red-team, round 1, surface "deploy": prod readiness of the two Actions workflows,
the README, the Slack manifest and the fresh-repo bootstrap of the `data` branch.

Every test here fails against the shipped files for the reason in its docstring. Git is
only ever run against tmp_path repositories (a bare origin plus clones).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from snipebot import cli
from snipebot.config import Persistence
from snipebot.faces import FakeFaceDetector
from snipebot.persistence import store_for

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
DAY = "2026-09-25"


# --- helpers -------------------------------------------------------------------------------

def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout!r}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", "-c", "user.name=seed",
                 "-c", "user.email=seed@fixture.invalid", *args], cwd=cwd)


def _runner_layout(tmp_path: Path) -> tuple[Path, Path]:
    """The Actions runner layout both workflows build: the code branch checked out at the
    workspace root, the `data` branch checked out at `_data` (40 section 7.1)."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", str(origin), str(seed)])
    (seed / "code.txt").write_text("code\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "code")
    _git(seed, "branch", "-M", "main")
    _git(seed, "push", "-u", "origin", "main")
    _git(seed, "checkout", "--orphan", "data")
    _git(seed, "rm", "-rf", "--cached", ".")
    (seed / "code.txt").unlink()
    (seed / "data").mkdir()
    (seed / "data" / "README").write_text("data branch\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init data branch")
    _git(seed, "push", "-u", "origin", "data")
    workspace = tmp_path / "workspace"
    _run(["git", "clone", "-b", "main", str(origin), str(workspace)])
    _run(["git", "clone", "-b", "data", str(origin), str(workspace / "_data")])
    return origin, workspace


def _step_env(doc: dict, job: str, step_name: str, workspace: Path) -> tuple[dict, str, Path]:
    """The literal (non-secret, non-input) env a step runs with, its run text and its cwd."""
    step = next(s for s in doc["jobs"][job]["steps"] if s.get("name") == step_name)
    env: dict[str, str] = {}
    for layer in (doc.get("env") or {}, doc["jobs"][job].get("env") or {}, step.get("env") or {}):
        for key, value in layer.items():
            value = str(value).replace("${{ github.workspace }}", str(workspace))
            if "${{" not in value:
                env[str(key)] = value
    run = str(step["run"])
    for key, value in re.findall(r"(?:^|\s)(?:export\s+)?([A-Z_][A-Z0-9_]*)=(\S+)", run):
        if key.startswith("SNIPEBOT_"):
            env[key] = value.strip("\"'")
    cwd = workspace / step.get("working-directory", ".")
    return env, run, cwd


def _assert_step_persists_to_data_checkout(tmp_path, monkeypatch, workflow: str, job: str,
                                           step_name: str) -> None:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    origin, workspace = _runner_layout(tmp_path)
    env, run, cwd = _step_env(doc, job, step_name, workspace)
    data_dir = Path(re.search(r"--data-dir\s+(\S+)", run).group(1).strip("\"'"))
    monkeypatch.delenv("SNIPEBOT_DATA_REPO", raising=False)
    monkeypatch.delenv("SNIPEBOT_GIT_AUTHOR", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(cwd)

    store = store_for(SimpleNamespace(persistence=Persistence.GIT), data_dir)
    store.refresh()
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("ledger.jsonl", "verdicts.jsonl", "state.json"):
        (data_dir / name).write_text("", encoding="utf-8")
    store.commit_and_push(local_day=DAY, large_movement=False, message=f"sync {DAY}",
                          boundary=lambda _name: None)

    code_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace).strip()
    assert code_branch == "main", (
        f"the data-branch protocol ran in the CODE checkout and switched it to {code_branch!r}"
    )
    tree = _run(["git", "ls-tree", "-r", "--name-only", "data"], cwd=origin).split()
    assert "data/verdicts.jsonl" in tree and "_data" not in tree, tree


# --- findings ------------------------------------------------------------------------------

def test_sync_workflow_git_store_runs_in_code_checkout(tmp_path, monkeypatch):
    """sync.yml never sets SNIPEBOT_DATA_REPO, so `store_for` falls back to "." -- the CODE
    checkout at the workspace root, not the `_data` checkout the workflow made for the data
    branch. The first sync then `git checkout -f -B data origin/data` in the code tree
    (deleting the package under the running process), and `git add -A` there commits the
    nested `_data` repo as a gitlink and pushes that to the data branch instead of
    data/ledger.jsonl etc.: the ledger is never persisted and every run starts empty.
    Violates 20 section 8.3 step 1 ("a checkout dedicated to the data branch ... on Actions
    the workflow's checkout ... so the protocol never switches branches under the running
    code") and 40 section 6.1 (SNIPEBOT_DATA_REPO, default ".")."""
    _assert_step_persists_to_data_checkout(tmp_path, monkeypatch, "sync.yml", "sync", "Sync")


def test_admin_workflow_git_store_runs_in_code_checkout(tmp_path, monkeypatch):
    """admin.yml's "Run admin command" step likewise never sets SNIPEBOT_DATA_REPO, so veto,
    unveto, selfie, rejoin, accept-deletes, backfill, restore, reevaluate and purge all run
    the data-branch protocol in the code checkout: the admin's movement commit carries a
    `_data` gitlink instead of the data files and the code tree is switched to `data`.
    Violates 20 section 8.3 step 1 and 40 sections 6.1 / 7.2."""
    _assert_step_persists_to_data_checkout(
        tmp_path, monkeypatch, "admin.yml", "admin", "Run admin command"
    )


def test_first_sync_on_fresh_data_branch_creates_data_dir(tmp_path, monkeypatch):
    """On a fresh org repo the `data` branch starts with no data/ directory (git tracks no
    empty directories), so the workflow's `--data-dir _data/data` does not exist on the first
    run. `sync` never creates it: `_atomic_write_text` calls `tempfile.mkstemp(dir=...)` in
    the missing directory and the first prod sync exits 1 ("unexpected error:
    FileNotFoundError") instead of writing the first ledger. Deploy requirement: the data/ subdirectory is bootstrapped by
    the program (20 section 8.3 step 2 writes the three files into the tree; 40 section 3.3
    first run)."""
    from tests.test_cli import NOW_US, _world, _write_config

    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write_config(tmp_path)
    data = tmp_path / "_data" / "data"          # parent exists, data/ itself does not
    data.parent.mkdir()
    try:
        rc = cli.main(["sync", "--no-post", "--config", str(cfg), "--data-dir", str(data)],
                      slack_factory=_world, detector_factory=lambda: FakeFaceDetector({}))
    except FileNotFoundError as exc:
        pytest.fail(f"first sync crashed on the missing data dir: {exc.__class__.__name__}")
    assert rc == 0
    assert (data / "ledger.jsonl").exists()


def test_manifest_grants_every_scope_slack_io_calls():
    """slack_io's real transport calls `emoji.list` (DOC-EMOJI-EXISTS, 40 section 5.2), which
    needs the `emoji:read` bot scope; the manifest does not grant it, so every prod `doctor`
    gets `missing_scope` and DOC-EMOJI-EXISTS can never verify anything. Deploy requirement:
    the manifest scopes equal what slack_io actually calls (40 section 7.3)."""
    method_scope = {
        "auth_test": None,
        "conversations_history": "channels:history",
        "conversations_info": "channels:read",
        "conversations_members": "channels:read",
        "users_list": "users:read",
        "reactions_get": "reactions:read",
        "reactions_add": "reactions:write",
        "reactions_remove": "reactions:write",
        "chat_postMessage": "chat:write",
        "chat_update": "chat:write",
        "emoji_list": "emoji:read",
    }
    source = (REPO / "snipebot" / "slack_io.py").read_text(encoding="utf-8")
    called = set(re.findall(r"_(?:call|paged)\(\s*\"(\w+)\"", source))
    unmapped = called - set(method_scope)
    assert not unmapped, f"unmapped Web API methods: {sorted(unmapped)}"
    manifest = yaml.safe_load((REPO / "slack-app-manifest.yaml").read_text(encoding="utf-8"))
    granted = set(manifest["oauth_config"]["scopes"]["bot"])
    needed = {method_scope[m] for m in called} - {None}
    assert needed <= granted, f"scopes called but not granted: {sorted(needed - granted)}"


def _readme() -> str:
    return (REPO / "README.md").read_text(encoding="utf-8")


def _readme_code() -> list[str]:
    blocks = re.findall(r"```[^\n]*\n(.*?)```", _readme(), flags=re.S)
    return [line for block in blocks for line in block.splitlines()]


def test_readme_documents_data_branch_bootstrap():
    """Both workflows check out `ref: data` (40 sections 7.1/7.2) and GitStore.refresh runs
    `git fetch origin data`; on a fresh org repo that branch does not exist, so the first
    scheduled sync fails at "Checkout data branch". Nothing creates it and the README never
    says to: deploy requirement "the data branch exists, or its bootstrap (an orphan branch,
    first push) is documented" (40 section 7.1: `data` is an orphan branch)."""
    text = _readme().lower()
    assert "orphan" in text and "data" in text, "README never documents creating the data branch"


def test_readme_token_step_sets_no_env_in_powershell_or_bash():
    """README's `set SLACK_BOT_TOKEN=xoxb-...` is cmd.exe syntax only: in PowerShell `set` is
    Set-Variable (a shell variable literally named "SLACK_BOT_TOKEN=xoxb-...", no env var) and
    in bash it sets the positional parameters. Either way the next `python -m snipebot doctor`
    sees no token and fails DOC-AUTH. Deploy requirement: the README steps work as written in
    PowerShell and bash (40 section 6.1: the token comes from the environment)."""
    bad = [ln for ln in _readme_code() if re.match(r"\s*set\s+[A-Z_]+=", ln)]
    assert not bad, f"cmd.exe-only env assignment in README: {bad}"


def test_readme_runs_snipebot_with_the_venv_interpreter():
    """README installs the dependencies into .venv (`.venv/Scripts/pip install ...`) but then
    runs `python -m snipebot ...` with whatever `python` is on PATH, never activating the venv:
    that interpreter lacks slack_sdk/PyYAML, so `doctor` dies with ModuleNotFoundError in
    both PowerShell and bash. Deploy requirement: the README steps work as written."""
    lines = _readme_code()
    activates = any("activate" in ln for ln in lines)
    bare = [ln for ln in lines if re.match(r"\s*python\s+-m\s+snipebot\b", ln)]
    assert activates or not bare, f"snipebot run outside the venv: {bare}"
