"""Wave 2 red-team, round 2 (hostile input + failure injection): a player farming
points or dodging gates end to end through `snipebot.sync.run_sync` with `FakeSlack`
+ `FakeFaceDetector` + FilesStore.

Every test asserts a rule the spec/plan states and the current code violates, so each
FAILS on the code under attack. Helpers mirror tests/_helpers_sync.py + tests/test_sync.py
(copied, not imported beyond the shared builders, per the wave brief).
"""

from __future__ import annotations

from pathlib import Path

from snipebot.faces import FakeFaceDetector
from snipebot.sync import Command, SyncResult, run_sync
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


def _users(*ids: str) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in ids:
        out[uid] = FakeUser(id=uid)
    return out


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack, config, tmp_path, *, now, detector=None, **kw) -> SyncResult:
    led, st = _paths(tmp_path)
    return run_sync(
        slack, config, detector=detector or FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now), **kw,
    )


# ---------------------------------------------------------------------------
# 1. Consent: opting out must not tear the bot's existing ✅ off old photos.
# ---------------------------------------------------------------------------

def test_optout_removes_existing_counted_reaction(tmp_path):
    """PLAN.md section 6 (Consent): "The bot's existing ✅ on their photos stay in place
    (owner, 2026-09-22): removing several at once would announce the opt-out to the
    channel. Reaction convergence therefore never removes a reaction because of an
    opt-out." 00-data.md section 4 (Reacted): a `SENDER_OPTED_OUT` message "stays silent
    — an opt-out in particular must not be signalled".

    Farming/consent break: A snipes B while on the roster; the bot adds ✅ and persists.
    On a later sync A has opted out, so the row re-evaluates to SENDER_OPTED_OUT and the
    desired reaction set collapses to empty. `_converge_reactions` then issues a
    reactions.remove for the observed ✅ — announcing the opt-out to the whole channel,
    exactly what plan section 6 forbids. It must never remove a reaction because of an
    opt-out.
    """
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])

    # Run 1: A not opted out -> counted -> the bot places ✅.
    cfg1 = make_config(roster=roster)
    r1 = _run(slack, cfg1, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r1.reactions_added >= 1  # precondition: the ✅ is now on the message

    # Run 2: A has opted out. The ✅ must stay in place (no removal because of opt-out).
    cfg2 = make_config(roster=roster, seed_opted_out=("U0A",))
    r2 = _run(slack, cfg2, tmp_path, now=mkts(2026, 9, 18, 12, 30))
    assert r2.reactions_removed == 0


# ---------------------------------------------------------------------------
# 2. Hostile payload: one malformed file object must not tear down the run.
# ---------------------------------------------------------------------------

def test_check_file_info_file_does_not_crash_run(tmp_path):
    """PLAN.md section 9 L8 / spec 20-sync-ledger.md section 2 step 3: a raised parse
    error maps to a failed run, never an unhandled crash; plan section 6/§14 and
    10-slack-io.md ("one odd payload never fails the run") require robustness to odd
    payloads. parse.py's `_is_present` explicitly excludes `file_access == check_file_info`
    stubs from live media, but the `file_sigs` builder (parse.py) filters only on
    `mimetype` + `is_tombstoned` and then indexes `f["name"]`/`f["size"]` directly.

    A real Slack `check_file_info` file stub carries an image mimetype but no `name`/`size`,
    so `parse` raises KeyError. run_sync's step 3 has no handling, so one such message
    aborts the entire sync with a traceback instead of a mapped SyncResult.
    """
    roster = roster_of({"U0A": "red", "U0B": "blue"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    # A post-shape human message whose one file is a check_file_info stub: image mimetype,
    # no name/size (exactly the shape _is_present already knows to skip).
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL, text="<@U0B>",
               files=[{"id": "F01", "mimetype": "image/png", "file_access": "check_file_info"}])

    result = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert isinstance(result, SyncResult)  # currently raises KeyError instead


# ---------------------------------------------------------------------------
# 3. Failure injection: a reaction error never aborts the run (E-W4-21).
# ---------------------------------------------------------------------------

def test_untolerated_reaction_error_maps_to_exit_1(tmp_path, capsys):
    """E-W4-21 (supersedes the old "untolerated -> 1" step-7 contract): a SlackAPIError on
    one reaction (here `internal_error`) is logged `WARN reaction_failed` and skipped; it
    neither crashes run_sync nor fails the run, persist still runs, and the exit code is 0.
    """
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    slack.faults.reaction_error(method="reactions_add", error="internal_error", times=1)

    result = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert result.exit_code == 0 and result.ledger_written
    assert "reaction_failed" in capsys.readouterr().err
