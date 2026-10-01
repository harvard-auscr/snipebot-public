"""Red-team wave 2, round 3 (invariants across runs) against snipebot/persistence.py.

The test builds a real bare `origin` with a `data` branch and drives `GitStore` exactly as
`run_sync_git` would (helpers stand alone here). Round-3 theme: convergence after a single
fault. One test per finding; it must FAIL on the current code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from snipebot.persistence import GitStore

# --------------------------------------------------------------------------- helpers


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr or proc.stdout}"
        )
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _make_origin(tmp_path: Path) -> Path:
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
    return dest


def _repo_under_test(tmp_path: Path) -> tuple[GitStore, Path, Path]:
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    return GitStore(repo, author="Test Bot <bot@example.com>"), repo, origin


def _write_data(repo: Path, *, verdicts: str = "", ledger: str = "", state: str = "{}") -> None:
    d = repo / "data"
    d.mkdir(exist_ok=True)
    (d / "verdicts.jsonl").write_bytes(verdicts.encode("utf-8"))
    (d / "ledger.jsonl").write_bytes(ledger.encode("utf-8"))
    (d / "state.json").write_bytes(state.encode("utf-8"))


def _noop(_stage: str) -> None:
    pass


def _msg(command: str, day: str) -> str:
    return f"{command} {day}\n\nrows +0 -0\n"


def _tree_files(repo: Path) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", "refs/heads/data")
    return [ln for ln in out.splitlines() if ln.strip()]


# --------------------------------------------------------------------------- findings


def test_atomic_write_tmp_residue_from_a_crash_is_committed(tmp_path):
    """20 §8.3 step 1: '... ensure the working tree matches the tip (this is the tree the
    run started from; §1.4 resets to it on a retry).'

    A hard crash (the §2.2 `SNIPEBOT_CRASH_AT` os._exit kill, or a power loss) between
    `ledger._atomic_write_text`'s `mkstemp(dir=data/, suffix='.tmp')` and its `os.replace`
    leaves an orphaned `data/<name>.<rand>.tmp` in the working tree. `refresh()`
    (`git checkout -f -B`) does NOT remove untracked files, and `commit_and_push` runs an
    unscoped `git add -A`, so the next run sweeps that residue into the data-branch commit:
    the tree never converges to the tip, and §8.4's 'never a ... file name' is violated by
    the orphan's own name landing in git. The tip's committed tree must hold only the three
    data files (plus the seed README), never the `.tmp` residue.
    """
    store, repo, origin = _repo_under_test(tmp_path)

    store.refresh()
    _write_data(repo, verdicts="v1\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_msg("sync", "2026-09-22"), boundary=_noop)

    # Residue a hard-killed save leaves behind: an orphaned atomic-write temp file in data/.
    (repo / "data" / "verdicts.jsonl.a1b2c3.tmp").write_bytes(b"partial")

    # Recovery run: refresh() is meant to reset the tree to the tip before anything is written.
    store.refresh()
    _write_data(repo, verdicts="v2\n")
    store.commit_and_push(local_day="2026-09-23", large_movement=False,
                          message=_msg("sync", "2026-09-23"), boundary=_noop)

    committed = _tree_files(repo)
    assert "data/verdicts.jsonl.a1b2c3.tmp" not in committed, (
        f"orphaned temp residue was committed into the data branch: {committed}"
    )
