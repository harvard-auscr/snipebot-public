"""E-W4-37: a movement commit's message counts what that movement changed relative to its
parent (the restore point it seals), not relative to the day's cumulative baseline.

Found in the dress rehearsal: after a daily sync commit that opted a player out, the
`rejoin` movement commit read `rows +1`, `vetoed +1` (its diff against the previous sealed
commit) although, against its own parent, it moved every opted-out verdict back. Summing
the history's lines then no longer matched the state.
"""

from __future__ import annotations

from snipebot.cli import main
from snipebot.faces import FakeFaceDetector

from tests.red_team.wave4.test_persistence_r1 import (  # noqa: F401  (fixture import)
    ADMIN,
    MSG_TS,
    _git,
    _world,
    git_cli,
)


def test_movement_commit_counts_its_own_change_against_its_parent(git_cli):
    """E-W4-37: `veto` on a snipe counted by today's daily commit is a movement commit whose
    body says `rows +0 -0` and `counted -1` (what the veto changed against its parent), not
    `rows +1` with `counted +0` (the net since the day's cumulative baseline)."""
    base, _repo, origin, sync_sha = git_cli
    slack = _world()
    rc = main(["veto", *base, "--ts", MSG_TS, "--by", ADMIN, "--no-post"],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    tip = _git(origin, "rev-parse", "data").strip()
    assert tip != sync_sha
    assert _git(origin, "rev-parse", "data~1").strip() == sync_sha
    body = _git(origin, "log", "-1", "--format=%B", "data")
    assert "[movement:admin]" in body, body
    assert "rows +0 -0" in body, body
    assert "counted -1" in body, body
    assert "vetoed: +1" in body, body


def test_daily_commit_still_counts_against_the_cumulative_baseline(git_cli):
    """E-W4-37 leaves daily commits alone: the fixture's first sync of the day is measured
    against the empty baseline, so it records the new row and its counted verdict."""
    _base, _repo, origin, _sync_sha = git_cli
    body = _git(origin, "log", "-1", "--format=%B", "data")
    assert "[movement" not in body.splitlines()[0], body
    assert "rows +1 -0" in body, body
    assert "counted +1" in body, body
