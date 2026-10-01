"""Wave-4 red-team, round 2, surface "deploy": the README's documented local run, the admin
workflow's shell and the fresh-org data-branch bootstrap.

Every test here fails against the shipped files for the reason in its docstring. Git is only
ever run against tmp_path repositories (a bare origin plus clones); Slack is a FakeSlack.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.faces import FakeFaceDetector

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"


# --- helpers -------------------------------------------------------------------------------

def _run(args, cwd=None):
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout!r}")
    return proc.stdout


def _git(cwd, *args):
    return _run(["git", "-c", "core.autocrlf=false", "-c", "user.name=seed",
                 "-c", "user.email=seed@fixture.invalid", *args], cwd=cwd)


def _readme_bootstrap_lines() -> list[str]:
    """The git commands of the README's 'Deploying (one-time setup)' block."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    section = text.split("## Deploying", 1)[1]
    block = re.search(r"```[^\n]*\n(.*?)```", section, flags=re.S).group(1)
    return [ln.strip() for ln in block.splitlines() if ln.strip()]


def _org_with_data_branch(tmp_path: Path) -> Path:
    """A fresh org remote: the code on `main` (with the repo's real .gitignore) and the `data`
    branch created exactly as the README's deploy block says."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", str(origin), str(seed)])
    (seed / "code.txt").write_text("code\n", encoding="utf-8")
    (seed / ".gitignore").write_text((REPO / ".gitignore").read_text(encoding="utf-8"),
                                     encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "code")
    _git(seed, "branch", "-M", "main")
    _git(seed, "push", "-u", "origin", "main")
    for line in _readme_bootstrap_lines():
        argv = [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', line)]
        assert argv[0] == "git", line
        _git(seed, *argv[1:])
    return origin


def _git_bash() -> str | None:
    """Git's own bash (on Windows `bash` on PATH may be a different shell entirely)."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            cand = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if cand.is_file():
                return str(cand)
        return None
    return shutil.which("bash")


def _admin_argv(inputs: dict[str, str], tmp_path: Path) -> list[str]:
    """Run admin.yml's 'Run admin command' script under bash exactly as the runner would (the
    step env bound from the dispatch inputs), with `python` stubbed to print its argv."""
    bash = _git_bash()
    if bash is None:
        pytest.skip("no bash to execute the workflow script")
    doc = yaml.safe_load((WORKFLOWS / "admin.yml").read_text(encoding="utf-8"))
    step = next(s for s in doc["jobs"]["admin"]["steps"] if s.get("name") == "Run admin command")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLACK_")}
    for key, value in step["env"].items():
        m = re.fullmatch(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", str(value))
        if m:
            env[str(key)] = inputs.get(m.group(1), "")
    script = tmp_path / "step.sh"
    script.write_text(
        "python() { for a in \"$@\"; do printf '%s\\037' \"$a\"; done; printf '\\n'; }\n"
        + str(step["run"]),
        encoding="utf-8", newline="\n",
    )
    proc = subprocess.run([bash, str(script)], env=env, capture_output=True, text=True,
                          cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    args = [a for a in proc.stdout.strip().split("\x1f") if a]
    assert args[:2] == ["-m", "snipebot"], args
    return args[2:]


# --- findings ------------------------------------------------------------------------------

def test_readme_quickstart_dry_run_wipes_the_code_clone(tmp_path, monkeypatch):
    """Following the README's 'Running it' block in a clone of a deployed repo destroys the
    clone. `cp config.example.yaml config.yaml` gives `persistence: git` (the example's
    default), the README sets no SNIPEBOT_DATA_REPO and passes no --data-dir, so
    `store_for` maps the default `data` dir to "." -- the CODE clone. `backfill --dry-run`
    still goes through run_sync_git, whose first act is GitStore.refresh():
    `git checkout -f -B data origin/data` switches the code clone to the orphan data branch
    (every tracked file, the package included, vanishes; uncommitted edits are discarded),
    then `git clean -fd` deletes every untracked file -- the owner's edited config.yaml, and,
    with the code branch's .gitignore gone, .venv/, the local data/ and the real-id
    scrub_map.local.json. Violates 20 section 8.3 step 1 (the protocol runs only in "a
    checkout dedicated to the data branch ... so the protocol never switches branches under
    the running code"), 40 section 4.2 backfill `--dry-run` ("write nothing") and the deploy
    requirement that the README steps work as written."""
    from tests.test_cli import NOW_US, _config_dict, _world

    origin = _org_with_data_branch(tmp_path)
    dev = tmp_path / "dev"
    _run(["git", "clone", "-b", "main", str(origin), str(dev)])
    # README: `cp config.example.yaml config.yaml   # then edit` -- untracked, keeps git mode.
    (dev / "config.yaml").write_text(
        yaml.safe_dump(_config_dict(persistence="git"), sort_keys=False), encoding="utf-8")
    (dev / ".venv").mkdir()
    (dev / ".venv" / "marker").write_text("venv\n", encoding="utf-8")

    monkeypatch.chdir(dev)
    monkeypatch.delenv("SNIPEBOT_DATA_REPO", raising=False)
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cli.main(["backfill", "--from", "fall-2026", "--dry-run"],
             slack_factory=_world, detector_factory=lambda: FakeFaceDetector({}))

    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=dev).strip()
    survivors = sorted(p.name for p in dev.iterdir() if p.name != ".git")
    assert branch == "main" and (dev / "config.yaml").is_file() and (dev / "code.txt").is_file() \
        and (dev / ".venv" / "marker").is_file(), (
        f"a README dry run left the code clone on {branch!r} holding only {survivors}")


def test_admin_user_input_is_word_split_into_extra_cli_flags(tmp_path):
    """admin.yml builds `BY="--by $USER"` and expands it unquoted (`veto --ts "$TS" $BY`), so
    the free-text `user` dispatch input is word-split and glob-expanded into separate CLI
    arguments. A `user` of "U0AAA009 --ts 1790000000.000002" vetoes message ...002 although
    the admin typed ts ...001 (argparse keeps the last --ts), and the CLI's UserID check
    never sees the bad input. Violates 40 section 7.2: inputs reach the shell only through
    env indirection and the CLI re-validates each (`--user` against UserID, a malformed
    input fails with exit 2); the spec's script passes `--by` as one quoted array element
    (`BY=(--by "$USER_ID")`, `"${BY[@]}"`)."""
    argv = _admin_argv(
        {"command": "veto", "ts": "1790000000.000001",
         "user": "U0AAA009 --ts 1790000000.000002"},
        tmp_path,
    )
    assert argv.count("--ts") == 1 and "U0AAA009 --ts 1790000000.000002" in argv, (
        f"the user input was split into extra CLI arguments: {argv}")
