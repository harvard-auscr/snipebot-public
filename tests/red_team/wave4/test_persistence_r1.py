"""Red team, wave 4, round 1: the git-backed `data` branch (snipebot/persistence.py) and the
CLI commands that drive it (restore, purge, history).

Real git only, against a bare `origin` and clones under tmp_path; never a remote host.
"""

from __future__ import annotations

import calendar
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import main
from snipebot.config import Persistence
from snipebot.faces import FakeFaceDetector
from snipebot.persistence import DEFAULT_GIT_AUTHOR, GitStore, store_for
from snipebot.ts import US_PER_SECOND

from tests._helpers_sync import image_file
from tests.fake_slack import FakeSlack, FakeUser

AUTHOR = "snipebot-test <snipebot-test@example.invalid>"
DAY = "2026-09-18"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
ADMIN = "U0AAA009"
BOT = "U0BOT01"
CHANNEL = "C0MAIN01"


# --------------------------------------------------------------------------- git helpers


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr}")
    return proc


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd).stdout


def _make_origin(tmp_path: Path) -> Path:
    """A bare origin whose `data` branch has one initial (non-tool) commit."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "data", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", str(origin), str(seed)])
    (seed / "data").mkdir(exist_ok=True)
    (seed / "data" / "README").write_text("data branch\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=seed", "-c", "user.email=seed@example.invalid",
         "commit", "-m", "init data branch")
    _git(seed, "push", "origin", "data")
    return origin


def _clone(origin: Path, dest: Path) -> Path:
    _run(["git", "clone", str(origin), str(dest)])
    _git(dest, "checkout", "data")
    (dest / "data").mkdir(exist_ok=True)
    return dest


def _write_data(repo: Path, marker: str) -> None:
    d = repo / "data"
    d.mkdir(exist_ok=True)
    (d / "ledger.jsonl").write_bytes(f"{marker}-ledger\n".encode())
    (d / "verdicts.jsonl").write_bytes(f"{marker}-verdicts\n".encode())
    (d / "state.json").write_bytes(f'{{"m":"{marker}"}}'.encode())


def _noop(_stage: str) -> None:
    pass


# --------------------------------------------------------------------------- CLI world


def _secs(y, mo, d, h=0) -> int:
    return calendar.timegm((y, mo, d, h, 0, 0, 0, 0, 0))


NOW_TS = f"{_secs(2026, 9, 18, 12)}.000000"
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = f"{_secs(2026, 9, 18, 10)}.000000"


def _config_yaml(tmp_path: Path) -> Path:
    cfg = {
        "enabled": True,
        "persistence": "git",
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
        "players": {"count_intra_group": True, "groups": {"fam": [SNIPER, TARGET]},
                    "extras": []},
        "consent": {"veto": {"emoji": "no_entry_sign", "by": ["admins"]},
                    "optout_messages": [], "opted_out": []},
        "admins": [ADMIN],
        "feedback": {
            "reactions": {"counted": "white_check_mark", "cooldown": "hourglass_flowing_sand",
                          "untagged": None, "not_counted": "x", "selfie": None},
            "review": {"min_targets": 5, "emoji": "question"},
        },
        "reports": [],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def _world() -> FakeSlack:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2"),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[image_file("F0FILE001", b"photo-1")])
    return slack


@pytest.fixture
def git_cli(tmp_path, monkeypatch):
    """A synced git-persisted data repo: one `sync` commit on origin's `data` branch holding
    a counted snipe by SNIPER on TARGET. Returns (argv base, repo, origin, sync sha)."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    monkeypatch.setenv("SNIPEBOT_DATA_REPO", str(repo))
    monkeypatch.setenv("SNIPEBOT_GIT_AUTHOR", AUTHOR)
    cfg = _config_yaml(tmp_path)
    base = ["--config", str(cfg), "--data-dir", str(repo / "data")]
    slack = _world()
    rc = main(["sync", *base, "--no-post"], slack_factory=lambda: slack,
              detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0
    sync_sha = _git(origin, "rev-parse", "data").strip()
    assert TARGET in _git(origin, "show", f"{sync_sha}:data/ledger.jsonl")
    return base, repo, origin, sync_sha


# --------------------------------------------------------------------------- findings


def test_store_for_git_ignores_data_dir_and_targets_the_code_checkout(tmp_path, monkeypatch):
    """Claim: under `persistence: git`, `store_for(config, data_dir)` ignores `data_dir` and
    opens the checkout named by SNIPEBOT_DATA_REPO (default `.`), while `GitStore` only ever
    commits `data/*` relative to that checkout. The shipped workflows (40 §7.1 sync.yml, 40
    §7.2 admin.yml) check the data branch out at `_data`, run from the code checkout with
    `--data-dir _data/data`, and set no SNIPEBOT_DATA_REPO. So the store refreshes, commits
    and force-pushes the CODE checkout (switching it to the data branch and committing `_data`
    as a gitlink) while every ledger write lands in `_data/data` and is never committed.
    Violates 20 §8.3 step 1-2 (the protocol runs in the checkout that holds the data files)
    and 40 §6.1 (SNIPEBOT_DATA_REPO = the workflow's data checkout): a prod deploy that never
    persists the ledger. The store must operate on the checkout whose `data/` is `data_dir`,
    or refuse the mismatch."""
    code = tmp_path / "code"
    (code / "_data" / "data").mkdir(parents=True)
    monkeypatch.chdir(code)
    monkeypatch.delenv("SNIPEBOT_DATA_REPO", raising=False)

    class _Cfg:
        persistence = Persistence.GIT

    data_dir = Path("_data/data")
    try:
        store = store_for(_Cfg(), data_dir)
    except Exception:
        return                                   # refusing the mismatch is also correct
    assert (Path(store.repo_path) / "data").resolve() == data_dir.resolve(), (
        f"git store opened {Path(store.repo_path).resolve()}, "
        f"but the data files are written to {data_dir.resolve()}"
    )


def test_commit_and_push_commits_the_local_only_users_json_name_cache(tmp_path):
    """Claim: `commit_and_push` stages with `git add -A`, so any other file in the data
    checkout is committed and force-pushed with the three data files. `roster` writes the
    id -> display-name cache to `<data-dir>/users.json` (40 §4.2 roster, 40 §4 `--data-dir`),
    and `restore` / `purge` call `commit_and_push` without a preceding `refresh()` clean, so
    display names reach the data branch history. Violates PLAN storage ("users.json — id to
    display name cache. Local only, never committed"), 20 §8.4 (never a display name in git)
    and the module's own contract (only the three data files, §7.1-7.2)."""
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    store = GitStore(repo, author=AUTHOR)
    store.refresh()
    _write_data(repo, "restored")
    (repo / "data" / "users.json").write_text(
        json.dumps({SNIPER: "user-1", TARGET: "user-2"}), encoding="utf-8"
    )
    store.commit_and_push(local_day=DAY, large_movement=True,
                          message=f"restore {DAY} [movement:admin]", boundary=_noop)
    pushed = _git(origin, "ls-tree", "-r", "--name-only", "data").split()
    assert "data/users.json" not in pushed, f"name cache pushed to the data branch: {pushed}"


def test_purge_commit_message_carries_the_purged_user_id(git_cli):
    """Claim: `purge --rewrite-history` commits with the message `purge <UserID>`, writing
    the very user ID being erased into the data branch's permanent commit messages (readable
    by everyone with repo access). Violates 20 §8.4 ("Only counts — never a user ID") and
    40 §4.2 purge (genuine erasure)."""
    base, repo, origin, _ = git_cli
    rc = main(["purge", "--user", TARGET, "--rewrite-history", "--yes", *base])
    assert rc == 0
    messages = _git(origin, "log", "data", "--format=%B")
    assert TARGET not in messages, f"purged user ID in a commit message:\n{messages}"


def test_purge_rewrite_history_leaves_the_user_in_earlier_snapshots(git_cli):
    """Claim: `purge --rewrite-history` only prunes the live ledger and appends one commit;
    it never rewrites the `data` branch, so every earlier daily snapshot still holds the
    purged user's rows and a later `restore --from` brings them straight back. Violates
    40 §4.2 purge ("rewrites the `data` branch history to drop them from earlier daily
    snapshots, then force-pushes"; output "commits rewritten") and PLAN §6 erasure."""
    base, repo, origin, _ = git_cli
    rc = main(["purge", "--user", TARGET, "--rewrite-history", "--yes", *base])
    assert rc == 0
    holding = []
    for sha in _git(origin, "rev-list", "data").split():
        proc = subprocess.run(["git", "grep", "-q", TARGET, sha, "--", "data"],
                              cwd=str(origin), capture_output=True)
        if proc.returncode == 0:
            holding.append(sha)
    assert holding == [], f"purged user still in reachable snapshots: {holding}"


def test_restore_commit_is_invisible_to_history(git_cli):
    """Claim: `restore --from <commit>` commits with the message `restore <sha>`, which is
    not the §8.4 header `<command> <YYYY-MM-DD> [movement:admin]`; `GitStore.history()`
    therefore treats it as a foreign commit and drops it, so `snipebot history` never shows
    the restore (nor a purge, `purge <UserID>`), and neither carries the movement marker a
    large movement needs. Violates 20 §8.4 (line 1 = command + local day, movement commits
    append `[movement:admin]`), 20 §8.5 (restore is always a large movement with its own
    dated commit line) and 40 §4.2 history (every daily/movement commit is listed).

    The restored snapshot is an OLDER commit than the tip: restoring the tip itself leaves the
    three files byte-identical and makes no commit (20 §8.3 step 3, E-W4-20f)."""
    base, repo, origin, sync_sha = git_cli
    # A later day's commit whose state.json bytes differ (same content, re-serialised).
    later = GitStore(repo, author=AUTHOR)
    later.refresh()
    state_path = repo / "data" / "state.json"
    state_path.write_bytes(
        json.dumps(json.loads(state_path.read_bytes()), indent=1).encode("utf-8")
    )
    later.commit_and_push(local_day="2026-09-19", large_movement=False,
                          message="sync 2026-09-19\n\nrows +0 -0\n", boundary=_noop)
    later_sha = _git(origin, "rev-parse", "data").strip()
    assert later_sha != sync_sha
    rc = main(["restore", "--from", sync_sha, *base])
    assert rc == 0
    tip = _git(origin, "rev-parse", "data").strip()
    assert tip not in (sync_sha, later_sha)
    entries = {e.sha: e for e in GitStore(repo).history()}
    assert tip in entries, "the restore commit is missing from history()"
    assert entries[tip].is_movement


def test_empty_git_author_env_breaks_every_commit(tmp_path, monkeypatch):
    """Claim: an EMPTY SNIPEBOT_GIT_AUTHOR (what GitHub Actions exports for
    `${{ secrets.SNIPEBOT_GIT_AUTHOR }}` when that optional secret is unset — exactly the
    40 §7.2 admin workflow's env block) is used verbatim: `git -c user.name= -c user.email=
    commit` fails with "empty ident name", so every admin command fails. Violates 40 §6.1
    (SNIPEBOT_GIT_AUTHOR optional, with a documented default identity): empty must mean
    unset."""
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    monkeypatch.setenv("SNIPEBOT_GIT_AUTHOR", "")
    store = GitStore(repo)
    store.refresh()
    _write_data(repo, "one")
    store.commit_and_push(local_day=DAY, large_movement=False, message=f"sync {DAY}",
                          boundary=_noop)
    author = _git(origin, "log", "-1", "--format=%an <%ae>", "data").strip()
    assert author == DEFAULT_GIT_AUTHOR


def test_restore_accepts_a_commit_that_is_not_on_the_data_branch(tmp_path):
    """Claim: `GitStore.restore` runs `git checkout <commit> -- data/...` on any object in
    the local store, so a commit that is NOT on the data branch history (here the day's
    first commit, amended away by the second run) is silently restored. Violates 40 §4.2
    restore ("Exit 2 if the commit is not on the data branch history"): the store must
    refuse it and leave the tree untouched (it is also how a pre-rewrite snapshot would
    come back after a purge)."""
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    store = GitStore(repo, author=AUTHOR)
    store.refresh()
    _write_data(repo, "first")
    first = store.commit_and_push(local_day=DAY, large_movement=False,
                                  message=f"sync {DAY}", boundary=_noop)
    store.refresh()
    _write_data(repo, "second")
    second = store.commit_and_push(local_day=DAY, large_movement=False,
                                   message=f"sync {DAY}", boundary=_noop)
    assert second.amended and second.sha != first.sha
    store.refresh()
    on_branch = _git(repo, "rev-list", "refs/heads/data").split()
    assert first.sha not in on_branch
    with pytest.raises(Exception):
        store.restore(first.sha)
    assert (repo / "data" / "ledger.jsonl").read_bytes() == b"second-ledger\n"
