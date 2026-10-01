"""Red-team wave 2 / round 2 — HOSTILE INPUT AND FAILURE INJECTION against the cross-module
joints of snipebot/sync.py (the state machine's step ordering and fail-closed contract),
checked against spec/20-sync-ledger.md §2 (the step table's "Abort -> exit" column).

Each test is a break: it FAILS on the shipped code and would pass once sync conforms to the
quoted spec sentence. Inputs are built with the FakeSlack authoring API (tests/fake_slack.py)
and the shared sync helpers (tests/_helpers_sync.py); helpers are copied in so this file
imports no other red-team module. No production or test file other than this one is modified.
"""

from __future__ import annotations

import dataclasses

from snipebot.config import DatedRules
from snipebot.faces import FakeFaceDetector
from snipebot.sync import run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import (
    BOT,
    CHANNEL,
    image_file,
    make_config,
    mkts,
    roster_of,
    rule,
    sha,
    us,
)


# --------------------------------------------------------------------------- #
# local harness (copied so this file stands alone)
# --------------------------------------------------------------------------- #

def _users(*ids: str) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in ids:
        out[uid] = FakeUser(id=uid)
    return out


def _paths(tmp_path):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack, config, tmp_path, *, now, detector=None, **kw):
    led, st = _paths(tmp_path)
    return run_sync(
        slack, config, detector=detector or FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now), no_post=True, **kw,
    )


# --------------------------------------------------------------------------- #
# Finding 1 — a raised parse error at step 3 is not mapped to exit 1
# --------------------------------------------------------------------------- #

def test_hostile_payload_parse_error_not_mapped_to_exit_1(tmp_path):
    """20 §2 step table, step 3 "parse", Abort -> exit column:

        "— (parse errors are impossible on real payloads; a raised parse error -> 1)".

    A hostile Slack payload — a post-shape human message whose only file carries an
    `image/*` mimetype but no `id`/`name`/`size` keys — makes `parse` raise `KeyError`
    while building the Candidate (`f["id"]`, snipebot/parse.py). Step 3's parse loop in
    `run_sync` runs the parse under `warnings.catch_warnings` with NO try/except, so the
    KeyError propagates out of `run_sync` as a traceback instead of the mandated exit-1
    fail-closed result. One odd message must never crash a run.
    """
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    # image mimetype, present (not tombstoned), but missing the id/name/size keys parse reads.
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[{"mimetype": "image/png"}])

    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 1, (
        "20 §2 step 3 requires a raised parse error to map to exit 1; "
        f"run_sync returned exit {r.exit_code} (or crashed uncaught)"
    )


# --------------------------------------------------------------------------- #
# Finding 2 — a message earlier than the first rule crashes at step 5 (faces)
# instead of the step-6 NoRuleInForceError -> exit 2
# --------------------------------------------------------------------------- #

def test_no_rule_in_force_crashes_in_faces_instead_of_exit_2(tmp_path):
    """20 §2 step table: step 5 "consent and faces" carries "— (detection faults are
    tolerated, never abort; §5.2.2)" in its Abort column, and step 6 "evaluate" carries
    "2 `NoRuleInForceError`".

    A sib-tagged snipe posted BEFORE the first rule's `effective_from` has no rule in
    force. `_detect_faces` (step 5) gates on `config.rules.in_force_at(parse_ts(row.ts))`
    `.selfie_bonus` with the `in_force_at` call unguarded, so it raises `NoRuleInForceError`
    at step 5 — a step whose contract is that it NEVER aborts — before evaluate (step 6),
    whose Abort column maps that error to exit 2, is ever reached. The run therefore crashes
    with a traceback instead of returning the fail-closed exit-2 result.
    """
    late = dataclasses.replace(rule(selfie_bonus=True), effective_from_us=us(2026, 9, 20))
    rules = DatedRules(entries=(late,))
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, rules=rules, selfie_emoji="selfie")
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])

    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12),
             detector=FakeFaceDetector({sha(b"pic"): 1}))
    assert r.exit_code == 2, (
        "20 §2 maps NoRuleInForceError to exit 2 (step 6) and forbids step 5 from aborting; "
        f"run_sync returned exit {r.exit_code} (or crashed uncaught at step 5)"
    )
