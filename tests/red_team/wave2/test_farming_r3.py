"""Wave 2 red-team, round 3 (INVARIANTS ACROSS RUNS): a player farming points or dodging
gates end to end through `snipebot.sync.run_sync` with `FakeSlack` + `FakeFaceDetector`
+ FilesStore. Every test asserts an across-runs invariant the spec/plan states and the
current code violates, so each FAILS on the code under attack.

Invariants under attack: idempotence of a second sync, order-independence, convergence
after any single fault, byte-identical ledgers across schedules, verdict/ledger agreement,
conservation between what was reacted and what was persisted.

Helpers mirror tests/_helpers_sync.py + tests/test_sync*.py (copied per the wave brief).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from snipebot.faces import FakeFaceDetector
from snipebot.sync import SyncResult, run_sync
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
        out[uid] = FakeUser(id=uid, display_name=uid)
    return out


def _paths(tmp_path: Path, sub: str = "data") -> tuple[Path, Path]:
    d = tmp_path / sub
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack, config, led, st, *, now, detector=None, **kw) -> SyncResult:
    return run_sync(
        slack, config, detector=detector or FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now), **kw,
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ledger_text(led: Path) -> str:
    return led.read_text(encoding="utf-8") if led.exists() else ""


ADM1 = "U0ADMN1"
ADM2 = "U0ADMN2"


def _selfie_world(now):
    """A sib-tagged photo (U0A -> U0B, same group) the detector reads as a plain SNIPE
    (T=1 target, 1 face). Two admins react the confirm emoji at DIFFERENT times."""
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADM1: None, ADM2: None})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie",
                      admins=(ADM1, ADM2))
    pic = b"one-face-photo"
    slack = FakeSlack(now=now, bot_user_id=BOT, users=_users("U0A", "U0B", ADM1, ADM2))
    slack.post(at=mkts(2026, 9, 18, 10, 0), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", pic)])
    # ADM2 confirms early, ADM1 (alphabetically first) confirms later.
    slack.react(at=mkts(2026, 9, 18, 10, 5), ts=mkts(2026, 9, 18, 10, 0),
                channel=CHANNEL, user=ADM2, name="selfie")
    slack.react(at=mkts(2026, 9, 18, 10, 20), ts=mkts(2026, 9, 18, 10, 0),
                channel=CHANNEL, user=ADM1, name="selfie")
    det = FakeFaceDetector({_sha(pic): 1})  # 1 face == T -> detector says SNIPE
    return cfg, slack, det


def _override_line(led: Path) -> str:
    for line in _ledger_text(led).splitlines():
        if '"selfie_override": {' in line or '"selfie_override":{' in line:
            return line
    return ""


# =====================================================================
# 1. BYTE-IDENTICAL LEDGERS ACROSS SCHEDULES: selfie_override (key 20)
# =====================================================================

def test_selfie_override_by_is_schedule_dependent(tmp_path):
    """spec/20-sync-ledger.md section 2.2: "`selfie_override` (key 20) is durable but
    **deterministic** - first-seen and byte-identical across schedules, never a
    schedule-differ." 00-data.md section 3 lists key 20 outside the faces carve-out
    (keys 17-19), so two sync schedules over one Slack timeline MUST write a
    byte-identical `selfie_override`.

    The admin 🤳 override records `by = sorted(admins currently reacting)[0]`
    (`_observe_admin_selfie`, sync.py). Which admins are in that set at the FIRST sync
    that observes the emoji depends on the sync schedule: a fine schedule that fires
    while only the later-alphabetical admin has reacted freezes `by` to that admin,
    while a coarse schedule that fires once after both have reacted picks the
    alphabetically-first admin. The persisted key-20 bytes therefore differ across
    schedules, which the spec forbids.
    """
    # Schedule A: sync at 10:10 (only ADM2 has reacted) then again at 10:30.
    cfgA, slackA, detA = _selfie_world(mkts(2026, 9, 18, 10, 10))
    ledA, stA = _paths(tmp_path, "A")
    slackA.as_of(mkts(2026, 9, 18, 10, 10))
    _run(slackA, cfgA, ledA, stA, now=mkts(2026, 9, 18, 10, 10), detector=detA)
    slackA.as_of(mkts(2026, 9, 18, 10, 30))
    _run(slackA, cfgA, ledA, stA, now=mkts(2026, 9, 18, 10, 30), detector=detA)

    # Schedule B: a single sync at 10:30 (both admins have reacted by then).
    cfgB, slackB, detB = _selfie_world(mkts(2026, 9, 18, 10, 30))
    ledB, stB = _paths(tmp_path, "B")
    slackB.as_of(mkts(2026, 9, 18, 10, 30))
    _run(slackB, cfgB, ledB, stB, now=mkts(2026, 9, 18, 10, 30), detector=detB)

    lineA = _override_line(ledA)
    lineB = _override_line(ledB)
    assert lineA and lineB              # both recorded an override
    assert lineA == lineB               # spec: byte-identical across schedules


# =====================================================================
# 2. HARD REPOST GATE evaded by delete + age-out (double points, farming)
# =====================================================================

def _verdicts(led: Path) -> dict:
    import json
    p = led.with_name("verdicts.jsonl")
    out = {}
    for line in (p.read_text(encoding="utf-8").splitlines() if p.exists() else []):
        if line:
            o = json.loads(line)
            out[o["ts"]] = o
    return out


