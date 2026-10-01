"""Wave-4 rulings on the operator documents: README.md and config.example.yaml.

E-W4-26 (thread replies), E-W4-36 (the README "Deploying" section) and the emoji-name note
of E-W4-7/E-W4-36. Offline: the one git test runs against repos under tmp_path only.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
from pathlib import Path

import yaml

from snipebot.cli import _build_parser
from snipebot.config import load_config

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"


def _readme() -> str:
    return (REPO / "README.md").read_text(encoding="utf-8")


def _example_text() -> str:
    return (REPO / "config.example.yaml").read_text(encoding="utf-8")


def _deploying() -> str:
    section = _readme().split("## Deploying", 1)[1]
    return re.split(r"\n## ", section, maxsplit=1)[0]


def _blocks(text: str) -> list[list[str]]:
    return [
        [ln.strip() for ln in block.splitlines() if ln.strip()]
        for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S)
    ]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    assert proc.returncode == 0, f"{' '.join(args)} failed: {proc.stderr or proc.stdout!r}"
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", "-c", "user.name=seed",
                 "-c", "user.email=seed@fixture.invalid", *args], cwd=cwd)


# --- E-W4-26: thread replies ----------------------------------------------------------------

def test_example_documents_count_thread_replies_covers_broadcast_only():
    """E-W4-26: conversations.history never returns a thread reply that was not also sent to
    the channel, so `count_thread_replies` reaches broadcast replies only, and the example
    config says so next to the key."""
    lines = _example_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("count_thread_replies:"))
    note = [lines[start]]
    for ln in lines[start + 1:]:
        if not ln.strip().startswith("#"):
            break
        note.append(ln)
    text = _flat(" ".join(note)).lower()
    assert "also sent to the channel" in text, text
    assert "never" in text and "thread replies" in text, text


def test_readme_says_a_snipe_is_posted_in_the_channel_not_a_thread():
    """E-W4-26: the README tells players a snipe must be posted in the channel itself, not
    inside a thread."""
    text = _flat(_readme()).lower()
    assert "must be posted in the channel itself, not inside a thread" in text


def test_example_config_loads_and_equals_the_defaults(tmp_path):
    """E-W4-26: the documentation edits change comments only. The example still loads, and
    every setting it spells out equals the default load_config fills in when the key is
    left out (only the required keys and the placeholder roster are carried over). The one
    deliberate departure is `recaps`: the code default stays true, the example ships false
    (40 §1.1, E-W4-39)."""
    example = load_config(REPO / "config.example.yaml")
    assert example.recaps is False
    example = dataclasses.replace(example, recaps=True)
    raw = yaml.safe_load(_example_text())
    minimal = {key: raw[key] for key in ("slack", "timezone", "semesters", "players")}
    minimal.update(rules={}, feedback={}, consent={"veto": {"emoji": "x"}})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(minimal, sort_keys=False), encoding="utf-8")
    defaults = load_config(path)
    diff = [f.name for f in dataclasses.fields(example)
            if getattr(example, f.name) != getattr(defaults, f.name)]
    assert not diff, f"config.example.yaml departs from the defaults in: {diff}"


# --- E-W4-36: the Deploying section ---------------------------------------------------------

def test_readme_names_every_secret_the_workflows_read():
    """E-W4-36: every `secrets.<NAME>` a workflow reads is named in the README's Deploying
    section, so the operator knows what to add."""
    names = set()
    for wf in WORKFLOWS.glob("*.yml"):
        names |= set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)",
                                wf.read_text(encoding="utf-8")))
    assert names, "no workflow secrets found"
    section = _deploying()
    missing = sorted(n for n in names if n not in section)
    assert not missing, f"README Deploying never names: {missing}"


def test_readme_deploying_is_a_numbered_list_covering_every_step():
    """E-W4-36: the Deploying section is a numbered list and covers the orphan `data` branch
    with its `data/` directory, the SLACK_BOT_TOKEN secret, a private repo holding the real
    config.yaml, read-and-write workflow permissions, no force-push protection on `data`,
    `doctor`, and the go-live backfill with --no-react --no-post."""
    section = _deploying()
    steps = re.findall(r"^(\d+)\. ", section, flags=re.M)
    assert steps and steps == [str(i) for i in range(1, len(steps) + 1)], steps
    text = _flat(section)
    for needle in ("--orphan data", "data/", "SLACK_BOT_TOKEN", "private", "config.yaml",
                   '"Workflow permissions"', "Read and write permissions", "force-push",
                   "python -m snipebot doctor"):
        assert needle in text, needle
    backfill = [ln for block in _blocks(section) for ln in block if "snipebot backfill" in ln]
    assert backfill and all("--no-react" in ln and "--no-post" in ln for ln in backfill)


def test_readme_operating_notes():
    """E-W4-36: the operating notes warn that the opt-out poster must not react (every reactor
    opts out) and that GitHub keeps purged commits until Support runs garbage collection."""
    text = _flat(_deploying()).lower()
    assert "must not react" in text and "opts out" in text
    assert "purge --rewrite-history" in text and "garbage collection" in text


def test_readme_deploy_commands_parse_and_need_no_shell_specific_syntax():
    """E-W4-36: the commands in the Deploying blocks run as written in PowerShell and bash:
    no placeholder (`<...>` is a redirection in both shells), no cmd.exe `set X=`, and every
    `python -m snipebot` line is accepted by the real parser."""
    lines = [ln for block in _blocks(_deploying()) for ln in block]
    assert lines
    assert not [ln for ln in lines if "<" in ln or ">" in ln], lines
    assert not [ln for ln in lines if re.match(r"set\s+[A-Z_]+=", ln)], lines
    for ln in lines:
        m = re.match(r"python -m snipebot\s+(.*)$", ln)
        if m:
            _build_parser().parse_args(m.group(1).split())


def test_readme_bootstrap_creates_the_data_directory(tmp_path):
    """E-W4-36: the README's first Deploying block, run command by command in a fresh clone,
    pushes an orphan `data` branch that holds the `data/` directory and nothing from `main`,
    and leaves the clone back on a clean `main`."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", str(origin), str(seed)])
    (seed / "code.txt").write_text("code\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "code")
    _git(seed, "push", "-u", "origin", "main")
    for line in _blocks(_deploying())[0]:
        argv = [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', line)]
        assert argv[0] == "git", line
        _git(seed, *argv[1:])
    tree = _git(seed, "ls-tree", "-r", "--name-only", "origin/data").split()
    assert tree and all(name.startswith("data/") for name in tree), tree
    shared = subprocess.run(["git", "merge-base", "origin/main", "origin/data"], cwd=str(seed),
                            capture_output=True, text=True)
    assert shared.returncode == 1 and not shared.stdout.strip(), "data shares history with main"
    assert _git(seed, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert _git(seed, "status", "--porcelain").strip() == ""


# --- E-W4-7 / E-W4-36: emoji names ----------------------------------------------------------

def test_emoji_names_must_be_slack_canonical_names():
    """E-W4-7, E-W4-36: with the emoji doctor check removed, a wrong emoji name only surfaces
    as a step-7 `invalid_name` warning, so both documents tell the operator to use Slack's
    canonical emoji names."""
    readme = _flat(_deploying())
    assert "canonical names" in readme and "invalid_name" in readme
    assert "canonical name" in _flat(_example_text())
