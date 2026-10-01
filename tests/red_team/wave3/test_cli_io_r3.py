"""Wave-3 red-team round 3 — INVARIANTS for snipebot/cli.py end to end.

Breaker tests: each asserts a behaviour the spec REQUIRES and is expected to FAIL on the
current code (a passing test is not a finding). Slack never touches the network: every
Slack-touching command is driven either through an injected FakeSlack or through the REAL
transport class `SlackWebClient` over a canned in-memory WebClient stub, and the detector
is always the injected FakeFaceDetector.

The theme is command OUTPUT conformance: 40-config-cli.md §4 fixes the channel of every
command's contract — "Logs go to stderr; command output to stdout" — and each command's
`Output` row in §4.2 (plus the §4.1 `Prints commit` column) then says what stdout must
carry. The handlers emit only the §4.3 confirming block and route the mandated per-command
summary/verdict/commit to stderr logs (or nowhere), so stdout is missing contract output.

Spec anchors:
  * 40-config-cli.md §4 (stdout vs stderr), §4.1 (Writes / Prints-commit table),
    §4.2 (`sync`, `backfill`, `veto`, `rules bump` Output rows), §4.3 (confirming block).
"""

from __future__ import annotations

import calendar
from pathlib import Path

import yaml

from snipebot import cli
from snipebot.cli import main
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file
from snipebot.ts import US_PER_SECOND

CHANNEL = "C0MAIN01"
BOT = "U0BOT"
ADMIN = "U0ADMIN"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 10)


def _config_dict(*, persistence="files"):
    return {
        "enabled": True,
        "persistence": persistence,
        "slack": {"channel": CHANNEL},
        "timezone": "UTC",
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}],
        "rules": {
            "cooldown": {"minutes": 15, "scope": "pair", "rejected_attempts_reset": False},
            "multi_tag": "per_target", "max_targets_per_message": None,
            "edit_grace_minutes": 10, "max_snipes_per_target_per_day": None,
            "allow_self": False, "allow_bots": False, "count_thread_replies": False,
            "count_image_links": False, "allow_video": False, "selfie_bonus": False,
        },
        "players": {"count_intra_group": True,
                    "groups": {"fam": ["U0AAAA1", "U0BBBB1"]}, "extras": []},
        "consent": {
            "veto": {"emoji": "no_entry_sign", "by": ["admins"]},
            "optout_messages": [], "opted_out": [],
        },
        "admins": [ADMIN],
        "feedback": {
            "reactions": {
                "counted": "white_check_mark", "cooldown": "hourglass_flowing_sand",
                "untagged": None, "not_counted": "x", "selfie": None,
            },
            "review": {"min_targets": 5, "emoji": "question"},
        },
        "reports": [],
    }


def _write_config(tmp_path: Path, **kw) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config_dict(**kw), sort_keys=False), encoding="utf-8")
    return path


def _data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d


def _base(cfg: Path, data: Path):
    return ["--config", str(cfg), "--data-dir", str(data)]


def _world() -> FakeSlack:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        "U0AAAA1": FakeUser(id="U0AAAA1", display_name="Alex"),
        "U0BBBB1": FakeUser(id="U0BBBB1", display_name="Bailey"),
        ADMIN: FakeUser(id=ADMIN, display_name="Admin"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    slack.post(at=MSG_TS, user="U0AAAA1", channel=CHANNEL, text="<@U0BBBB1>",
               files=[image_file("F01", b"snap")])
    return slack


def _detector():
    return FakeFaceDetector({})


# --------------------------------------------------------------------------- #
# Finding 1 — `sync` prints NO run summary to stdout.
# §4 fixes the channel: "Logs go to stderr; command output to stdout." §4.2 `sync`
# Output is "one summary line per run: rows scanned, verdict flips by reason, messages
# flagged for review (`needs_review`), `selfies`, `faces_fetched`, `ambiguous_selfie`
# and `repost` counts, digests posted/revised, delete-breaker state; then the produced
# commit SHA(s) and the pushed ref". The handler prints only the §4.3 confirming block
# (`pushed …` + `moved:`); every enumerated count is emitted to stderr via `_log`
# (and `rows scanned` / `selfies` / `faces_fetched` / `ambiguous_selfie` / `repost` /
# `needs_review` are not even carried out of `run_sync` on `SyncResult`).
# --------------------------------------------------------------------------- #

def test_sync_run_summary_absent_from_stdout(tmp_path, monkeypatch, capsys):
    """40-config-cli.md §4: "Logs go to stderr; command output to stdout." §4.2 `sync`
    Output: "one summary line per run: rows scanned, verdict flips by reason, messages
    flagged for review ... `selfies` ... `faces_fetched` ... `ambiguous_selfie` ... and
    `repost` counts, digests posted/revised, delete-breaker state ...". So `sync`'s
    stdout must carry that summary; the handler emits it only to the stderr log."""
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)

    rc = main(["sync", "--no-react", "--no-post", *_base(cfg, data)],
              slack_factory=lambda: _world(), detector_factory=_detector)
    assert rc == 0
    # sanity: the pass counted the intra-group snipe, so a summary genuinely exists.
    assert [r.ts for r in load_ledger(data / "ledger.jsonl")] == [MSG_TS]

    out = capsys.readouterr().out.lower()
    assert any(tok in out for tok in ("scanned", "digests", "selfies", "faces", "breaker")), (
        "§4.2 requires sync's run summary (rows scanned / selfies / faces_fetched / "
        "digests posted-revised / delete-breaker state) on stdout, but the handler prints "
        f"only the §4.3 confirming block and logs the summary to stderr; stdout was: {out!r}"
    )


# --------------------------------------------------------------------------- #
# Finding 2 — `backfill --dry-run` prints NO verdict counts / audit list to stdout.
# §4.2 `backfill` Output: "verdict counts by reason; with `--dry-run` also the **L8
# audit list** ...". The dry-run branch prints the single literal line
# "dry run — no writes (audit list on stderr)" — no verdict counts by reason and no
# audit list reach stdout at all.
# --------------------------------------------------------------------------- #

def test_backfill_dry_run_counts_absent_from_stdout(tmp_path, monkeypatch, capsys):
    """40-config-cli.md §4.2 `backfill` Output: "verdict counts by reason; with
    `--dry-run` also the **L8 audit list**". A dry-run backfill over a window that
    contains a counted snipe must therefore print the by-reason verdict counts (and the
    audit list) to stdout; the handler prints only "dry run — no writes (audit list on
    stderr)"."""
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)

    rc = main(["backfill", "--from", "fall-2026", "--dry-run", *_base(cfg, data)],
              slack_factory=lambda: _world(), detector_factory=_detector)
    assert rc == 0

    out = capsys.readouterr().out.lower()
    # the one counted intra-group snipe means the by-reason counts include `counted`;
    # the placeholder line "audit list on stderr" is not the by-reason counts, so match
    # the reason token itself, not the word "audit".
    assert "counted" in out, (
        "§4.2 backfill --dry-run must emit the verdict counts by reason (and the L8 audit "
        "list) on stdout, but the handler routed them to stderr and printed only the "
        f"placeholder line 'dry run — no writes (audit list on stderr)'; stdout was: {out!r}"
    )


# --------------------------------------------------------------------------- #
# Finding 3 — `veto` does not print the message's NEW VERDICT to stdout.
# §4.2 `veto` Output lists three things: "the message's new verdict, verdict flips by
# reason, the produced commit SHA". "the message's new verdict" is distinct from
# "verdict flips by reason" (the §4.3 `moved:` line). The handler prints only the §4.3
# confirming block, so the vetoed message's resulting verdict (VETOED) never appears.
# --------------------------------------------------------------------------- #

def test_veto_new_verdict_absent_from_stdout(tmp_path, monkeypatch, capsys):
    """40-config-cli.md §4.2 `veto` Output: "the message's new verdict, verdict flips by
    reason, the produced commit SHA". After a `veto` the message's new verdict (VETOED,
    00-data §4) must be printed to stdout; the handler prints only the §4.3 confirming
    block and never the per-message verdict."""
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)

    assert main(["sync", "--no-react", "--no-post", *_base(cfg, data)],
                slack_factory=lambda: _world(), detector_factory=_detector) == 0
    capsys.readouterr()  # drop the sync output

    rc = main(["veto", "--ts", MSG_TS, "--by", ADMIN, *_base(cfg, data)],
              slack_factory=lambda: _world(), detector_factory=_detector)
    assert rc == 0

    out = capsys.readouterr().out.lower()
    assert "veto" in out, (
        "§4.2 requires `veto` to print the message's new verdict (VETOED) to stdout, but "
        f"the handler emits only the §4.3 confirming block; stdout was: {out!r}"
    )


# --------------------------------------------------------------------------- #
# `rules bump` only ever edits config.yaml on the code branch, never the data
# branch or its store (ruling E22, superseding the prior §4.1/§4.2/§7.2 reading
# that treated it as a data-branch movement with a data commit). The CLI never
# commits or pushes anything itself — under either persistence mode it just
# rewrites config.yaml locally and prints a local confirmation plus a diff
# summary; the admin workflow's own separate step is what commits config.yaml
# on the code branch and echoes that commit's SHA.
# --------------------------------------------------------------------------- #

def test_rules_bump_git_prints_no_data_movement(tmp_path, monkeypatch, capsys):
    """E22: `rules bump` must never print a data-branch movement/pushed-data line,
    under either persistence mode — it only ever touches config.yaml on the code
    branch, and prints a local confirmation plus diff summary instead."""
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write_config(tmp_path, persistence="git")
    data = _data_dir(tmp_path)

    rc = main(["rules", "bump", "--effective-from", "2030-01-01", *_base(cfg, data)])
    assert rc == 0

    out = capsys.readouterr().out.lower()
    assert not any(tok in out for tok in ("movement", "pushed data")), (
        "E22 requires `rules bump` to never claim a data-branch commit/push, since it "
        f"only ever edits config.yaml on the code branch; stdout was: {out!r}"
    )
    assert "config.yaml updated" in out
    assert "entries" in out or "entry" in out
