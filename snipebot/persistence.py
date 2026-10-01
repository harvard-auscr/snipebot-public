"""The `data`-branch storage backend (20 §8).

Two backends behind one `Store` protocol:

- `GitStore` runs the git-backed data-branch protocol (§8.1-8.5): one amended commit per
  local day, large movements sealed into their own commits, `--force-with-lease` push with a
  bounded lease-retry driven by the caller (`run_sync_git`, §1.4). This module owns the git
  primitives; `sync.py`'s retry wrapper calls them.
- `FilesStore` is the files-only mode (§8.6, development and rig): the three data files are
  already written atomically by `ledger.save_*`, so there is nothing to commit and no history.

The three data files live at `data/ledger.jsonl`, `data/verdicts.jsonl` and `data/state.json`
inside the repository (§7.1-7.2). `commit_and_push` commits whatever `ledger.save_*` has
already written into the working tree; it never serialises data itself.

Only counts by reason reach a commit message; no user ID, name, ts, permalink, file name or
image byte is ever written to git (§8.4). Secrets are never read here: the push is authorised
by the host's own git credentials (40 §6.1, `snipebot` never reads a git token).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .config import ConfigError, Persistence
from .ledger import count_moved_pairs

DATA_BRANCH: str = "data"          # §8, default branch name
MAX_LEASE_RETRIES: int = 3         # §8 / §1.4, bounded re-runs on a rejected lease

# Default commit identity (40 §6.1); overridable by SNIPEBOT_GIT_AUTHOR.
DEFAULT_GIT_AUTHOR = "snipebot <snipebot@example.invalid>"  # no GitHub account can own it

# The three committed data files, relative to the repository root (§7.1-7.2). Forward slashes
# are the git pathspec form on every platform.
_DATA_FILES = ("data/ledger.jsonl", "data/verdicts.jsonl", "data/state.json")
_VERDICTS_PATH = "data/verdicts.jsonl"

# A commit made by this tool: `<command> <YYYY-MM-DD>[ [movement:<trigger>]]` (§8.4).
_HEADER_RE = re.compile(
    r"^(?P<command>\S+)\s+(?P<day>\d{4}-\d{2}-\d{2})"
    r"(?:\s+\[movement:[^\]]+\])?\s*$"
)
_MOVEMENT_MARKER = "[movement:"


class LeaseRejected(Exception):
    """The `--force-with-lease` push was rejected: another runner pushed first (§8.3 step 6).
    The caller (`run_sync_git`, §1.4) refreshes to the new tip and re-runs the whole sync."""


class GitCommandError(RuntimeError):
    """A git subprocess exited non-zero for a reason other than a rejected lease."""


@dataclass(frozen=True)
class CommitResult:
    sha: str                    # the daily/movement commit that landed this run's change
    sealed_sha: str | None      # the sealed pre-movement restore point (large movement only)
    amended: bool               # today's daily commit was amended in place
    pushed: bool                # the push to the data branch succeeded


@dataclass(frozen=True)
class HistoryEntry:
    sha: str
    day: str
    message: str
    moved_pairs: int            # verdict pairs moved vs the previous sealed commit (40 §4.2)
    is_movement: bool


class Store(Protocol):
    def refresh(self) -> None:
        """git: fetch, check out the branch tip, reset the tree to it (§8.3 step 1); also the
        reset a lease retry lands on (§1.4). files: no-op."""

    def baseline_verdicts(self, local_day: str) -> bytes | None:
        """The `verdicts.jsonl` of the most recent sealed commit for `local_day` (the cumulative
        baseline, §8.1-8.2). The tip is sealed unless it is `local_day`'s own amendable (daily)
        commit, in which case the sealed baseline is its parent. `None` means an empty baseline
        (empty repository, or files mode §8.6)."""

    def commit_and_push(
        self,
        *,
        local_day: str,
        large_movement: bool,
        message: str,
        boundary: Callable[[str], None],
    ) -> CommitResult:
        """git: §8.3 steps 2-6 over the files `ledger.save_*` already wrote into the tree.
        files: nothing to commit -> CommitResult("", None, False, False)."""

    def history(self) -> list[HistoryEntry]:
        """git: the branch's commits, newest first. files: []."""

    def restore(self, commit: str) -> None:
        """git: bring that snapshot's three data files back into the tree (the caller commits
        them as a RESTORE movement). files: raise NotImplementedError."""


def _parse_author(author: str) -> tuple[str, str]:
    """`Name <email>` -> (name, email). A bare string with no angle brackets is all name."""
    m = re.match(r"^\s*(?P<name>.*?)\s*<(?P<email>[^>]*)>\s*$", author)
    if m:
        return m.group("name"), m.group("email")
    return author.strip(), ""


def _parse_header(message: str) -> tuple[str, bool] | None:
    """First line -> (day, is_movement) for a tool commit, or None for any other commit."""
    first = message.splitlines()[0] if message else ""
    m = _HEADER_RE.match(first)
    if not m:
        return None
    return m.group("day"), (_MOVEMENT_MARKER in first)


class GitStore:
    """The git-backed data-branch protocol (§8.1-8.5)."""

    def __init__(
        self,
        repo_path: Path,
        *,
        branch: str = DATA_BRANCH,
        author: str | None = None,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.branch = branch
        # The directory the run writes the three files into, when known (set by store_for).
        self.data_dir: Path | None = None
        # An empty or blank SNIPEBOT_GIT_AUTHOR (what Actions exports for an unset optional
        # secret) means unset: fall back to the default identity (40 §6.1).
        author_str = author if author is not None else (
            (os.environ.get("SNIPEBOT_GIT_AUTHOR") or "").strip() or DEFAULT_GIT_AUTHOR
        )
        self._author_name, self._author_email = _parse_author(author_str)

    # -- git plumbing --------------------------------------------------------

    def _run(self, args: list[str], *, text: bool = True) -> subprocess.CompletedProcess:
        # cwd is passed explicitly (the working directory is never assumed) and autocrlf is
        # pinned off so blob reads and writes are byte-stable across platforms.
        return subprocess.run(
            ["git", "-c", "core.autocrlf=false", *args],
            cwd=str(self.repo_path),
            capture_output=True,
            text=text,
            # Git writes paths and messages as UTF-8; the locale code page (cp1252 on Windows)
            # cannot decode every such path and would drop stdout (20 §8.3 step 1).
            encoding="utf-8" if text else None,
            errors="surrogateescape" if text else None,
        )

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        proc = self._run(list(args))
        if proc.returncode != 0:
            # The subcommand and exit code only: git's own stderr/stdout and the arguments carry
            # remote URLs and local checkout paths, and this text reaches the one-line stderr
            # error of the run log (40 §4, 20 §9.2; the cli._git_out convention).
            i = 0
            while i < len(args) and args[i] == "-c":
                i += 2
            sub = args[i] if i < len(args) else "command"
            raise GitCommandError(f"git {sub} failed ({proc.returncode})")
        return proc

    def _commit_identity(self) -> list[str]:
        # Sets both author and committer for this commit; no global config is relied on.
        return [
            "-c", f"user.name={self._author_name}",
            "-c", f"user.email={self._author_email}",
        ]

    def _tip(self) -> tuple[str, str] | None:
        """(sha, message) of the branch tip, or None on an unborn branch."""
        head = self._run(["rev-parse", "--verify", "--quiet", "HEAD"])
        if head.returncode != 0 or not head.stdout.strip():
            return None
        proc = self._git("log", "-1", "--format=%H%x1f%B")
        sha, _, body = proc.stdout.partition("\x1f")
        return sha.strip(), body

    # -- Store protocol ------------------------------------------------------

    def _ensure_dedicated_checkout(self) -> None:
        """Refuse, tree untouched, unless this checkout is dedicated to the data branch (§8.3
        step 1: the protocol never switches branches under the running code). A checkout on
        another branch with commits (e.g. the code clone that the default `--data-dir ./data`
        maps to) or one that contains the running package is refused; an unborn or detached
        HEAD, or one already on the data branch, carries on."""
        message = ("refresh: checkout is not dedicated to the data branch "
                   "(set SNIPEBOT_DATA_REPO)")
        ref = self._run(["symbolic-ref", "-q", "--short", "HEAD"])
        current = ref.stdout.strip() if ref.returncode == 0 else ""
        if current and current != self.branch:
            born = self._run(["rev-parse", "--verify", "--quiet", "HEAD"])
            if born.returncode == 0 and born.stdout.strip():
                raise GitCommandError(message)
        top = self._run(["rev-parse", "--show-toplevel"])
        if top.returncode == 0 and top.stdout.strip():
            package_dir = Path(__file__).resolve().parent
            if package_dir.is_relative_to(Path(top.stdout.strip()).resolve()):
                raise GitCommandError(message)

    def refresh(self) -> None:
        if self.data_dir is not None and (
            self.data_dir.resolve() != (self.repo_path / "data").resolve()
        ):
            # Files written anywhere but the data checkout's own `data/` are never staged, so
            # the commit would carry nothing (§8.3 step 2, 40 §6.1). Refuse before any write;
            # the message is fixed so no path reaches the log (40 §4).
            raise ConfigError("--data-dir must be the data/ directory of SNIPEBOT_DATA_REPO")
        self._ensure_dedicated_checkout()
        self._git("fetch", "origin", self.branch)
        # Force the local branch to the fetched tip, discarding any working-tree state; this is
        # both the run's starting tree (§8.3 step 1) and the reset a lease retry lands on.
        self._git("checkout", "-f", "-B", self.branch, f"origin/{self.branch}")
        # `checkout -f` discards tracked changes but leaves untracked residue (e.g. a
        # crash-orphaned `data/*.tmp` from an interrupted atomic write). Remove it so the
        # working tree truly equals the fetched tip before step 2's `git add -A` (§8.3 step 1).
        self._git("clean", "-fd")

    def baseline_verdicts(self, local_day: str) -> bytes | None:
        tip = self._tip()
        if tip is None:
            return None
        sha, message = tip
        # The cumulative baseline is the most recent SEALED commit's verdicts (§8.1). The tip is
        # sealed EXCEPT when it is `local_day`'s own amendable (non-movement) commit: that commit
        # is still being amended today, so its stable parent (the last sealed state before today's
        # amends) is the baseline. A movement tip is itself sealed; a PREVIOUS day's daily commit
        # is sealed (it will never be amended again), so on a new day's first run the tip itself is
        # the baseline; a non-tool tip (e.g. an initial empty commit) has no verdicts and yields an
        # empty baseline.
        if self._is_amendable_tip(message, local_day):   # today's live daily commit -> parent
            parent = self._run(["rev-parse", "--verify", "--quiet", f"{sha}^"])
            if parent.returncode != 0 or not parent.stdout.strip():
                return None
            sealed = parent.stdout.strip()
        else:
            sealed = sha
        blob = self._run(["show", f"{sealed}:{_VERDICTS_PATH}"], text=False)
        if blob.returncode != 0:
            return None
        return blob.stdout

    def commit_and_push(
        self,
        *,
        local_day: str,
        large_movement: bool,
        message: str,
        boundary: Callable[[str], None],
    ) -> CommitResult:
        # Stage only the three data files (deletions included): untracked residue such as the
        # local-only users.json name cache must never reach the data branch (20 §7.1, §8.4).
        self._git("add", "-A", "--", *_DATA_FILES)
        tip = self._tip()
        if tip is not None:
            # The three files' staged bytes equal the tip's: no commit, no empty amend, no push
            # (§8.3 step 3); the caller prints `no change` on the empty sha (40 §4.3).
            same = self._run(["diff", "--cached", "--quiet", "HEAD", "--", *_DATA_FILES])
            if same.returncode == 0:
                return CommitResult(sha="", sealed_sha=None, amended=False, pushed=False)
            if same.returncode != 1:
                raise GitCommandError(f"git diff failed ({same.returncode})")
        amendable = (
            not large_movement
            and tip is not None
            and self._is_amendable_tip(tip[1], local_day)
        )

        sealed_sha: str | None = None
        amended = False
        boundary("before_commit")
        if large_movement:
            # A large movement never amends: the current tip is the pre-movement restore point,
            # and this run is a new sealed commit on top of it (§8.3 step 3).
            sealed_sha = tip[0] if tip is not None else None
            self._git(*self._commit_identity(), "commit", "--allow-empty", "-m", message)
        elif amendable:
            self._git(
                *self._commit_identity(), "commit", "--amend", "--allow-empty", "-m", message
            )
            amended = True
        else:
            # First activity of the local day: day D's first (amendable) commit.
            self._git(*self._commit_identity(), "commit", "--allow-empty", "-m", message)

        new_sha = self._git("rev-parse", "HEAD").stdout.strip()
        boundary("after_commit")

        boundary("before_push")
        push = self._run(
            ["push", "--force-with-lease", "origin", self.branch]
        )
        if push.returncode != 0:
            err = f"{push.stderr or ''}{push.stdout or ''}"
            low = err.lower()
            # A `--force-with-lease` loss to an advanced ref is uniquely marked by "stale info"
            # (git prints "stale info" / "force-with-lease" when the lease no longer matches the
            # remote tip). Only that is a retryable lease loss (§8.3 step 6, §1.4). Every other
            # non-zero push — a protected / denyNonFastForwards branch's "[remote rejected] ...
            # (non-fast-forward)", "denying non-fast-forward", auth failures — is a permanent
            # error and must fail fast, not drive the concurrency retry to exit 9.
            if "stale info" in low or "force-with-lease" in low:
                # Discard nothing here: run_sync_git's next refresh() resets the tree to the
                # new tip (§8.3 step 6, §1.4).
                raise LeaseRejected("force-with-lease rejected")
            raise GitCommandError(f"git push failed ({push.returncode})")
        boundary("after_push")

        return CommitResult(sha=new_sha, sealed_sha=sealed_sha, amended=amended, pushed=True)

    def history(self) -> list[HistoryEntry]:
        # refs/heads/<branch>, not the bare name, so a `data/` directory never makes the
        # revision ambiguous with a pathspec.
        proc = self._git("log", f"refs/heads/{self.branch}", "--format=%H%x1f%B%x1e")
        entries: list[HistoryEntry] = []
        for record in proc.stdout.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, _, message = record.partition("\x1f")
            sha = sha.strip()
            header = _parse_header(message)
            if header is None:                 # skip non-tool commits (e.g. an initial commit)
                continue
            day, is_movement = header
            # Deltas are computed by diffing this commit's committed verdicts.jsonl against the
            # previous sealed commit's — this commit's actual parent (40 §4.2, 00-data §4) — never
            # by re-judging a baseline ledger and never from the message text.
            this_verdicts = self._verdicts_at(sha)
            parent_verdicts = self._verdicts_at(f"{sha}^")
            entries.append(
                HistoryEntry(
                    sha=sha,
                    day=day,
                    message=message.strip("\n"),
                    moved_pairs=count_moved_pairs(parent_verdicts, this_verdicts),
                    is_movement=is_movement,
                )
            )
        return entries                          # git log is already newest first

    def _verdicts_at(self, rev: str) -> str:
        """The committed `data/verdicts.jsonl` text at `rev`, or "" when the path or the
        revision is absent (an unborn parent, or a commit with no verdicts blob)."""
        blob = self._run(["show", f"{rev}:{_VERDICTS_PATH}"], text=False)
        if blob.returncode != 0:
            return ""
        return blob.stdout.decode("utf-8")

    def restore(self, commit: str) -> None:
        # Bring the snapshot's three files into the index and working tree together (§4.2); the
        # caller commits them as a RESTORE large movement. A commit that is not on the data
        # branch history (amended away, or a pre-rewrite snapshot) is refused, tree untouched.
        on_branch = self._run(
            ["merge-base", "--is-ancestor", commit, f"refs/heads/{self.branch}"]
        )
        if on_branch.returncode != 0:
            raise ValueError("commit is not on the data branch history")
        self._git("checkout", commit, "--", *_DATA_FILES)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _is_amendable_tip(message: str, local_day: str) -> bool:
        """True iff the tip is this tool's non-movement commit for `local_day` (§8.1)."""
        header = _parse_header(message)
        if header is None:
            return False
        day, is_movement = header
        return day == local_day and not is_movement


class FilesStore:
    """Files-only mode (§8.6): no git, no lease, no history. `ledger.save_*` has already
    written the three files atomically; this backend has nothing left to do."""

    def __init__(self) -> None:
        pass

    def refresh(self) -> None:
        return None

    def baseline_verdicts(self, local_day: str) -> bytes | None:
        return None                             # empty baseline (§8.6)

    def commit_and_push(
        self,
        *,
        local_day: str,
        large_movement: bool,
        message: str,
        boundary: Callable[[str], None],
    ) -> CommitResult:
        return CommitResult(sha="", sealed_sha=None, amended=False, pushed=False)

    def history(self) -> list[HistoryEntry]:
        return []

    def restore(self, commit: str) -> None:
        raise NotImplementedError("restore needs git history; unavailable under persistence: files")


def store_for(config, data_dir: Path) -> Store:
    """Select the backend for `config.persistence` (§8, 40 §6.1). Under git the checkout is
    named by SNIPEBOT_DATA_REPO; when that is unset or empty it is the checkout whose `data/`
    is `data_dir` (its parent), so the default `--data-dir ./data` still maps to `.` and a
    workflow's `--data-dir _data/data` maps to the `_data` checkout, never the code tree.
    Under files the data files were already written to `data_dir` by `ledger.save_*`, so the
    backend only reports 'nothing to commit'."""
    if config.persistence == Persistence.GIT:
        repo = (os.environ.get("SNIPEBOT_DATA_REPO") or "").strip()
        store = GitStore(Path(repo) if repo else Path(data_dir).parent)
        # refresh() (the protocol's first step, before any write) refuses a data dir that is
        # not this checkout's own `data/` (§8.3 step 2).
        store.data_dir = Path(data_dir)
        return store
    return FilesStore()
