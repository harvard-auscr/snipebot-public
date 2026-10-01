"""L3 git-persistence crash matrix (50 §5, 20 §2.2 + §8.3): the git commit/push
boundaries and a mid-run lease rejection, which the files-backed crash matrix
(``tests/test_crash_matrix.py``) cannot reach.

The files backend has nothing to commit, so its crash matrix never fires the four
git boundaries (``before_commit`` .. ``after_push``) nor a lease rejection. This
module runs the real ``sync`` under ``persistence: git`` against a tmp bare origin +
a data-repo clone (``SNIPEBOT_DATA_REPO``, exactly as ``test_persistence.py`` builds
one), kills it at each git boundary in a subprocess, and asserts a clean
``run_sync_git`` re-run converges: the data branch holds **exactly one commit for the
local day**, its committed tree is byte-identical to an uninterrupted control run,
local and origin tips agree (no dangling amend left unpushed), and no user ID ever
reaches a commit message (20 §8.4).

Which boundaries fire: ``GitStore.commit_and_push`` fires all four §2.2 git-persistence
boundaries -- ``before_commit`` / ``after_commit`` around the ``git commit`` and
``before_push`` / ``after_push`` around the push (§8.3 steps 4-5). The matrix
parametrises over all four; the convergence invariant must hold whether the process
died before the commit, between commit and push, or mid-push, and ``_FIRES`` records
that each of the four actually kills the process.

The mid-run lease rejection (20 §8.3 step 6, §1.4) is driven the way §5.3 describes:
a competing clone pushes first, *during our run*, injected at our own ``before_push``
(after ``run_sync_git``'s refresh pinned the lease) so the ``--force-with-lease`` is
rejected exactly once; ``run_sync_git`` refreshes to the winning tip and re-runs the
whole sync, which lands on top -- one commit per day preserved, ours recomputed to the
control tree.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

import snipebot.sync as sync_mod
from snipebot.config import load_config
from snipebot.faces import FakeFaceDetector
from snipebot.sync import run_sync_git
from snipebot.ts import parse_ts

from tests.fake_slack import FileBackedFakeSlack

# The world, config and face map are the files-matrix ones; only `persistence` differs.
from tests.test_crash_matrix import (
    FACES_MAP,
    NOW,
    _CONFIG_YAML,
    _author_world,
)

AUTHOR = "Test Bot <bot@example.com>"
DAY = "2026-09-18"                       # local day of NOW under UTC (matches the world)
PRIOR_DAY = "2026-09-17"                 # the competing pusher's day (§5.3 retry test)
U_ID_RE = re.compile(r"\bU[A-Z0-9]{7,}\b")

# GitStore.commit_and_push fires all four §2.2 git-persistence boundaries: before_commit /
# after_commit around the `git commit` (20 §2.2) and before_push / after_push around the push
# (§8.3 steps 4-5). A crash armed at any of them kills the process before the run completes.
_GIT_BOUNDARIES = ["before_commit", "after_commit", "before_push", "after_push"]
_FIRES = {"before_commit", "after_commit", "before_push", "after_push"}

_GIT_CONFIG_YAML = _CONFIG_YAML.replace("persistence: files", "persistence: git")


# --- git plumbing (a tmp bare origin + clones, as in test_persistence.py) -----

def _run(args: list[str], cwd: Path | None = None, *, text: bool = True):
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=text,
    )
    if proc.returncode != 0:
        out = proc.stderr or proc.stdout
        raise AssertionError(f"{' '.join(args)} failed ({proc.returncode}): {out!r}")
    return proc.stdout


def _git(cwd: Path, *args: str, text: bool = True):
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, text=text)


def _make_origin(tmp_path: Path) -> Path:
    """A bare origin whose `data` branch has one initial (non-tool) commit."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "data", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", str(origin), str(seed)])
    (seed / "data").mkdir(exist_ok=True)
    (seed / "data" / "README").write_text("data branch\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=seed", "-c", "user.email=seed@example.com",
         "commit", "-m", "init data branch")
    _git(seed, "branch", "-M", "data")
    _git(seed, "push", "-u", "origin", "data")
    return origin


def _clone(origin: Path, dest: Path) -> Path:
    _run(["git", "clone", str(origin), str(dest)])
    _git(dest, "checkout", "data")
    (dest / "data").mkdir(exist_ok=True)      # the data dir sync writes its files into
    return dest


def _subjects(repo: Path) -> list[str]:
    out = _git(repo, "log", "data", "--format=%s")
    return [line for line in out.splitlines() if line.strip()]


def _rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).strip()


def _show(repo: Path, ref: str, path: str) -> bytes:
    return _git(repo, "show", f"{ref}:{path}", text=False)


# --- driving the real sync ----------------------------------------------------

@contextmanager
def _git_env(repo: Path):
    """`store_for` reads SNIPEBOT_DATA_REPO; GitStore reads SNIPEBOT_GIT_AUTHOR."""
    prev = {k: os.environ.get(k) for k in ("SNIPEBOT_DATA_REPO", "SNIPEBOT_GIT_AUTHOR")}
    os.environ["SNIPEBOT_DATA_REPO"] = str(repo)
    os.environ["SNIPEBOT_GIT_AUTHOR"] = AUTHOR
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run_git_inproc(cfg_path: Path, world: Path, repo: Path):
    """A full `run_sync_git` (refresh -> sync -> commit + push, with the lease retry)."""
    cfg = load_config(str(cfg_path))
    slack = FileBackedFakeSlack(path=str(world))
    data = repo / "data"
    with _git_env(repo):
        return run_sync_git(
            slack, cfg, detector=FakeFaceDetector(dict(FACES_MAP)),
            ledger_path=data / "ledger.jsonl", state_path=data / "state.json",
            now_us=parse_ts(NOW), no_post=True,
        )


def _run_entry_git(cfg_path: Path, world: Path, repo: Path, *, crash_at: str) -> int:
    """Launch `python -m tests._fake_entry` (a single `run_sync`) under git persistence,
    armed to die at `crash_at`; return its exit code."""
    root = Path(__file__).resolve().parents[1]
    data = repo / "data"
    env = dict(os.environ)
    env.update(
        SNIPEBOT_FAKE_SLACK=str(world),
        SNIPEBOT_CONFIG=str(cfg_path),
        SNIPEBOT_FAKE_FACES=json.dumps(FACES_MAP),
        SNIPEBOT_LEDGER=str(data / "ledger.jsonl"),
        SNIPEBOT_STATE=str(data / "state.json"),
        SNIPEBOT_NOW_US=str(parse_ts(NOW)),
        SNIPEBOT_CRASH_AT=crash_at,
        SNIPEBOT_DATA_REPO=str(repo),
        SNIPEBOT_GIT_AUTHOR=AUTHOR,
        SNIPEBOT_NO_POST="1",
        PYTHONPATH=str(root),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "tests._fake_entry"],
        cwd=str(root), env=env, capture_output=True, timeout=120,
    )
    return proc.returncode


def _write_world(path: Path) -> None:
    path.write_text(
        json.dumps(_author_world(), ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )


# --- fixtures -----------------------------------------------------------------

@dataclass(frozen=True)
class GitKit:
    cfg_path: Path


@dataclass(frozen=True)
class Control:
    ledger: bytes
    verdicts: bytes


@pytest.fixture(scope="module")
def git_kit(tmp_path_factory) -> GitKit:
    base = tmp_path_factory.mktemp("git_crash_base")
    cfg = base / "config.yaml"
    cfg.write_text(_GIT_CONFIG_YAML, encoding="utf-8")
    return GitKit(cfg_path=cfg)


@pytest.fixture(scope="module")
def control(git_kit, tmp_path_factory) -> Control:
    """An uninterrupted git-persisted run: same world, same `now`. Its committed data
    files are the byte-for-byte target every crash-then-converge run must reach."""
    d = tmp_path_factory.mktemp("git_control")
    origin = _make_origin(d)
    repo = _clone(origin, d / "repo")
    world = d / "world.json"
    _write_world(world)
    result = _run_git_inproc(git_kit.cfg_path, world, repo)
    assert result.exit_code == 0
    assert _subjects(origin) == [f"sync {DAY}", "init data branch"]
    return Control(
        ledger=_show(origin, "data", "data/ledger.jsonl"),
        verdicts=_show(origin, "data", "data/verdicts.jsonl"),
    )


def _assert_one_commit_per_day_at_control(repo: Path, origin: Path, control: Control) -> None:
    subjects = _subjects(origin)
    # C: exactly one tool commit for the day (no dangling amend, no duplicate).
    assert subjects == [f"sync {DAY}", "init data branch"]
    assert subjects.count(f"sync {DAY}") == 1
    # local tip == origin tip: the amend/commit was pushed, nothing left unpushed.
    assert _rev(repo, "refs/heads/data") == _rev(origin, "data")
    # committed tree is byte-identical to the uninterrupted control (C1).
    assert _show(origin, "data", "data/ledger.jsonl") == control.ledger
    assert _show(origin, "data", "data/verdicts.jsonl") == control.verdicts
    assert (repo / "data" / "ledger.jsonl").read_bytes() == control.ledger
    assert (repo / "data" / "verdicts.jsonl").read_bytes() == control.verdicts
    # IDs only: no user ID reaches any commit message (20 §8.4).
    assert not U_ID_RE.search(_git(origin, "log", "data", "--format=%B"))


# --- the git crash matrix -----------------------------------------------------

@pytest.mark.parametrize("boundary", _GIT_BOUNDARIES)
def test_crash_at_git_boundary_converges(git_kit, control, tmp_path, boundary):
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    world = tmp_path / "world.json"
    _write_world(world)

    # 1) crash the real sync at the boundary. All four git-persistence boundaries fire
    #    (§2.2), so the armed crash kills the process at each.
    rc = _run_entry_git(git_kit.cfg_path, world, repo, crash_at=boundary)
    if boundary in _FIRES:
        assert rc == 137, f"boundary {boundary!r} did not fire (exit {rc})"
    else:
        assert rc == 0, f"boundary {boundary!r} unexpectedly changed the exit ({rc})"

    # 2) a clean run_sync_git refreshes to the origin tip -- discarding any local unpushed
    #    commit left by a before_push kill -- and drives the sync to completion.
    result = _run_git_inproc(git_kit.cfg_path, world, repo)
    assert result.exit_code == 0

    _assert_one_commit_per_day_at_control(repo, origin, control)


# --- mid-run lease rejection: refresh + retry lands (20 §8.3 step 6, §1.4) -----

def test_second_pusher_mid_run_triggers_retry_and_lands(git_kit, control, tmp_path, monkeypatch):
    """A competing clone pushes a prior-day commit *during* our run -- injected at our own
    `before_push`, after `run_sync_git`'s refresh pinned the lease -- so the first
    `--force-with-lease` is rejected. `run_sync_git` refreshes to the winning tip and
    re-runs the whole sync, which lands on top: one commit per day preserved, ours
    recomputed to the control tree, and the (channel, period_key) never double-posted."""
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    world = tmp_path / "world.json"
    _write_world(world)

    # A competing clone with a prior-day tool commit staged locally, not yet pushed.
    other = _clone(origin, tmp_path / "other")
    (other / "data" / "README").write_text("advanced by other\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "-c", "user.name=other", "-c", "user.email=other@example.com",
         "commit", "-m", f"sync {PRIOR_DAY}")

    # Fire the competitor's push exactly once, at OUR before_push, so the lease is stale.
    state = {"pushed": False}

    def hook(name, key=None):
        if name == "before_push" and not state["pushed"]:
            state["pushed"] = True
            _git(other, "push", "origin", "data")

    monkeypatch.setattr(sync_mod, "_boundary", hook)

    result = _run_git_inproc(git_kit.cfg_path, world, repo)
    assert result.exit_code == 0
    assert state["pushed"], "the competing push was never injected"

    # Converged: our day landed on top of the winner's day; one commit per day, no duplicate.
    subjects = _subjects(origin)
    assert subjects == [f"sync {DAY}", f"sync {PRIOR_DAY}", "init data branch"]
    assert subjects.count(f"sync {DAY}") == 1
    assert subjects.count(f"sync {PRIOR_DAY}") == 1

    # The retry re-ran the whole sync on the new tip -> our committed tree == control.
    assert _rev(repo, "refs/heads/data") == _rev(origin, "data")
    assert _show(origin, "data", "data/ledger.jsonl") == control.ledger
    assert _show(origin, "data", "data/verdicts.jsonl") == control.verdicts
    assert not U_ID_RE.search(_git(origin, "log", "data", "--format=%B"))


# --- positive control: a bare --force would let the loser clobber the winner ---

def test_positive_control_bare_force_would_lose_the_winners_commit(git_kit, control, tmp_path,
                                                                   monkeypatch):
    """C's convergence rests on `--force-with-lease` rejecting a stale push (20 §8.3 step 4).
    Swap it for a bare `--force` and the same competing-push race clobbers the winner's
    commit instead of retrying: the prior-day commit vanishes from the branch. This proves
    the retry assertion has teeth; the shipped push never uses a bare force."""
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    world = tmp_path / "world.json"
    _write_world(world)

    other = _clone(origin, tmp_path / "other")
    (other / "data" / "README").write_text("advanced by other\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "-c", "user.name=other", "-c", "user.email=other@example.com",
         "commit", "-m", f"sync {PRIOR_DAY}")

    state = {"pushed": False}

    def hook(name, key=None):
        if name == "before_push" and not state["pushed"]:
            state["pushed"] = True
            _git(other, "push", "origin", "data")

    monkeypatch.setattr(sync_mod, "_boundary", hook)

    # Rewrite the store's push to a bare --force (the very thing the protocol forbids).
    import snipebot.persistence as persistence
    real_run = persistence.subprocess.run

    def forcing_run(args, *a, **k):
        if isinstance(args, list) and "push" in args and "--force-with-lease" in args:
            args = ["--force" if x == "--force-with-lease" else x for x in args]
        return real_run(args, *a, **k)

    monkeypatch.setattr(persistence.subprocess, "run", forcing_run)

    result = _run_git_inproc(git_kit.cfg_path, world, repo)
    assert result.exit_code == 0
    assert state["pushed"]

    # Under a bare force there is no lease, no rejection, no retry: our single commit
    # overwrites the branch and the winner's prior-day commit is gone -- convergence broken.
    subjects = _subjects(origin)
    assert f"sync {PRIOR_DAY}" not in subjects
    assert subjects == [f"sync {DAY}", "init data branch"]
