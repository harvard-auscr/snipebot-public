"""Wave 4 / round 1 breaker on the cross-module joints.

Each test FAILS on the current code for exactly the reason in its docstring and would pass
once the code conforms to the quoted spec sentence. Offline only: a FakeSlack world, files
persistence under tmp_path, no git, no network.
"""

from __future__ import annotations

import ast
import calendar
from pathlib import Path

import yaml

from snipebot import cli, doctor
from snipebot import sync as sync_mod
from snipebot.cli import main
from snipebot.config import load_config
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_state
from snipebot.persistence import FilesStore
from snipebot.sync import Command, run_sync
from snipebot.ts import US_PER_SECOND, parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import make_config, roster_of

PKG = Path(__file__).resolve().parents[3] / "snipebot"

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
OTHER = "U0AAA003"
ADMIN = "U0AAA009"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = parse_ts(NOW_TS)
MSG_TS = _ts(2026, 9, 18, 10, micro=1)
MSG2_TS = _ts(2026, 9, 18, 11, micro=1)


def _photo(n: int) -> dict:
    return {
        "id": f"F0FILE{n:03d}",
        "mimetype": "image/jpeg",
        "name": f"photo-{n}.jpg",
        "size": 1000 + n,
        "original_w": 100,
        "original_h": 100,
        "thumb_1024": f"https://fixture.invalid/thumb/{n}",
        "url_private_download": f"https://fixture.invalid/dl/{n}",
        "_bytes": f"photo-{n}".encode(),
    }


def _users() -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for i, uid in enumerate((SNIPER, TARGET, OTHER, ADMIN), start=1):
        out[uid] = FakeUser(id=uid, display_name=f"user-{i}")
    return out


def _top_level_assigns(module_file: Path) -> set[str]:
    tree = ast.parse(module_file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


# --------------------------------------------------------------------------- #
# 1. The 20 §8.4 commit body never carries the "<other changed status/reason>" lines
# --------------------------------------------------------------------------- #

def test_commit_body_carries_other_changed_reason_lines(tmp_path, monkeypatch):
    """20 §8.4 pins the commit body as `rows`, `counted`, `cooldown`, `deleted`, `selfie`,
    `repost` and then "`<other changed status/reason>: +<Δ>`" -- "verdict deltas by
    `Status`/`Reason`"; 40 §4.3's own example of the resulting `moved:` line is
    "`counted +3 cooldown -1 late_tag +2`". `sync._verdict_reason_counts` /
    `_commit_message` only ever emit the five fixed names (`_COMMIT_DELTA_ORDER`), so a CLI
    veto of the only counted snipe commits a body with no `vetoed` line at all: the reason
    the message moved to (VETOED) is absent from the committed record `history` reads.
    """
    captured: list[str] = []

    class _CapturingStore(FilesStore):
        def commit_and_push(self, **kw):
            captured.append(kw["message"])
            return super().commit_and_push(**kw)

    monkeypatch.setattr(sync_mod, "store_for", lambda config, data_dir: _CapturingStore())

    cfg = make_config(roster=roster_of({SNIPER: "fam", TARGET: "fam"}), admins=(ADMIN,))
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=_users())
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[_photo(1)])
    data = tmp_path / "data"
    data.mkdir()
    kw = dict(detector=FakeFaceDetector({}), ledger_path=data / "ledger.jsonl",
              state_path=data / "state.json", no_post=True, no_react=True)

    r1 = run_sync(slack, cfg, now_us=NOW_US, **kw)
    assert r1.exit_code == 0
    r2 = run_sync(slack, cfg, now_us=NOW_US + 60 * US_PER_SECOND, command=Command.VETO,
                  veto_ts=MSG_TS, veto_by=ADMIN, **kw)
    assert r2.exit_code == 0 and r2.target_verdict is not None
    assert "vetoed" in r2.target_verdict

    body = captured[-1].splitlines()[2:]
    first_tokens = {line.split()[0].rstrip(":") for line in body if line.strip()}
    assert "vetoed" in first_tokens, (
        "20 §8.4 requires an `<other changed status/reason>: +<Δ>` line for the VETOED "
        f"flip; the committed body was:\n{captured[-1]}"
    )


# --------------------------------------------------------------------------- #
# 2. purge re-derives verdicts.jsonl with the config seeds folded in, so doctor
#    DOC-VERDICTS-FRESH (state set only) calls the file it just wrote stale
# --------------------------------------------------------------------------- #

def _config_dict(seeds: list[str]) -> dict:
    return {
        "enabled": True,
        "persistence": "files",
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
        "players": {"count_intra_group": True,
                    "groups": {"fam": [SNIPER, TARGET, OTHER]}, "extras": []},
        "consent": {"veto": {"emoji": "no_entry_sign", "by": ["admins"]},
                    "optout_messages": [], "opted_out": seeds},
        "admins": [ADMIN],
        "feedback": {
            "reactions": {"counted": "white_check_mark", "cooldown": "hourglass_flowing_sand",
                          "untagged": None, "not_counted": "x", "selfie": None},
            "review": {"min_targets": 5, "emoji": "question"},
        },
        "reports": [],
    }


def test_purge_rewrites_verdicts_that_doctor_verdicts_fresh_calls_stale(tmp_path, monkeypatch):
    """40 §2 (after ReportSpec): "`opted_out` at runtime is the durable observed set in
    `state.json` ... the config `opted_out:` seeds ... are folded into that set on the first
    run that observes them ... `evaluate` receives the state set, not the config seeds."
    40 §5.1 `DOC-VERDICTS-FRESH` / 20 §7.3 compare `verdicts.jsonl` with a fresh evaluate
    over the durable ledger and `state.opted_out` (doctor.py does exactly that).

    `cli._cmd_purge` recomputes `verdicts.jsonl` through `cli._opted_out`, which returns
    `state.opted_out | config.consent.seed_opted_out`. When a seed was added to config
    after the last sync (not yet folded into state), purge writes verdicts with that
    target opted out, and doctor immediately reports DOC-VERDICTS-FRESH as a FAIL (exit 10)
    on the file purge itself just wrote.
    """
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    data = tmp_path / "data"
    data.mkdir()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(_config_dict([]), sort_keys=False), encoding="utf-8")

    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=_users())
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[_photo(1)])
    slack.post(at=MSG2_TS, user=OTHER, channel=CHANNEL, text=f"<@{SNIPER}>",
               files=[_photo(2)])
    argv = ["--config", str(cfg_path), "--data-dir", str(data)]
    rc = main(["sync", "--no-react", "--no-post", *argv], slack_factory=lambda: slack,
              detector_factory=lambda: FakeFaceDetector({}))
    assert rc == 0

    # The owner seeds TARGET's opt-out in config; before the next sync folds it in, an
    # admin purges an unrelated user.
    cfg_path.write_text(yaml.safe_dump(_config_dict([TARGET]), sort_keys=False),
                        encoding="utf-8")
    rc = main(["purge", "--user", OTHER, *argv])
    assert rc == 0

    config = load_config(cfg_path)
    state = load_state(data / "state.json")
    _rows, results = doctor._check_ledger_and_verdicts(
        config, data / "ledger.jsonl", state, data / "verdicts.jsonl")
    fresh = next(r for r in results if r.id == "DOC-VERDICTS-FRESH")
    assert fresh.ok, (
        "purge wrote verdicts.jsonl from state + config seeds; DOC-VERDICTS-FRESH "
        "(state set only, 40 §2) reports it stale"
    )


# --------------------------------------------------------------------------- #
# 3. H24_US is defined twice (periods.py and sync.py)
# --------------------------------------------------------------------------- #

def test_h24_us_is_defined_in_exactly_one_module():
    """00 §10: every shared name "is **defined exactly once**, in the file and section
    below; every other file imports it by reference and never redefines". The index homes
    `H24_US` in 20 §6 (the digest passes, §6.2: `H24_US = 24 * 60 * 60 * US_PER_SECOND`),
    i.e. beside `post_digests` in sync.py. periods.py assigns its own module-level
    `H24_US` as well (unused there), so the 24 h window constant has two definitions that
    can drift apart.
    """
    homes = sorted(p.name for p in PKG.glob("*.py") if "H24_US" in _top_level_assigns(p))
    assert homes == ["sync.py"], f"H24_US defined at module level in: {homes}"


# --------------------------------------------------------------------------- #
# 4. doctor.py redefines Exit members instead of importing Exit
# --------------------------------------------------------------------------- #

def test_doctor_does_not_redefine_exit_codes():
    """00 §10 / 40 §4.4: `Exit` (the exit-code enum, incl. `OK = 0` and
    `DOCTOR_FAILED = 10`) is a shared name defined once, in 40 §4.4 (cli.py); "every other
    file imports it by reference and never redefines or renames it". doctor.py declares its
    own module-level `OK = 0` and `DOCTOR_FAILED = 10` and returns those, so a renumbering
    of `Exit` would silently leave `doctor` exiting with the old codes.
    """
    exit_members = {m.name for m in cli.Exit}
    redefined = sorted(_top_level_assigns(PKG / "doctor.py") & exit_members)
    assert not redefined, f"doctor.py redefines Exit members: {redefined}"
