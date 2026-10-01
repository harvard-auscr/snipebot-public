"""Wave-4 red-team, round 3, surface "deploy": the README's documented local run after the
round-2 repair of the data-branch protocol, and its bash variant on a POSIX host.

Every test here fails against the shipped files for the reason in its docstring. Git is only
ever run against tmp_path repositories (a bare origin plus clones); Slack is a FakeSlack.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from snipebot import cli
from snipebot.faces import FakeFaceDetector

REPO = Path(__file__).resolve().parents[3]


# --- helpers -------------------------------------------------------------------------------

def _run(args, cwd=None):
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout!r}")
    return proc.stdout


def _git(cwd, *args):
    return _run(["git", "-c", "core.autocrlf=false", "-c", "user.name=seed",
                 "-c", "user.email=seed@fixture.invalid", *args], cwd=cwd)


def _readme_section(heading: str) -> str:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    section = text.split(heading, 1)[1]
    return section.split("\n## ", 1)[0]


def _running_it_block() -> list[str]:
    block = re.search(r"```[^\n]*\n(.*?)```", _readme_section("## Running it"), flags=re.S)
    return [ln.rstrip() for ln in block.group(1).splitlines() if ln.strip()]


def _org_clone(tmp_path: Path) -> Path:
    """A deployed org repo (code on `main`, the `data` branch bootstrapped exactly as the
    README's deploy block says) and a fresh clone of it, as the README's reader has."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", str(origin), str(seed)])
    (seed / "code.txt").write_text("code\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "code")
    _git(seed, "branch", "-M", "main")
    _git(seed, "push", "-u", "origin", "main")
    deploy = re.search(r"```[^\n]*\n(.*?)```", _readme_section("## Deploying"), flags=re.S)
    for line in (ln.strip() for ln in deploy.group(1).splitlines() if ln.strip()):
        argv = [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', line)]
        assert argv[0] == "git", line
        _git(seed, *argv[1:])
    dev = tmp_path / "dev"
    _run(["git", "clone", "-b", "main", str(origin), str(dev)])
    return dev


# --- findings ------------------------------------------------------------------------------

def test_readme_quickstart_backfill_runs_as_written(tmp_path, monkeypatch):
    """The README's 'Running it' block cannot run its own data command. `cp
    config.example.yaml config.yaml` gives `persistence: git` (the example's default); the
    block names no SNIPEBOT_DATA_REPO, no data-branch clone or worktree and no --data-dir,
    and never says to switch to `persistence: files` for a local run. Since the round-2
    repair, GitStore.refresh() refuses the code clone the default `./data` maps to, so the
    documented `python -m snipebot backfill --from <semester> --dry-run` exits 1 with
    'unexpected error: GitCommandError: refresh: checkout is not dedicated to the data
    branch' right after a green `doctor`. Violates the deploy requirement that the README
    install steps work as written, and 40 section 6.1 (SNIPEBOT_DATA_REPO: on a box a
    separate clone or `git worktree` on the data branch), which the README never mentions."""
    from tests.test_cli import NOW_US, _config_dict, _world

    block = _running_it_block()
    section = _readme_section("## Running it")
    example = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    persistence = "files" if re.search(r"persistence:\s*files", section) else \
        example["persistence"]
    env = dict(re.findall(r"(SNIPEBOT_[A-Z_]+)\s*=\s*\"?([^\s\"]+)", "\n".join(block)))
    backfill = next(ln for ln in block if "snipebot backfill" in ln)
    argv = backfill.split("#", 1)[0].split("snipebot", 1)[1].split()
    argv = ["fall-2026" if a.startswith("<") else a for a in argv]

    dev = _org_clone(tmp_path)
    (dev / "config.yaml").write_text(
        yaml.safe_dump(_config_dict(persistence=persistence), sort_keys=False),
        encoding="utf-8")
    monkeypatch.chdir(dev)
    monkeypatch.delenv("SNIPEBOT_DATA_REPO", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    rc = cli.main(argv, slack_factory=_world, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0, (
        f"the README's documented `snipebot {' '.join(argv)}` exits {rc} in a fresh clone "
        f"(persistence: {persistence}, SNIPEBOT_* env {env or 'none'})")


