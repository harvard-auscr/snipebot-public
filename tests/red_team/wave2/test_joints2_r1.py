"""Round-1 spec-conformance red team against the cross-module joints of
snipebot/{sync,ledger,persistence,cli}.py — the seams between the state machine, the
git-backed store and the durable-file layer, checked against spec/20-sync-ledger.md
(§2.2 the named step boundaries, §8.3-§8.4 the commit + push protocol).

Every test here is a break: it FAILS on the current code and would pass once the code
conforms to the quoted spec sentence. Inputs are built directly (a throwaway git repo in
tmp_path for the store; the sync commit-message builder for the message format), so this
file imports no other red-team module.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from snipebot.persistence import GitCommandError, GitStore, LeaseRejected
from snipebot.sync import Command, _commit_message


# --------------------------------------------------------------------------- #
# Finding 1 — the git commit boundaries `before_commit` / `after_commit` never fire
# --------------------------------------------------------------------------- #

def _git_init(repo: Path) -> None:
    """A bare-minimum local git repo (no remote). Commit identity is supplied by
    GitStore itself via `-c user.name=/-c user.email=`, so no `git config` is needed."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)


def test_git_persistence_never_fires_before_and_after_commit(tmp_path):
    """20 §2.2 boundary table: "| `before_commit` | git persistence: before `git commit` | — |"
    and "| `after_commit` | git persistence: after `git commit`, before push | — |".

    The crash matrix (§2.2) requires that killing at ANY named boundary and re-running yields
    the byte-identical result, so the git commit+push path must fire `before_commit` (before the
    `git commit`) and `after_commit` (after the commit, before the push). GitStore.commit_and_push
    fires only `before_push`/`after_push`; the two commit boundaries are emitted nowhere in
    snipebot/, so those crash points can never be exercised and the §2.2 invariant is unverifiable
    at them. (The only test that lists all boundaries, test_sync.py::test_all_boundaries_emitted_in_order,
    passes solely because it monkeypatches a fake store that fires them by hand.)
    """
    repo = tmp_path / "repo"
    _git_init(repo)
    data = repo / "data"
    data.mkdir()
    for name in ("ledger.jsonl", "verdicts.jsonl", "state.json"):
        (data / name).write_text("", encoding="utf-8")

    recorded: list[str] = []

    def boundary(name: str) -> None:
        recorded.append(name)

    store = GitStore(repo)
    # There is no `origin` remote, so the `--force-with-lease` push fails AFTER the commit has
    # been made; by then `before_commit`/`after_commit` — if the store fired them — are already
    # recorded. A rejected-lease phrasing would be LeaseRejected; any other push failure is
    # GitCommandError. Either way the commit boundaries must have fired first.
    with pytest.raises((GitCommandError, LeaseRejected)):
        store.commit_and_push(
            local_day="2026-09-18",
            large_movement=False,
            message="sync 2026-09-18\n\nrows +0 -0\n",
            boundary=boundary,
        )

    assert "before_commit" in recorded, (
        "20 §2.2 requires `before_commit` to fire before `git commit`; "
        f"boundaries actually fired: {recorded}"
    )
    assert "after_commit" in recorded, (
        "20 §2.2 requires `after_commit` to fire after the commit and before the push; "
        f"boundaries actually fired: {recorded}"
    )


# --------------------------------------------------------------------------- #
# Finding 2 — the commit message body omits the mandated per-reason count lines
# --------------------------------------------------------------------------- #

def test_commit_message_omits_per_reason_counts():
    """20 §8.4 pins the commit message body: after the `<command> <YYYY-MM-DD>` header it lists

        rows +<added> -<dropped>
        counted +<Δ>
        cooldown +<Δ>
        deleted +<Δ>
        selfie +<Δ>
        repost +<Δ>

    and states: "Body: verdict deltas by `Status`/`Reason` and the row add/drop counts, relative
    to the cumulative baseline." `snipebot history` "prints these per-commit deltas", and
    persistence.py::_moved_pairs_from_message parses exactly these count-by-reason lines. But
    sync.py::_commit_message — the sole producer of the message committed at step 8 (line 950) —
    emits only the `rows +A -B` line, so every commit's `Status`/`Reason` deltas are absent and
    `snipebot history` reports 0 moved pairs on every commit.
    """
    msg = _commit_message(Command.SYNC, "2026-09-18", False, "cumulative", 2, 1)
    first_tokens = {
        line.split()[0] for line in msg.splitlines() if line.strip()
    }

    missing = [
        name for name in ("counted", "cooldown", "deleted", "selfie", "repost")
        if name not in first_tokens
    ]
    assert not missing, (
        "20 §8.4 requires per-reason count lines "
        f"({', '.join(name + ' +Δ' for name in missing)}) in the commit body; "
        f"the message was:\n{msg!r}"
    )
