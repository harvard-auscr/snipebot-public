"""Red-team wave 2, round 1 (spec conformance) against snipebot/persistence.py.

Each test builds a real bare `origin` with a `data` branch and drives `GitStore`
exactly as `run_sync_git` would (helpers copied from tests/test_persistence.py so this
file stands alone). One test per finding; each must FAIL on the current code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from snipebot.ledger import count_moved_pairs
from snipebot.persistence import GitCommandError, GitStore, LeaseRejected


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


def _sync_message(day: str) -> str:
    """The exact commit message `snipebot.sync._commit_message` emits for a plain,
    non-large `sync` run: a header line then a single `rows +A -B` line, no per-reason
    body lines."""
    return f"sync {day}\n\nrows +0 -0\n"


def _verdict_line(ts: str, target: str, status: str, reason: str) -> str:
    """One canonical verdicts.jsonl line (00-data §4 shape used by ledger._index_pairs)."""
    return (
        '{"ts":"%s","status":"OK","selfie":"NONE",'
        '"pairs":[{"target":"%s","status":"%s","reason":"%s",'
        '"blocked_by":null,"selfie":false}]}' % (ts, target, status, reason)
    )


# --------------------------------------------------------------------------- findings


def test_history_moved_pairs_from_verdicts_not_message(tmp_path):
    """40 §4.2 (`history`): "Deltas are computed by diffing each commit's
    `verdicts.jsonl` against the previous sealed commit's (`count_moved_pairs`,
    `00-data.md` §4) — never by re-judging a baseline ledger". The real sync commit
    message carries only `rows +A -B` (sync._commit_message), so a `HistoryEntry` whose
    verdicts genuinely moved one pair must still report that move; deriving the count
    from the message text yields 0.
    """
    store, repo, origin = _repo_under_test(tmp_path)

    v1 = _verdict_line("1000.000001", "U0TARGET", "COUNTED", "OK") + "\n"
    v2 = _verdict_line("1000.000001", "U0TARGET", "NOT_COUNTED", "VETOED") + "\n"

    store.refresh()
    _write_data(repo, verdicts=v1)
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_sync_message("2026-09-22"), boundary=_noop)

    store.refresh()
    _write_data(repo, verdicts=v2)
    store.commit_and_push(local_day="2026-09-23", large_movement=False,
                          message=_sync_message("2026-09-23"), boundary=_noop)

    expected = count_moved_pairs(v1, v2)      # the §4.2 measure: exactly one pair moved
    assert expected == 1
    newest = store.history()[0]               # git log is newest-first: the 09-23 commit
    assert newest.day == "2026-09-23"
    assert newest.moved_pairs == expected


def test_branch_protection_rejection_is_not_a_lease_loss(tmp_path):
    """20 §8.3 step 6 / `LeaseRejected`: a rejected lease means "another run pushed
    first". A server-policy rejection (a protected/`denyNonFastForwards` data branch)
    fails with `! [remote rejected] ... (non-fast-forward)` even though our lease matched
    the tip — no other runner advanced it — so it is a non-lease push failure
    (`GitCommandError`), not a retryable `LeaseRejected`. Keying detection on the generic
    substring "rejected" instead of the lease's "stale info" misclassifies it and drives
    the concurrency retry to exit 9.
    """
    store, repo, origin = _repo_under_test(tmp_path)
    _git(origin, "config", "receive.denyNonFastForwards", "true")

    # First run of the day: a NEW commit on top of the seed -> a fast-forward push, allowed.
    store.refresh()
    _write_data(repo, verdicts="v0\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_sync_message("2026-09-22"), boundary=_noop)

    # Second run of the same day: AMEND -> a non-fast-forward push. Our lease still matches
    # origin/data (we are the only pusher), so this is purely the server's policy rejection.
    store.refresh()
    _write_data(repo, verdicts="v1\n")
    with pytest.raises(GitCommandError):
        store.commit_and_push(local_day="2026-09-22", large_movement=False,
                              message=_sync_message("2026-09-22"), boundary=_noop)
