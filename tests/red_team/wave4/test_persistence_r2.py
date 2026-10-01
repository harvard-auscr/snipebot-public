"""Red team, wave 4, round 2: the git-backed `data` branch (snipebot/persistence.py).

Real git only, against a bare `origin` and clones under tmp_path; never a remote host.
"""

from __future__ import annotations

import calendar
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from snipebot import cli
from snipebot.cli import main
from snipebot.config import Persistence
from snipebot.faces import FakeFaceDetector
from snipebot.persistence import GitCommandError, GitStore, store_for
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


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "-c", "user.name=seed", "-c", "user.email=seed@example.invalid",
         "commit", "-q", "-m", message)


def _make_origin(tmp_path: Path) -> Path:
    """A bare origin whose `data` branch has one initial (non-tool) commit."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", "data", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", "-q", str(origin), str(seed)])
    (seed / "data").mkdir(exist_ok=True)
    (seed / "data" / "README").write_text("data branch\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _commit(seed, "init data branch")
    _git(seed, "push", "-q", "origin", "data")
    return origin


def _clone(origin: Path, dest: Path) -> Path:
    _run(["git", "clone", "-q", str(origin), str(dest)])
    _git(dest, "checkout", "-q", "data")
    (dest / "data").mkdir(exist_ok=True)
    return dest


def _write_data(repo: Path, marker: str) -> None:
    d = repo / "data"
    d.mkdir(exist_ok=True)
    (d / "ledger.jsonl").write_bytes(f"{marker}-ledger\n".encode())
    (d / "verdicts.jsonl").write_bytes(f"{marker}-verdicts\n".encode())
    (d / "state.json").write_bytes(f'{{"m":"{marker}"}}'.encode())


# --------------------------------------------------------------------------- CLI world


def _secs(y, mo, d, h=0) -> int:
    return calendar.timegm((y, mo, d, h, 0, 0, 0, 0, 0))


NOW_TS = f"{_secs(2026, 9, 18, 12)}.000000"
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = f"{_secs(2026, 9, 18, 10)}.000000"


def _config_yaml(path: Path) -> Path:
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
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _sync(base: list[str], slack: FakeSlack) -> int:
    return main(["sync", *base, "--no-post"], slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({}))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.delenv("SNIPEBOT_DATA_REPO", raising=False)
    monkeypatch.setenv("SNIPEBOT_GIT_AUTHOR", AUTHOR)
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    return monkeypatch


# --------------------------------------------------------------------------- findings


def test_refresh_in_a_code_checkout_switches_branches_and_cleans_untracked_files(
    tmp_path, env
):
    """Claim: GitStore.refresh() never checks that its checkout is dedicated to the data
    branch. With the shipped config.example.yaml (`persistence: git`) and the defaults
    (`--data-dir ./data`, SNIPEBOT_DATA_REPO unset), store_for maps the checkout to `.`, the
    CODE clone. refresh() then runs `git checkout -f -B data origin/data` (the code tree is
    replaced by the orphan data branch, uncommitted edits discarded by -f) and `git clean
    -fd` (the untracked config.yaml the README just told the owner to create is deleted).
    Even README's `backfill --dry-run` reaches it, since run_sync_git refreshes first.
    Violates 20 section 8.3 step 1 ("a checkout dedicated to the data branch ... so the
    protocol never switches branches under the running code") and 20 section 9.3
    (`--dry-run`: no write)."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", "main", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", "-q", str(origin), str(seed)])
    _git(seed, "checkout", "-q", "-b", "main")
    (seed / "snipebot").mkdir()
    (seed / "snipebot" / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    (seed / ".gitignore").write_text("data/\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _commit(seed, "code")
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "checkout", "-q", "--orphan", "data")
    _git(seed, "rm", "-rq", "--cached", ".")
    _commit_empty = ["-c", "user.name=seed", "-c", "user.email=seed@example.invalid",
                     "commit", "-q", "--allow-empty", "-m", "init data branch"]
    _git(seed, *_commit_empty)
    _git(seed, "push", "-q", "origin", "data")

    code = tmp_path / "code"
    _run(["git", "clone", "-q", "-b", "main", str(origin), str(code)])
    (code / "config.yaml").write_text("persistence: git\n", encoding="utf-8")   # untracked
    (code / "snipebot" / "__init__.py").write_text("VERSION = 2\n", encoding="utf-8")

    config = SimpleNamespace(persistence=Persistence.GIT)
    env.chdir(code)
    store = store_for(config, Path("data"))           # the CLI default --data-dir ./data
    try:
        store.refresh()
    except Exception:                                  # a refusal is the correct outcome
        pass

    branch = _git(code, "rev-parse", "--abbrev-ref", "HEAD").strip()
    init = code / "snipebot" / "__init__.py"
    harms = []
    if branch != "main":
        harms.append(f"code checkout switched to {branch!r}")
    if not (code / "config.yaml").exists():
        harms.append("untracked config.yaml deleted")
    if not init.exists() or init.read_text(encoding="utf-8") != "VERSION = 2\n":
        harms.append("uncommitted code edit discarded")
    assert not harms, "; ".join(harms)


def test_sync_with_data_dir_outside_the_data_repo_pushes_empty_commits(tmp_path, env):
    """Claim: store_for honours SNIPEBOT_DATA_REPO but never checks that `--data-dir` is that
    checkout's `data/`. A box set up per 40 section 6.1 (SNIPEBOT_DATA_REPO = the data
    clone) that runs with the default `--data-dir ./data` writes the three files outside the
    data checkout, and GitStore still commits (`--allow-empty`) and pushes a commit whose
    message claims `rows +1` while the data branch never receives the new row. Once the
    data branch already tracks the three files (any branch an earlier runner synced), `git
    add -A -- data/...` stages nothing and the empty commit is pushed; the baseline never
    moves, and the run exits 0. Violates 20 section 8.3 step 2 (the three files are written
    into the working tree of the data checkout and committed)."""
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    env.setenv("SNIPEBOT_DATA_REPO", str(repo))
    cfg = _config_yaml(tmp_path / "code" / "config.yaml")
    # An earlier runner (Actions) synced into the data checkout itself.
    assert _sync(["--config", str(cfg), "--data-dir", str(repo / "data")], _world()) == 0
    # The box now runs with the default data dir, outside the data checkout.
    slack = _world()
    msg2 = f"{_secs(2026, 9, 18, 11)}.000000"
    slack.post(at=msg2, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[image_file("F0FILE002", b"photo-2")])
    elsewhere = tmp_path / "code" / "data"
    rc = _sync(["--config", str(cfg), "--data-dir", str(elsewhere)], slack)
    if rc != 0:
        return                                         # a refusal is the correct outcome
    assert msg2 in (elsewhere / "ledger.jsonl").read_text(encoding="utf-8")
    tip = _git(origin, "rev-parse", "data").strip()
    message = _git(origin, "log", "-1", "--format=%B", tip)
    ledger = _git(origin, "show", f"{tip}:data/ledger.jsonl")
    assert msg2 in ledger, (
        f"sync exited 0 and pushed {message.splitlines()[0]!r}, "
        "but the data branch never received the new row"
    )


def test_git_errors_carry_git_stderr_with_local_paths(tmp_path):
    """Claim: GitStore._git and commit_and_push build GitCommandError from git's own stderr,
    and main() prints that text on the one-line `unexpected error` stderr message (the
    Actions log, readable by everyone with repo access). A fetch that fails (origin moved or
    unreachable) therefore prints the remote's absolute path or URL and the runner's
    checkout path, e.g. a box's C:/Users/<account>/... tree. cli._git_out already follows
    the rule (subcommand and exit code only, "never paths, ids or git's own stderr") and
    restore maps GitCommandError to a fixed message; persistence does not. Violates the
    log rule of 40 section 4.1/20 section 9.2 (IDs and counts only, never paths or URLs)."""
    origin = _make_origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    secret_dir = tmp_path / "home-of-user-7" / "moved.git"
    _git(repo, "remote", "set-url", "origin", str(secret_dir))
    with pytest.raises(GitCommandError) as info:
        GitStore(repo, author=AUTHOR).refresh()
    text = str(info.value).replace("\\", "/")
    assert "home-of-user-7" not in text, f"error text carries a local path: {text!r}"
