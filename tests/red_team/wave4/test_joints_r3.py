"""Wave 4 / round 3 breaker on the cross-module joints.

Each test FAILS on the current code for exactly the reason in its docstring and would pass
once the code conforms to the quoted spec sentence. Offline only: a FakeSlack world, files
persistence under tmp_path, or git against a bare origin plus a clone under tmp_path.
"""

from __future__ import annotations

import calendar
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.faces import FakeFaceDetector
from snipebot.sync import run_sync
from snipebot.ts import US_PER_SECOND

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file, make_config, roster_of

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
OTHER = "U0AAA003"
ADMIN = "U0AAA009"
THIRD = "U0AAA010"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 10, micro=1)


def _users(*, bots: tuple[str, ...] = ()) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for i, uid in enumerate((SNIPER, TARGET, OTHER, ADMIN, THIRD), start=1):
        out[uid] = FakeUser(id=uid, display_name=f"user-{i}", is_bot=uid in bots)
    return out


def _photo(n: int) -> dict:
    return image_file(f"F0FILE{n:03d}", b"photo-%d" % n, name=f"photo-{n}.png")


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


# --------------------------------------------------------------------------- #
# 1. A malformed snipe_digest payload fails every sync instead of a ParseAnomaly
# --------------------------------------------------------------------------- #

def test_malformed_snipe_digest_payload_is_an_anomaly_not_a_failed_run(tmp_path):
    """20 §2 step 3: "`parse` signals a `text`/`blocks` mention disagreement or a
    **malformed digest** by emitting a `ParseAnomaly` **warning** (10 §4); step 3 runs
    `parse` under `warnings.catch_warnings(record=True)`, stores the row normally ...
    (never a verdict; **never a failed run**)". 10 §4 step 1 recognises a digest by
    `metadata.event_type == "snipe_digest"` alone, "regardless of sender", and
    `conversations.history` is called with `include_all_metadata=True`, so every app's
    metadata reaches `parse`.

    `DigestMetadata.from_wire` indexes `event_payload["period_key"]` etc. without a check,
    so a `snipe_digest` message whose payload lacks a key (another integration reusing the
    event type, or a digest from an older payload shape) raises KeyError inside `parse`.
    Step 3 maps any raised parse error to exit 1 with nothing written, so every scheduled
    sync fails -- and the real snipe beside it is never scored -- for as long as that one
    message stays in the fetch range (scan_days).
    """
    cfg = make_config(roster=roster_of({SNIPER: "fam", TARGET: "fam"}), admins=(ADMIN,))
    slack = FakeSlack(now=_ts(2026, 9, 18, 12), bot_user_id=BOT, users=_users())
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[_photo(1)])
    slack.post(at=_ts(2026, 9, 18, 11, micro=1), user=THIRD, channel=CHANNEL,
               text="weekly poll",
               metadata={"event_type": "snipe_digest",
                         "event_payload": {"report": "poll", "channel": CHANNEL}})
    ledger, state = _paths(tmp_path)
    r = run_sync(slack, cfg, detector=FakeFaceDetector({}), ledger_path=ledger,
                 state_path=state, now_us=NOW_US, no_react=True, no_post=True)
    assert r.exit_code == 0, (
        f"a malformed snipe_digest payload failed the whole sync (exit {r.exit_code})")
    assert MSG_TS in ledger.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# git rig (bare origin + clone under tmp_path; never a remote host)
# --------------------------------------------------------------------------- #

def _config_dict(*, persistence: str = "files", bot_target: bool = False) -> dict:
    fam = [SNIPER, TARGET, OTHER]
    if bot_target:
        fam.append(THIRD)
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
            "count_image_links": False, "allow_video": False, "selfie_bonus": False,
        },
        "players": {"count_intra_group": True, "groups": {"fam": fam}, "extras": []},
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
    slack = FakeSlack(now=_ts(2026, 9, 18, 12), bot_user_id=BOT, users=_users())
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[_photo(1)])
    rc = main(["sync", "--no-react", "--no-post", *_argv(cfg, repo / "data")],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    assert TARGET in _git(origin, "show", "data:data/ledger.jsonl")
    return origin, repo, cfg, slack


# --------------------------------------------------------------------------- #
# 2. `history` prints a bare pair count, never the counts by reason of what moved
# --------------------------------------------------------------------------- #

def test_history_prints_counts_by_reason_of_what_moved(tmp_path, monkeypatch, capsys):
    """40 §4.2 `history`: "For each daily/movement commit: SHA, local date, and **counts by
    reason of what moved** (never IDs/names); plus veto totals by actor and the audit
    flags". `_cmd_history` prints only `<sha>  <day>  <kind>  moved <N>` (the
    `count_moved_pairs` total) for every commit.

    Scenario: a counted snipe is synced, then an admin vetoes it. The veto movement commit
    flipped one message from `counted` to `vetoed`; `history` must say so by reason (the
    same `counted -1` / `vetoed` vocabulary as the 20 §8.4 body), so an admin without repo
    access can see WHAT moved. It prints `moved 1` and no reason at all.
    """
    origin, repo, cfg, slack = _git_rig(tmp_path, monkeypatch)
    rc = main(["veto", "--ts", MSG_TS, "--by", ADMIN, "--no-react", "--no-post",
               *_argv(cfg, repo / "data")],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == int(Exit.OK)
    veto_sha = _git(origin, "rev-parse", "data").strip()
    capsys.readouterr()

    rc = main(["history", *_argv(cfg, repo / "data")])
    out = capsys.readouterr().out
    assert rc == int(Exit.OK)
    # The veto commit's entry: its SHA line plus any lines up to the next commit's SHA.
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(veto_sha))
    entry = [lines[start]]
    for ln in lines[start + 1:]:
        if re.match(r"^[0-9a-f]{40}(\s|$)", ln):
            break
        entry.append(ln)
    block = "\n".join(entry)
    assert "vetoed" in block and "counted" in block, (
        f"history gives no counts by reason for the veto movement: {block!r}")


# --------------------------------------------------------------------------- #
# 3. report/export score a rostered bot target that sync's verdicts reject
# --------------------------------------------------------------------------- #

def test_report_totals_match_the_synced_verdicts_for_a_rostered_bot(
        tmp_path, monkeypatch, capsys):
    """30 §1: `eligible_snipes` is "the single boundary" -- report, export and the digest
    all take their numbers from one fresh `evaluate` over the ledger, so for one ledger the
    `report` totals equal the verdicts `sync` wrote. 40 §2.1: the CLI resolves
    `RosterEntry.is_bot` from `users.list` so the TARGET_IS_BOT gate (00 §4) can fire.

    The round-2 repair loads the is_bot map only on the Slack-touching commands
    (`_load_config_with_bots`); `_cmd_report` and `_cmd_export` still call `_load_config`
    with no map, so every rostered user is `is_bot=False` there. A snipe of a rostered bot
    account is `not_counted/target_is_bot` in verdicts.jsonl (and in the channel digest,
    which renders inside sync), yet `report --by snipes` lists it as a counted snipe (and
    `export` writes it to every table), so report/export totals disagree with the ledger's
    own verdicts.
    """
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write_cfg(tmp_path / "config.yaml", bot_target=True)
    data = tmp_path / "data"
    data.mkdir()
    slack = FakeSlack(now=_ts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users(bots=(THIRD,)))
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{THIRD}>",
               files=[_photo(1)])
    rc = main(["sync", "--no-react", "--no-post", *_argv(cfg, data)],
              slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    row = json.loads((data / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["reason"] == "target_is_bot"       # sync's verdict: not a snipe
    capsys.readouterr()

    rc = main(["report", "--by", "snipes", *_argv(cfg, data)])
    out = capsys.readouterr().out
    assert rc == int(Exit.OK)
    assert THIRD not in out, (
        f"report lists a snipe the synced verdicts rejected as target_is_bot:\n{out}")
