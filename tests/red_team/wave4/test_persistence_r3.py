"""Red team, wave 4, round 3: the git-backed `data` branch (snipebot/persistence.py).

Real git only, against a bare `origin` and clones under tmp_path; never a remote host.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from snipebot.persistence import GitStore

AUTHOR = "snipebot-test <snipebot-test@example.invalid>"
DAY = "2026-09-18"


# --------------------------------------------------------------------------- git helpers


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True)
    if proc.returncode != 0:
        raise AssertionError(f"{args[:3]} failed ({proc.returncode})")
    return proc


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd).stdout.decode("utf-8")


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "-c", "user.name=seed", "-c", "user.email=seed@example.invalid",
         "commit", "-q", "--allow-empty", "-m", message)


def _write_data(repo: Path, marker: str) -> None:
    d = repo / "data"
    d.mkdir(exist_ok=True)
    (d / "ledger.jsonl").write_bytes(f"{marker}-ledger\n".encode())
    (d / "verdicts.jsonl").write_bytes(f"{marker}-verdicts\n".encode())
    (d / "state.json").write_bytes(f'{{"m":"{marker}"}}'.encode())


def _make_origin(base: Path, days: tuple[str, ...] = ()) -> Path:
    """A bare origin whose `data` branch has an initial non-tool commit plus one daily
    tool commit (with the three data files) per entry of `days`."""
    origin = base / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", "data", str(origin)])
    seed = base / "seed"
    _run(["git", "init", "-q", "-b", "data", str(seed)])
    _git(seed, "remote", "add", "origin", str(origin))
    (seed / "data").mkdir()
    (seed / "data" / "README").write_bytes(b"data branch\n")
    _git(seed, "add", "-A")
    _commit(seed, "init data branch")
    for day in days:
        _write_data(seed, day)
        _git(seed, "add", "-A")
        _commit(seed, f"sync {day}\n\nrows +0 -0\n")
    _git(seed, "push", "-q", "origin", "data")
    return origin


def _count(origin: Path, ref: str = "data") -> int:
    return int(_git(origin, "rev-list", "--count", ref).strip())


def _noop(_stage: str) -> None:
    return None


def _store(repo: Path) -> GitStore:
    store = GitStore(repo, author=AUTHOR)
    store.data_dir = repo / "data"
    return store


# --------------------------------------------------------------------------- findings


def test_refresh_crashes_when_the_checkout_path_is_not_ascii(tmp_path):
    """_ensure_dedicated_checkout reads `git rev-parse --show-toplevel` in text mode, which
    decodes git's UTF-8 path with the locale code page. On a Windows box (cp1252) a data
    checkout under a folder holding a character such as 'Á' (UTF-8 C3 81; 0x81 is undefined in
    cp1252) kills subprocess's reader thread, stdout comes back None, and refresh() dies with
    AttributeError: every sync on that box exits 1 before step 1 (20 §8.3 step 1, 40 §6.1
    'on the box a separate clone'). Git output that carries paths must be read as UTF-8
    bytes."""
    base = tmp_path / "snipes-Á"
    base.mkdir()
    origin = _make_origin(base, (DAY,))
    clone = base / "data-clone"
    _run(["git", "clone", "-q", "--branch", "data", str(origin), str(clone)])
    store = _store(clone)
    store.refresh()
    assert _git(clone, "symbolic-ref", "--short", "HEAD").strip() == "data"


def test_restore_of_the_tip_snapshot_makes_an_empty_movement_commit(tmp_path):
    """When the staged three files are byte-identical to the tip, 20 §8.3 step 3 says 'the
    run makes no commit' and 40 §4.3 says 'no change ... makes no commit, under both
    persistence modes'. `restore --from <tip>` (or a purge that removes no row) reaches
    commit_and_push(large_movement=True) with nothing staged, and GitStore commits
    `--allow-empty` anyway: an empty `[movement:admin]` commit is pushed, it becomes the new
    sealed baseline, and the CLI prints 'movement <sha>' / 'pushed data' instead of its
    'no change' branch (which only runs when CommitResult.sha is empty)."""
    origin = _make_origin(tmp_path, (DAY,))
    clone = tmp_path / "clone"
    _run(["git", "clone", "-q", "--branch", "data", str(origin), str(clone)])
    store = _store(clone)
    store.refresh()
    before = _count(origin)
    tip = _git(clone, "rev-parse", "HEAD").strip()
    store.restore(tip)
    result = store.commit_and_push(
        local_day=DAY, large_movement=True,
        message=f"restore {DAY} [movement:admin]\n\nrows +0 -0\n", boundary=_noop,
    )
    assert _count(origin) == before, "an empty movement commit was pushed"
    assert not result.sha
