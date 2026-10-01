"""Red-team wave 2 / round 3 — INVARIANTS ACROSS RUNS for snipebot/sync.py steps 5-7
(veto/opt-out/selfie observation, face detection, evaluate, reaction convergence).

Each test targets one break in the shipped code and FAILS on it. Inputs are built with
the FakeSlack authoring API (tests/fake_slack.py) and the shared sync helpers
(tests/_helpers_sync.py). No production or other test file is modified.
"""

from __future__ import annotations

from snipebot.faces import FakeFaceDetector
from snipebot.sync import SyncResult, run_sync
from snipebot.ts import parse_ts
from snipebot.ledger import load_ledger

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


def _paths(tmp_path):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack, config, tmp_path, *, now, detector=None, **kw) -> SyncResult:
    led, st = _paths(tmp_path)
    return run_sync(
        slack, config, detector=detector or FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now), no_post=True, **kw,
    )


# ---------------------------------------------------------------------------
# 1. Consent: a TARGET opting out must not tear the bot's ✅ off the sniper's
#    photo, exactly as a SENDER opt-out must not (both are opt-outs).
# ---------------------------------------------------------------------------

def test_target_optout_removes_existing_counted_reaction(tmp_path):
    """00-data.md section 4 (Reacted): every other NOT_COUNTED reason
    "(`SENDER_OPTED_OUT`, `TARGET_OPTED_OUT`, `VETOED`, ...) stays **silent** — an opt-out
    in particular must not be signalled (plan section 6, section 14)."
    `_converge_reactions`'s own guard states the intent: "An opt-out never removes the bot's
    existing status reaction: tearing several off at once would announce the opt-out to the
    channel."

    Break: the guard is gated on `mv.reason is Reason.SENDER_OPTED_OUT` ONLY. A single-target
    sib snipe that is COUNTED (bot places ✅) flips to message-level NOT_COUNTED / reason
    TARGET_OPTED_OUT once the target opts out (per-target reason 2, promoted to the message
    reason when it is the only pair). The desired set collapses to empty, the guard does not
    fire (reason is TARGET_OPTED_OUT, not SENDER_OPTED_OUT), and the bot removes the ✅ from
    the sniper's photo — signalling the target's opt-out to the whole channel.
    """
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])

    # Run 1: B not opted out -> the snipe is COUNTED -> the bot places ✅.
    cfg1 = make_config(roster=roster)
    r1 = _run(slack, cfg1, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r1.reactions_added >= 1  # precondition: the ✅ is now on the message

    # Run 2: the TARGET B has opted out. The ✅ must stay (opt-out is never signalled).
    cfg2 = make_config(roster=roster, seed_opted_out=("U0B",))
    r2 = _run(slack, cfg2, tmp_path, now=mkts(2026, 9, 18, 12, 30))
    assert r2.reactions_removed == 0, (
        "the bot tore its ✅ off the sniper's photo because a TARGET opted out — "
        "announcing the opt-out, which the SENDER_OPTED_OUT guard forbids for opt-outs"
    )
