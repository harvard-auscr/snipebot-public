"""Real-git tests for the data-branch backend (20 §8).

Each test builds a bare `origin` with an initial empty `data` branch, clones it as the repo
under test, and drives `GitStore` against it exactly as `run_sync_git` would.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from snipebot.config import Persistence
from snipebot.persistence import (
    CommitResult,
    FilesStore,
    GitStore,
    HistoryEntry,
    LeaseRejected,
    store_for,
)

U_ID_RE = re.compile(r"\bU[A-Z0-9]{7,}\b")


# --------------------------------------------------------------------------- helpers


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
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
    # write_bytes: no newline translation, so the blobs are LF-clean on every platform.
    (d / "verdicts.jsonl").write_bytes(verdicts.encode("utf-8"))
    (d / "ledger.jsonl").write_bytes(ledger.encode("utf-8"))
    (d / "state.json").write_bytes(state.encode("utf-8"))


def _msg(command: str, day: str, *, movement: str | None = None,
         rows: tuple[int, int] = (0, 0), reasons: dict[str, int] | None = None) -> str:
    header = f"{command} {day}"
    if movement:
        header += f" [movement:{movement}]"
    lines = [header, "", f"rows +{rows[0]} -{rows[1]}"]
    for name, delta in (reasons or {}).items():
        lines.append(f"{name} {'+' if delta >= 0 else '-'}{abs(delta)}")
    return "\n".join(lines)


def _noop(_stage: str) -> None:
    pass


def _verdict_line(ts: str, target: str, status: str, reason: str) -> str:
    """One canonical verdicts.jsonl line (00-data §4 shape used by ledger._index_pairs)."""
    return (
        '{"ts":"%s","status":"OK","selfie":"NONE",'
        '"pairs":[{"target":"%s","status":"%s","reason":"%s",'
        '"blocked_by":null,"selfie":false}]}' % (ts, target, status, reason)
    )


def _subjects(repo: Path) -> list[str]:
    out = _git(repo, "log", "refs/heads/data", "--format=%s")
    return [line for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------- tests


def test_two_runs_one_day_amend_into_one_commit(tmp_path):
    store, repo, origin = _repo_under_test(tmp_path)

    store.refresh()
    _write_data(repo, verdicts="v1\n")
    r1 = store.commit_and_push(
        local_day="2026-09-22", large_movement=False,
        message=_msg("sync", "2026-09-22"), boundary=_noop,
    )
    assert r1.pushed and not r1.amended and r1.sealed_sha is None

    store.refresh()
    _write_data(repo, verdicts="v2\n")
    r2 = store.commit_and_push(
        local_day="2026-09-22", large_movement=False,
        message=_msg("sync", "2026-09-22"), boundary=_noop,
    )
    assert r2.pushed and r2.amended

    # One tool commit for the day (plus the initial commit) and the amend kept the latest tree.
    subjects = _subjects(repo)
    assert subjects.count("sync 2026-09-22") == 1
    assert subjects == ["sync 2026-09-22", "init data branch"]
    assert _git(repo, "show", "HEAD:data/verdicts.jsonl") == "v2\n"
    # The amend landed on origin too.
    assert _git(origin, "show", "data:data/verdicts.jsonl") == "v2\n"


def test_next_day_starts_a_new_commit(tmp_path):
    store, repo, origin = _repo_under_test(tmp_path)

    store.refresh()
    _write_data(repo, verdicts="d1\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_msg("sync", "2026-09-22"), boundary=_noop)

    store.refresh()
    _write_data(repo, verdicts="d2\n")
    r = store.commit_and_push(local_day="2026-09-23", large_movement=False,
                              message=_msg("sync", "2026-09-23"), boundary=_noop)
    assert not r.amended
    assert _subjects(repo) == ["sync 2026-09-23", "sync 2026-09-22", "init data branch"]


def test_large_movement_seals_new_commit_and_keeps_previous_tip(tmp_path):
    store, repo, origin = _repo_under_test(tmp_path)

    store.refresh()
    _write_data(repo, verdicts="base\n")
    daily = store.commit_and_push(local_day="2026-09-22", large_movement=False,
                                  message=_msg("sync", "2026-09-22"), boundary=_noop)

    store.refresh()
    _write_data(repo, verdicts="moved\n")
    mv = store.commit_and_push(
        local_day="2026-09-22", large_movement=True,
        message=_msg("veto", "2026-09-22", movement="admin", reasons={"counted": 2}),
        boundary=_noop,
    )
    assert not mv.amended
    assert mv.sealed_sha == daily.sha            # the pre-movement restore point
    assert mv.sha != daily.sha
    # The sealed commit is the movement commit's parent and is still reachable intact.
    assert _git(repo, "rev-parse", f"{mv.sha}^").strip() == daily.sha
    assert _git(repo, "show", f"{daily.sha}:data/verdicts.jsonl") == "base\n"
    assert _subjects(repo) == [
        "veto 2026-09-22 [movement:admin]", "sync 2026-09-22", "init data branch",
    ]


def test_push_uses_force_with_lease_never_bare_force(tmp_path, monkeypatch):
    store, repo, origin = _repo_under_test(tmp_path)
    import snipebot.persistence as persistence

    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(args, *a, **k):
        calls.append(list(args))
        return real_run(args, *a, **k)

    monkeypatch.setattr(persistence.subprocess, "run", spy)

    store.refresh()
    _write_data(repo, verdicts="x\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_msg("sync", "2026-09-22"), boundary=_noop)

    push_calls = [c for c in calls if "push" in c]
    assert push_calls, "expected a push"
    assert all("--force-with-lease" in c for c in push_calls)
    # Never a bare --force anywhere.
    for c in calls:
        assert "--force" not in c


def test_second_pusher_wins_then_lease_rejected_and_refresh_lands_on_new_tip(tmp_path):
    store, repo, origin = _repo_under_test(tmp_path)

    store.refresh()                              # tracking pinned at the current origin tip

    # A second clone pushes first, advancing origin/data.
    other = _clone(origin, tmp_path / "other")
    _write_data(other, verdicts="other\n")
    _git(other, "add", "-A")
    _git(other, "-c", "user.name=other", "-c", "user.email=other@example.com",
         "commit", "-m", "sync 2026-09-22")
    _git(other, "push", "origin", "data")
    other_tip = _git(other, "rev-parse", "HEAD").strip()

    _write_data(repo, verdicts="mine\n")
    with pytest.raises(LeaseRejected):
        store.commit_and_push(local_day="2026-09-22", large_movement=False,
                              message=_msg("sync", "2026-09-22"), boundary=_noop)

    # The retry wrapper's refresh() lands the tree on the winning tip.
    store.refresh()
    assert _git(repo, "rev-parse", "HEAD").strip() == other_tip
    assert _git(repo, "show", "HEAD:data/verdicts.jsonl") == "other\n"


def test_commit_message_carries_no_user_ids(tmp_path):
    store, repo, origin = _repo_under_test(tmp_path)
    message = _msg("selfie", "2026-09-22", movement="cumulative",
                   rows=(3, 1), reasons={"counted": 3, "cooldown": -1, "selfie": 2})

    store.refresh()
    _write_data(repo, verdicts="x\n")
    r = store.commit_and_push(local_day="2026-09-22", large_movement=True,
                              message=message, boundary=_noop)

    body = _git(repo, "log", "-1", "--format=%B", r.sha).rstrip("\n")
    assert body == message
    assert not U_ID_RE.search(body)


def test_author_taken_from_env(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    monkeypatch.setenv("SNIPEBOT_GIT_AUTHOR", "Env Bot <env@example.com>")
    store = GitStore(repo)                       # author=None -> read the env

    store.refresh()
    _write_data(repo, verdicts="x\n")
    r = store.commit_and_push(local_day="2026-09-22", large_movement=False,
                              message=_msg("sync", "2026-09-22"), boundary=_noop)

    assert _git(repo, "log", "-1", "--format=%an <%ae>", r.sha).strip() == "Env Bot <env@example.com>"


def test_history_lists_newest_first_with_movement_and_moved_pairs(tmp_path):
    from snipebot.ledger import count_moved_pairs

    store, repo, origin = _repo_under_test(tmp_path)

    # Deltas are computed by diffing each commit's verdicts.jsonl against the previous sealed
    # commit's (40 §4.2), never from the message body — so the commit messages below carry the
    # usual per-reason lines while the moved_pairs counts follow the real verdict movements.
    v1 = _verdict_line("1000.000001", "U0TARGET", "COUNTED", "OK") + "\n"
    # A movement: the one existing pair changes verdict (moved), plus a brand-new pair (never counts).
    v2 = (
        _verdict_line("1000.000001", "U0TARGET", "NOT_COUNTED", "VETOED")
        + "\n"
        + _verdict_line("1000.000002", "U0OTHER", "COUNTED", "OK")
        + "\n"
    )
    # No verdict moves on the next day (v3 == v2): moved_pairs == 0. The day's state.json does
    # change, so the three files differ from the tip and the day gets its commit (20 §8.3
    # step 3: byte-identical files make no commit; E-W4-20f).
    v3 = v2

    store.refresh()
    _write_data(repo, verdicts=v1)
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_msg("sync", "2026-09-22", reasons={"counted": 1}),
                          boundary=_noop)
    store.refresh()
    _write_data(repo, verdicts=v2)
    store.commit_and_push(
        local_day="2026-09-22", large_movement=True,
        message=_msg("veto", "2026-09-22", movement="admin",
                     reasons={"counted": 2, "cooldown": -1}),
        boundary=_noop,
    )
    store.refresh()
    _write_data(repo, verdicts=v3, state='{"day":"2026-09-23"}')
    store.commit_and_push(local_day="2026-09-23", large_movement=False,
                          message=_msg("sync", "2026-09-23"), boundary=_noop)

    entries = store.history()
    # Newest first; the initial (non-tool) commit is excluded.
    assert [(e.day, e.is_movement) for e in entries] == [
        ("2026-09-23", False),
        ("2026-09-22", True),
        ("2026-09-22", False),
    ]
    assert all(isinstance(e, HistoryEntry) for e in entries)
    # Diffed against the actual parent (the previous sealed commit), never the message text.
    assert entries[0].moved_pairs == count_moved_pairs(v2, v3) == 0
    assert entries[1].moved_pairs == count_moved_pairs(v1, v2) == 1
    assert entries[2].moved_pairs == count_moved_pairs("", v1) == 0


def test_restore_brings_a_snapshots_files_back(tmp_path):
    store, repo, origin = _repo_under_test(tmp_path)

    store.refresh()
    _write_data(repo, verdicts="snapA\n", ledger="rowA\n", state='{"a": 1}')
    snap = store.commit_and_push(local_day="2026-09-22", large_movement=False,
                                 message=_msg("sync", "2026-09-22"), boundary=_noop)

    store.refresh()
    _write_data(repo, verdicts="snapB\n", ledger="rowB\n", state='{"b": 2}')
    store.commit_and_push(local_day="2026-09-23", large_movement=False,
                          message=_msg("sync", "2026-09-23"), boundary=_noop)

    store.restore(snap.sha)
    assert (repo / "data" / "verdicts.jsonl").read_text(encoding="utf-8") == "snapA\n"
    assert (repo / "data" / "ledger.jsonl").read_text(encoding="utf-8") == "rowA\n"
    assert (repo / "data" / "state.json").read_text(encoding="utf-8") == '{"a": 1}'


def test_baseline_verdicts_is_the_most_recent_sealed_commit(tmp_path):
    store, repo, origin = _repo_under_test(tmp_path)

    # Empty (only the initial non-tool commit): empty baseline.
    store.refresh()
    assert store.baseline_verdicts("2026-09-22") is None

    _write_data(repo, verdicts="day1\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_msg("sync", "2026-09-22"), boundary=_noop)

    # Tip is TODAY's live daily commit -> baseline is its stable parent (the initial commit: empty).
    store.refresh()
    assert store.baseline_verdicts("2026-09-22") is None

    # A movement seals its own verdicts as the next baseline.
    _write_data(repo, verdicts="moved\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=True,
                          message=_msg("veto", "2026-09-22", movement="admin"), boundary=_noop)
    store.refresh()
    assert store.baseline_verdicts("2026-09-22") == b"moved\n"


def test_baseline_on_a_new_days_first_run_is_the_previous_days_final_commit(tmp_path):
    # A new day's first run: the tip is the PREVIOUS day's sealed daily commit. Because that
    # commit is not today's amendable tip, the baseline is the tip itself, not its parent.
    store, repo, origin = _repo_under_test(tmp_path)

    store.refresh()
    _write_data(repo, verdicts="day1-final\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_msg("sync", "2026-09-22"), boundary=_noop)

    store.refresh()
    assert store.baseline_verdicts("2026-09-23") == b"day1-final\n"


def test_baseline_on_a_same_day_second_run_is_the_parent_of_todays_commit(tmp_path):
    # Two sealed days, then a fresh commit on day 3. The second run of day 3 must measure from
    # day 2's final commit (the parent of day 3's amendable tip), not from day 3's own tip.
    store, repo, origin = _repo_under_test(tmp_path)

    store.refresh()
    _write_data(repo, verdicts="day1\n")
    store.commit_and_push(local_day="2026-09-21", large_movement=False,
                          message=_msg("sync", "2026-09-21"), boundary=_noop)

    store.refresh()
    _write_data(repo, verdicts="day2-final\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_msg("sync", "2026-09-22"), boundary=_noop)

    store.refresh()
    _write_data(repo, verdicts="day3\n")
    store.commit_and_push(local_day="2026-09-23", large_movement=False,
                          message=_msg("sync", "2026-09-23"), boundary=_noop)

    store.refresh()
    assert store.baseline_verdicts("2026-09-23") == b"day2-final\n"


def test_baseline_when_tip_is_a_movement_is_the_movement_commit(tmp_path):
    # A movement commit is sealed the moment it lands, so it is its own baseline even when the
    # queried day matches the movement's day.
    store, repo, origin = _repo_under_test(tmp_path)

    store.refresh()
    _write_data(repo, verdicts="day1\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=False,
                          message=_msg("sync", "2026-09-22"), boundary=_noop)

    store.refresh()
    _write_data(repo, verdicts="moved\n")
    store.commit_and_push(local_day="2026-09-22", large_movement=True,
                          message=_msg("veto", "2026-09-22", movement="admin"), boundary=_noop)

    store.refresh()
    assert store.baseline_verdicts("2026-09-22") == b"moved\n"


def test_files_store_is_inert(tmp_path):
    fs = FilesStore()
    assert fs.refresh() is None
    assert fs.baseline_verdicts("2026-09-22") is None
    assert fs.history() == []
    r = fs.commit_and_push(local_day="2026-09-22", large_movement=True,
                           message="anything", boundary=_noop)
    assert r == CommitResult(sha="", sealed_sha=None, amended=False, pushed=False)
    with pytest.raises(NotImplementedError):
        fs.restore("deadbeef")


def test_store_for_selects_backend(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    monkeypatch.setenv("SNIPEBOT_DATA_REPO", str(repo))

    class _Cfg:
        def __init__(self, persistence):
            self.persistence = persistence

    git_store = store_for(_Cfg(Persistence.GIT), tmp_path)
    assert isinstance(git_store, GitStore)
    assert git_store.repo_path == Path(str(repo))

    assert isinstance(store_for(_Cfg(Persistence.FILES), tmp_path), FilesStore)
