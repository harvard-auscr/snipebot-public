"""Wave 4, round 2: `run_sync` fed with the REAL captured fixture messages (tests/fixtures,
scrubbed G2 captures) through a raw-message Slack double, files-mode persistence under
`tmp_path`. Offline and deterministic; no network, no git.

The double serves the captured dicts verbatim and applies the bot's reaction writes to
them, so a later run sees the bot's own earlier reactions exactly as Slack shows them.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger
from snipebot.parse import VetoSource
from snipebot.slack_io import (
    AlreadyReacted,
    AuthIdentity,
    MessageNotFound,
    NoReaction,
    SlackHTTPError,
)
from snipebot.sync import SyncResult, run_sync
from snipebot.ts import parse_ts

from tests._helpers_sync import make_config, roster_of

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
ADMIN = "U0AAA009"
THIRD = "U0AAA010"
NOW = "1790210000.000000"          # a few hours after the captured September 2026 messages
LATER = "1790210600.000000"        # the next scheduled run, ten minutes on


def _load(*parts: str) -> dict:
    return json.loads(FIXTURES.joinpath(*parts).read_text(encoding="utf-8"))


class RawSlack:
    """A SlackIO double that serves raw captured message dicts and records calls."""

    def __init__(self, messages: list[dict]) -> None:
        self.msgs: dict[str, dict] = {m["ts"]: copy.deepcopy(m) for m in messages}
        self.calls: list[tuple] = []
        self.gone_for_reactions_get: set[str] = set()

    def auth_identity(self) -> AuthIdentity:
        return AuthIdentity(user_id=BOT, bot_id="B0BOT", team_id="T0TEAM",
                            url="https://fixture.invalid/")

    def history(self, channel, oldest, latest=None):
        self.calls.append(("history", channel))
        if channel != CHANNEL:
            return []
        lo = parse_ts(oldest)
        hi = parse_ts(latest) if latest is not None else None
        out = [copy.deepcopy(m) for ts, m in self.msgs.items()
               if parse_ts(ts) >= lo and (hi is None or parse_ts(ts) <= hi)]
        out.sort(key=lambda m: parse_ts(m["ts"]), reverse=True)
        return out

    def reactions_get(self, channel, ts):
        self.calls.append(("reactions_get", ts))
        if ts not in self.msgs or ts in self.gone_for_reactions_get:
            raise MessageNotFound("message_not_found")
        return copy.deepcopy(self.msgs[ts])

    def reactions_add(self, channel, ts, name):
        self.calls.append(("add", ts, name))
        m = self.msgs.get(ts)
        if m is None or ts in self.gone_for_reactions_get:
            raise MessageNotFound("message_not_found")
        for r in m.setdefault("reactions", []):
            if r["name"] == name:
                if BOT in r["users"]:
                    raise AlreadyReacted("already_reacted")
                r["users"].append(BOT)
                r["count"] += 1
                return
        m["reactions"].append({"name": name, "users": [BOT], "count": 1})

    def reactions_remove(self, channel, ts, name):
        self.calls.append(("remove", ts, name))
        m = self.msgs.get(ts)
        if m is None or ts in self.gone_for_reactions_get:
            raise MessageNotFound("message_not_found")
        for r in m.get("reactions", []):
            if r["name"] == name and BOT in r["users"]:
                r["users"].remove(BOT)
                r["count"] -= 1
                if r["count"] == 0:
                    m["reactions"].remove(r)
                return
        raise NoReaction("no_reaction")

    def post_message(self, channel, *, text, blocks, metadata):
        self.calls.append(("post", channel))
        return "1790300000.000100"

    def update_message(self, channel, ts, *, text, blocks, metadata):
        self.calls.append(("update", channel, ts))
        return ts

    def users_list(self):
        return []

    def channel_info(self, channel):
        return {"id": channel, "is_member": True, "is_private": False}

    def conversations_members(self, channel):
        return []

    def fetch_file_bytes(self, url):
        raise SlackHTTPError("not served by this double")


def _config(groups: dict[str, str | None] | None = None, **kw):
    if groups is None:
        groups = {u: None for u in (SNIPER, TARGET, "U0AAA003", ADMIN, THIRD)}
    cfg = make_config(roster=roster_of(groups), admins=(ADMIN,), **kw)
    # The owner mapping: veto emoji `x`, so the not_counted feedback emoji must differ.
    return replace(
        cfg,
        consent=replace(cfg.consent, veto_emoji="x"),
        feedback=replace(cfg.feedback, not_counted="heavy_multiplication_x"),
    )


def _run(slack: RawSlack, cfg, tmp_path: Path, now: str = NOW, **kw) -> SyncResult:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return run_sync(
        slack, cfg, detector=FakeFaceDetector({}), ledger_path=d / "ledger.jsonl",
        state_path=d / "state.json", now_us=parse_ts(now), no_post=True, **kw,
    )


def _row(tmp_path: Path, ts: str):
    return next(r for r in load_ledger(tmp_path / "data" / "ledger.jsonl") if r.ts == ts)


def _verdict(tmp_path: Path, ts: str) -> dict:
    lines = (tmp_path / "data" / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
    return next(v for v in (json.loads(x) for x in lines) if v["ts"] == ts)


# --- findings -----------------------------------------------------------------------------

def test_veto_read_message_not_found_erases_the_reaction_veto(tmp_path):
    """Claim: when the veto reaction's payload user list is truncated and the settling
    `reactions_get` answers `message_not_found` (the message vanished between the history
    fetch and the read), `_observe_vetoes` treats the answer as "no veto emoji" and rebuilds
    the row's `vetoes` from its CLI half only. The admin's durable REACTION veto is erased
    and persisted, and the vetoed photo is COUNTED again in verdicts.jsonl.

    Violates 10 section 3 caller tolerance ("Opt-out / veto read: `reactions_get` ...
    raising `MessageNotFound` ... is not an error and changes nothing") and 20 section 5.1
    (a message is vetoed iff it carries a recorded Veto). `_observe_admin_selfie` and
    `_observe_optouts` already treat the same answer as "change nothing"."""
    msg = _load("refetch", "veto-by-target", "after.json")   # real upload carrying `x`
    # The admin vetoed, and one more person reacted; the history payload shows a truncated
    # list (count 2, one user), so step 5 settles it with one reactions_get (20 section 5.1).
    msg["reactions"] = [{"name": "x", "users": [ADMIN], "count": 2}]
    slack = RawSlack([msg])
    full = copy.deepcopy(msg)
    full["reactions"] = [{"name": "x", "users": [ADMIN, THIRD], "count": 2}]
    orig_get = slack.reactions_get

    def reactions_get(channel, ts):
        if ts in slack.gone_for_reactions_get:
            return orig_get(channel, ts)
        slack.calls.append(("reactions_get", ts))
        return copy.deepcopy(full)

    slack.reactions_get = reactions_get
    cfg = _config()

    first = _run(slack, cfg, tmp_path)
    assert first.exit_code == 0
    vetoes = _row(tmp_path, msg["ts"]).vetoes
    assert any(v.by == ADMIN and v.source == VetoSource.REACTION for v in vetoes)
    assert _verdict(tmp_path, msg["ts"])["reason"] == "vetoed"

    # Next run: history still returns the message, but the settle read races its delete.
    slack.gone_for_reactions_get.add(msg["ts"])
    second = _run(slack, cfg, tmp_path, now=LATER)
    assert second.exit_code == 0
    assert ("reactions_get", msg["ts"]) in slack.calls

    vetoes = _row(tmp_path, msg["ts"]).vetoes
    assert any(v.by == ADMIN and v.source == VetoSource.REACTION for v in vetoes), (
        "a message_not_found on the veto read erased the admin's REACTION veto"
    )
    assert _verdict(tmp_path, msg["ts"])["reason"] == "vetoed", (
        "the vetoed photo counts again after a tolerated message_not_found"
    )
