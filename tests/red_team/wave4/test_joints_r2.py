"""Wave 4 / round 2 breaker on the cross-module joints.

Each test FAILS on the current code for exactly the reason in its docstring and would pass
once the code conforms to the quoted spec sentence. Offline only: a FakeSlack world, files
persistence under tmp_path, or git against a bare origin plus a clone under tmp_path.
"""

from __future__ import annotations

import calendar
import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.config import Cadence, ReportSpec, Section, Weekday
from snipebot.faces import FakeFaceDetector
from snipebot.sync import run_sync
from snipebot.ts import US_PER_SECOND, parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file, make_config, roster_of

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
OTHER = "U0AAA003"
ADMIN = "U0AAA009"
OFF_ROSTER = "U0AAA099"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


def _users() -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for i, uid in enumerate((SNIPER, TARGET, OTHER, ADMIN, OFF_ROSTER), start=1):
        out[uid] = FakeUser(id=uid, display_name=f"user-{i}")
    return out


def _photo(n: int) -> dict:
    return image_file(f"F0FILE{n:03d}", b"photo-%d" % n, name=f"photo-{n}.png")


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


# --------------------------------------------------------------------------- #
# 1. Pass B re-renders an old digest under the report's NEW cadence and crashes
# --------------------------------------------------------------------------- #

def test_report_cadence_change_makes_every_sync_crash_in_digest_pass_b(tmp_path):
    """00 §8: a period key is `<report>:<cadence tail>` (`daily:2026-09-18`,
    `weekly:2026-W38`); the tail "makes each period unique within a report" and is only
    meaningful for the cadence that minted it. 20 §6.2 Pass B picks the ReportSpec for a
    found digest by NAME alone (`rpt = next(r ... if r.name == p.report)`) and re-renders
    the stored period_key with it; 20 §9.1 / 30 §5.5 make only DigestTooLargeError and
    SlackError the step-9 failures.

    When the owner changes a report's `every:` (1d -> 1w) and keeps its name, the daily
    digest already in the channel is re-rendered as a WEEK section: `_parse_week` splits
    "2026-09-18" on "-W" and raises ValueError, which step 9 does not catch. Every sync
    for the next `scan_days` (while that digest stays in the scan window) then dies with
    an unexpected error after persisting, fails the Actions job every 10 minutes, and
    never revises any later digest. A found digest whose period key does not belong to
    the report's current cadence must be skipped, not re-rendered.
    """
    roster = roster_of({SNIPER: "fam", TARGET: "fam"})
    daily = ReportSpec(name="standings", cadence=Cadence.DAILY, at_hour=21, at_minute=0,
                       weekday=None, post_to=None, sections=(Section.DAY,), top_n=5)
    weekly = ReportSpec(name="standings", cadence=Cadence.WEEKLY, at_hour=20, at_minute=0,
                        weekday=Weekday.SUN, post_to=None, sections=(Section.WEEK,),
                        top_n=5)
    cfg_daily = make_config(roster=roster, admins=(ADMIN,), reports=(daily,))
    cfg_weekly = make_config(roster=roster, admins=(ADMIN,), reports=(weekly,))

    slack = FakeSlack(now=_ts(2026, 9, 18, 21, 30), bot_user_id=BOT, users=_users())
    slack.post(at=_ts(2026, 9, 18, 10, micro=1), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(1)])
    ledger, state = _paths(tmp_path)
    kw = dict(detector=FakeFaceDetector({}), ledger_path=ledger, state_path=state,
              no_react=True)

    r1 = run_sync(slack, cfg_daily, now_us=_secs(2026, 9, 18, 21, 30) * US_PER_SECOND, **kw)
    assert r1.exit_code == 0 and r1.digests_posted == 1

    # A new snipe lands, so the old daily digest's numbers would change on re-render.
    slack.as_of(_ts(2026, 9, 19, 9))
    slack.post(at=_ts(2026, 9, 19, 9, micro=1), user=TARGET, channel=CHANNEL,
               text=f"<@{SNIPER}>", files=[_photo(2)])
    slack.as_of(_ts(2026, 9, 19, 10))
    try:
        r2 = run_sync(slack, cfg_weekly, now_us=_secs(2026, 9, 19, 10) * US_PER_SECOND, **kw)
    except ValueError as exc:  # pragma: no cover - the defect
        pytest.fail(f"step 9 Pass B crashed re-rendering the old daily digest as weekly: {exc!r}")
    assert r2.exit_code == 0


# --------------------------------------------------------------------------- #
# 2. The dry-run "verdict counts by reason" still carries only the five fixed names
# --------------------------------------------------------------------------- #

def test_backfill_dry_run_counts_by_reason_omit_every_other_reason(tmp_path):
    """40 §4.2 `backfill` Output: "verdict counts by reason"; 20 §8.4 lists, after the five
    fixed lines, "`<other changed status/reason>`" — the round-1 repair added those other
    reasons to the commit body and `moved_lines` (`_count_other_reason`), but the dry-run
    branch of `run_sync` still builds `counts_by_reason` from `_COMMIT_DELTA_ORDER` only.

    A go-live `backfill --dry-run` (plan L8) over a channel with one counted snipe and one
    snipe from an off-roster sender therefore prints `counted 1 cooldown 0 deleted 0
    selfie 0 repost 0` and no `sender_off_roster` count at all: the not-counted reasons the
    dry run exists to surface are missing from its only stdout summary.
    """
    cfg = make_config(roster=roster_of({SNIPER: "fam", TARGET: "fam"}), admins=(ADMIN,))
    slack = FakeSlack(now=_ts(2026, 9, 18, 12), bot_user_id=BOT, users=_users())
    slack.post(at=_ts(2026, 9, 18, 10, micro=1), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(1)])
    slack.post(at=_ts(2026, 9, 18, 11, micro=1), user=OFF_ROSTER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(2)])
    ledger, state = _paths(tmp_path)
    r = run_sync(slack, cfg, detector=FakeFaceDetector({}), ledger_path=ledger,
                 state_path=state, now_us=_secs(2026, 9, 18, 12) * US_PER_SECOND,
                 dry_run=True, no_react=True, no_post=True,
                 backfill_from_us=_secs(2026, 9, 1) * US_PER_SECOND)
    assert r.exit_code == 0
    tokens = dict(t.rsplit(" ", 1) for t in r.counts_by_reason)
    assert tokens.get("counted") == "1"
    assert tokens.get("sender_off_roster") == "1", (
        f"dry-run verdict counts by reason omit the off-roster snipe: {r.counts_by_reason}"
    )


# --------------------------------------------------------------------------- #
# CLI config (files and git) shared by tests 3-5
# --------------------------------------------------------------------------- #

NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 10, micro=1)


def _config_dict(*, persistence: str = "files", selfie_bonus: bool = False) -> dict:
    return {
        "enabled": True,
        "persistence": persistence,
        "slack": {"channel": CHANNEL},
        "timezone": "UTC",
        "sync": {"interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
                 "max_deletes_per_run": 5, "large_movement_rows": 25},
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}],
        "rules": {
            "cooldown": {"minutes": 15, "scope": "pair", "rejected_attempts_reset": False},
            "multi_tag": "per_target", "max_targets_per_message": None,
            "edit_grace_minutes": 10, "max_snipes_per_target_per_day": None,
            "allow_self": False, "allow_bots": False, "count_thread_replies": False,
            "count_image_links": False, "allow_video": False, "selfie_bonus": selfie_bonus,
        },
        "players": {"count_intra_group": True,
                    "groups": {"fam": [SNIPER, TARGET, OTHER]}, "extras": []},
        "consent": {"veto": {"emoji": "no_entry_sign", "by": ["admins"]},
                    "optout_messages": [], "opted_out": []},
        "admins": [ADMIN],
        "feedback": {
            "reactions": {"counted": "white_check_mark",
                          "cooldown": "hourglass_flowing_sand",
                          "untagged": None, "not_counted": "x", "selfie": None},
            "review": {"min_targets": 5, "emoji": "question"},
        },
        "reports": [],
    }


def _write_cfg(path: Path, **kw) -> Path:
    path.write_text(yaml.safe_dump(_config_dict(**kw), sort_keys=False), encoding="utf-8")
    return path


def _argv(cfg: Path, data: Path) -> list[str]:
    return ["--config", str(cfg), "--data-dir", str(data)]


def _world() -> FakeSlack:
    slack = FakeSlack(now=_ts(2026, 9, 18, 12), bot_user_id=BOT, users=_users())
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[_photo(1)])
    return slack


def _detector() -> FakeFaceDetector:
    # T = 1 tagged sib, one face -> a plain SNIPE by the detector.
    return FakeFaceDetector({hashlib.sha256(b"photo-1").hexdigest(): 1})


# --------------------------------------------------------------------------- #
# 3. `selfie` never prints the message's new SelfieClass
# --------------------------------------------------------------------------- #

def test_selfie_command_prints_the_new_selfie_class(tmp_path, monkeypatch, capsys):
    """40 §4.2 `selfie` Output: "the message's new `SelfieClass`, verdict flips by reason,
    the produced commit (§4.3)" — the same shape as veto/unveto's "the message's new
    verdict", which the CLI prints from `SyncResult.target_verdict`. For `selfie`,
    `run_sync` fills `target_verdict` only when `veto_ts` is set and `_cmd_selfie` prints
    only the §4.3 block, so an admin who runs `selfie --ts <ts> --no` never sees the class
    the message now has (`snipe`), only the generic `moved:` counts.
    """
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write_cfg(tmp_path / "config.yaml", selfie_bonus=True)
    data = tmp_path / "data"
    data.mkdir()
    slack = _world()
    rc = main(["sync", "--no-react", "--no-post", *_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=_detector)
    assert rc == 0
    capsys.readouterr()

    rc = main(["selfie", "--ts", MSG_TS, "--no", "--by", ADMIN, "--no-react", "--no-post",
               *_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=_detector)
    out = capsys.readouterr().out
    assert rc == 0
    verdicts = (data / "verdicts.jsonl").read_text(encoding="utf-8")
    assert '"selfie":"snipe"' in verdicts           # the class really is SNIPE now
    words = out.replace(":", " ").split()
    assert "snipe" in words, f"selfie printed no SelfieClass; stdout was:\n{out}"


# --------------------------------------------------------------------------- #
# git rig for tests 4-5 (bare origin + clone under tmp_path; never a remote host)
# --------------------------------------------------------------------------- #

def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _git_rig(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path, FakeSlack]:
    """(origin, repo, config, slack) with one synced counted snipe committed and pushed."""
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "data", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", str(origin), str(seed)])
    (seed / "data").mkdir(exist_ok=True)
    (seed / "data" / "README").write_text("data branch\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=seed", "-c", "user.email=seed@fixture.invalid",
         "commit", "-m", "init data branch")
    _git(seed, "branch", "-M", "data")
    _git(seed, "push", "-u", "origin", "data")
    repo = tmp_path / "repo"
    _run(["git", "clone", str(origin), str(repo)])
    _git(repo, "checkout", "data")
    monkeypatch.setenv("SNIPEBOT_DATA_REPO", str(repo))
    monkeypatch.setenv("SNIPEBOT_GIT_AUTHOR", "snipebot <snipebot@fixture.invalid>")
    cfg = _write_cfg(tmp_path / "config.yaml", persistence="git")
    slack = _world()
    rc = main(["sync", "--no-react", "--no-post", *_argv(cfg, repo / "data")],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    assert TARGET in _git(origin, "show", "data:data/ledger.jsonl")
    return origin, repo, cfg, slack


# --------------------------------------------------------------------------- #
# 4. Sync-driven large movements never print the sealed restore point
# --------------------------------------------------------------------------- #

def test_veto_under_git_prints_the_sealed_restore_point(tmp_path, monkeypatch, capsys):
    """40 §4.3: "A large movement prints both `commit` (the restore point sealed before it)
    and `movement`"; `veto` is a large movement (20 §8.5). `GitStore.commit_and_push`
    returns `CommitResult.sealed_sha`, but `run_sync` drops it: `SyncResult` (20 §1.3) has
    only `commit_sha`, so `_print_write_outcome` can print `movement <sha>` and never
    `commit <sealed>`. Every sync-driven admin movement (veto, unveto, selfie, rejoin,
    accept-deletes, backfill, --reevaluate) hides the restore point an admin without repo
    access needs to undo it with `restore --from`.
    """
    origin, repo, cfg, slack = _git_rig(tmp_path, monkeypatch)
    sealed = _git(origin, "rev-parse", "data").strip()
    capsys.readouterr()
    rc = main(["veto", "--ts", MSG_TS, "--by", ADMIN, "--no-react", "--no-post",
               *_argv(cfg, repo / "data")],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    out = capsys.readouterr().out
    assert rc == int(Exit.OK)
    assert _git(origin, "rev-parse", "data").strip() != sealed   # the movement landed
    assert f"commit {sealed}" in out, out


# --------------------------------------------------------------------------- #
# 5. restore commits a §8.4 body whose verdict deltas are always zero
# --------------------------------------------------------------------------- #

def test_restore_commit_body_carries_the_real_verdict_deltas(tmp_path, monkeypatch):
    """20 §8.4: the commit body is "verdict deltas by `Status`/`Reason` and the row add/drop
    counts, relative to the cumulative baseline" (the last sealed commit, §8.1), and 40
    §4.2 `restore` Output includes "verdict flips". `_cmd_restore` calls the shared
    `_commit_message` with no `deltas`, so every restore body reads `counted +0 cooldown +0
    ...` whatever it moved.

    Scenario: a counted snipe is committed, an admin vetoes it (movement commit, counted
    0, vetoed 1), then `restore --from` the pre-veto commit brings the counted verdict
    back. The restore commit's body must say `counted +1` (and `vetoed: -1`) against the
    sealed veto commit; it says `counted +0` and records no flip at all.
    """
    origin, repo, cfg, slack = _git_rig(tmp_path, monkeypatch)
    pre_veto = _git(origin, "rev-parse", "data").strip()
    rc = main(["veto", "--ts", MSG_TS, "--by", ADMIN, "--no-react", "--no-post",
               *_argv(cfg, repo / "data")],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.OK)
    assert '"status":"counted"' not in _git(origin, "show", "data:data/verdicts.jsonl")

    rc = main(["restore", "--from", pre_veto, *_argv(cfg, repo / "data")])
    assert rc == int(Exit.OK)
    assert '"status":"counted"' in _git(origin, "show", "data:data/verdicts.jsonl")
    body = _git(origin, "log", "-1", "--format=%B", "data").splitlines()
    assert body[0].startswith("restore ")
    assert "counted +1" in body, f"restore commit body records no verdict flip: {body!r}"
