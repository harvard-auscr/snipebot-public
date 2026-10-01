"""Red-team wave 2, round 3 (INVARIANTS ACROSS RUNS) against sync steps 8-10,
`run_sync_git`, `post_digests`, and how sync drives `persistence.py` / `ledger.py`.

Each test reproduces one spec violation and FAILS on the current code. A test that
passes would not be a finding and is deleted.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from snipebot import sync
from snipebot.config import Persistence
from snipebot.faces import FakeFaceDetector
from snipebot.persistence import LeaseRejected
from snipebot.sync import Command, MAX_LEASE_RETRIES, run_sync_git
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import (
    BOT,
    CHANNEL,
    image_file,
    make_config,
    mkts,
    roster_of,
)

ADMIN = "U0ADMIN"


# --------------------------------------------------------------------------- helpers

def _users(*ids: str) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in ids:
        out[uid] = FakeUser(id=uid, display_name=uid)
    return out


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run_git(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _make_origin(tmp_path: Path) -> Path:
    """A bare origin whose `data` branch has one initial (non-tool, verdicts-free) commit,
    so the cumulative baseline of day D's first commit is the empty verdicts file ("")."""
    origin = tmp_path / "origin.git"
    _run_git(["git", "init", "--bare", "-b", "data", str(origin)])
    seed = tmp_path / "seed"
    _run_git(["git", "clone", str(origin), str(seed)])
    (seed / "data").mkdir(exist_ok=True)
    (seed / "data" / "README").write_text("data branch\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=seed", "-c", "user.email=seed@example.com",
         "commit", "-m", "init data branch")
    _git(seed, "branch", "-M", "data")
    _git(seed, "push", "-u", "origin", "data")
    return origin


def _clone(origin: Path, dest: Path) -> Path:
    _run_git(["git", "clone", str(origin), str(dest)])
    _git(dest, "checkout", "data")
    return dest


def _head_body(repo: Path) -> str:
    return _git(repo, "log", "-1", "refs/heads/data", "--format=%B")


class _AlwaysRejectStore:
    """A `Store` whose push always loses the lease, driving `run_sync_git`'s retry loop."""

    def __init__(self) -> None:
        self.refreshes = 0

    def refresh(self) -> None:
        self.refreshes += 1

    def baseline_verdicts(self, local_day: str) -> bytes | None:
        return None

    def commit_and_push(self, *, local_day, large_movement, message, boundary):
        raise LeaseRejected("stale info")

    def history(self):
        return []

    def restore(self, commit: str) -> None:  # pragma: no cover - unused here
        raise NotImplementedError


# --------------------------------------------------------------------------- Finding 1

def test_rows_delta_not_relative_to_cumulative_baseline(tmp_path, monkeypatch):
    """20 §8.4 (Commit message format) mandates: "Body: verdict deltas by `Status`/`Reason`
    and the row add/drop counts, relative to the cumulative baseline." The cumulative
    baseline (20 §8.1-8.2) of day D's amendable commit is the most recent SEALED commit's
    verdicts -- here the empty initial commit ("") -- NOT the day's own (amended) first
    commit.

    `run_sync` (snipebot/sync.py) builds the `rows +<added> -<dropped>` line from `added =
    sum(r for r in rows_final if r.ts not in stored_rows)` where `stored_rows` is *this run's
    starting ledger* (the amendable tip's tree after `refresh`), not the cumulative baseline.
    The by-reason deltas ARE taken from the cumulative baseline (`_baseline_reason_counts`
    over `store.baseline_verdicts`). So within a day, each amend's `rows` line counts only the
    rows added since the previous run, while `counted`/`cooldown`/`selfie` stay cumulative --
    the two halves of the same body disagree, and a string of small runs slides its row count
    under the sealed baseline that §8.1 says the cumulative measure exists to prevent.

    Two amending syncs on the same day add two brand-new counted rows above an empty baseline.
    The persisted daily commit's by-reason line reads `counted +2` (cumulative, correct), but
    its `rows` line reads `+1 -0` instead of the cumulative `+2 -0`.
    """
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    monkeypatch.setenv("SNIPEBOT_DATA_REPO", str(repo))

    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, persistence=Persistence.GIT)

    now1 = mkts(2026, 9, 18, 21, 30)
    slack = FakeSlack(now=now1, bot_user_id=BOT, users=_users("U0A", "U0B", ADMIN),
                      channels=(CHANNEL,), bot_member_of=(CHANNEL,))
    slack.post(at=mkts(2026, 9, 18, 12), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"a")])

    led = repo / "data" / "ledger.jsonl"
    st = repo / "data" / "state.json"

    run_sync_git(slack, cfg, detector=FakeFaceDetector({}),
                 ledger_path=led, state_path=st, now_us=parse_ts(now1),
                 command=Command.SYNC, now_fn=lambda: parse_ts(now1),
                 no_react=True, no_post=True)

    # Second sync, same local day: one more brand-new counted row -> the day's commit is
    # amended, and its cumulative baseline is still the empty initial commit.
    slack.post(at=mkts(2026, 9, 18, 13), user="U0B", channel=CHANNEL,
               text="<@U0A>", files=[image_file("F02", b"b")])
    now2 = mkts(2026, 9, 18, 21, 40)
    slack.as_of(now2)
    run_sync_git(slack, cfg, detector=FakeFaceDetector({}),
                 ledger_path=led, state_path=st, now_us=parse_ts(now2),
                 command=Command.SYNC, now_fn=lambda: parse_ts(now2),
                 no_react=True, no_post=True)

    body = _head_body(repo)
    assert "sync 2026-09-18" in body
    counted = re.search(r"^counted ([+-]\d+)$", body, re.MULTILINE)
    rows = re.search(r"^rows \+(\d+) -(\d+)$", body, re.MULTILINE)
    assert counted is not None and rows is not None, body
    # The by-reason line is cumulative from the empty baseline (two counted messages).
    assert counted.group(1) == "+2", body
    # §8.4 requires the rows add/drop to be relative to the SAME cumulative baseline: two
    # brand-new rows -> `rows +2 -0`. The current code reports only the last run's addition.
    assert rows.group(1) == "2", (
        f"rows added counted from this run's ledger, not the cumulative baseline: {body!r}"
    )


# --------------------------------------------------------------------------- Finding 2

def test_lease_retry_reuses_stale_now_us(tmp_path, monkeypatch):
    """20 §8.3 step 6 and §1.4 both require that on a rejected `--force-with-lease` push,
    `run_sync_git` "re-runs the whole sync from step 1 with a fresh `now_us`". The §1.4
    signature is `run_sync_git(slack, config, *, ledger_path, state_path, now_us, command,
    **flags)` -- it carries no clock; producing a fresh `now_us` is the wrapper's own job.

    The shipped wrapper only advances the clock when an optional, undocumented `now_fn`
    keyword is supplied (`attempt_now = now_fn() if now_fn is not None else attempt_now`).
    Called per its §1.4 contract -- no `now_fn` -- every retry re-runs the sync with the very
    same `now_us`, so the re-run does not reflect the fresh wall clock the spec mandates (the
    stale window would, e.g., re-select a digest period and 24 h window from an out-of-date
    instant). Here each retry is handed an identical `now_us`.
    """
    origin_now = mkts(2026, 9, 18, 21, 30)
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, persistence=Persistence.GIT)
    slack = FakeSlack(now=origin_now, bot_user_id=BOT, users=_users("U0A", "U0B", ADMIN),
                      channels=(CHANNEL,), bot_member_of=(CHANNEL,))
    slack.post(at=mkts(2026, 9, 18, 12), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"a")])

    store = _AlwaysRejectStore()
    monkeypatch.setattr(sync, "store_for", lambda config, data_dir: store)

    seen_now: list[int] = []
    real_run_sync = sync.run_sync

    def _capture(*args, **kwargs):
        seen_now.append(kwargs["now_us"])
        return real_run_sync(*args, **kwargs)

    monkeypatch.setattr(sync, "run_sync", _capture)

    data = tmp_path / "data"
    data.mkdir()
    result = run_sync_git(
        slack, cfg, detector=FakeFaceDetector({}),
        ledger_path=data / "ledger.jsonl", state_path=data / "state.json",
        now_us=parse_ts(origin_now), command=Command.SYNC, no_react=True, no_post=True,
    )

    assert result.exit_code == 9                     # exhausted the bounded retries (§8.3/§9.1)
    assert len(seen_now) == MAX_LEASE_RETRIES        # each attempt re-ran the whole sync
    # §8.3 step 6: every re-run must carry a FRESH now_us; the shipped wrapper reuses it.
    assert len(set(seen_now)) == len(seen_now), (
        f"lease retries re-ran with a stale now_us: {seen_now}"
    )
