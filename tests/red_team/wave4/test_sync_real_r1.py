"""Wave 4, round 1: `run_sync` fed with the REAL captured fixture messages (tests/fixtures,
scrubbed G2 captures) through a raw-message Slack double, files-mode persistence under
`tmp_path`. Offline and deterministic; no network, no git.

The double serves the captured dicts verbatim (bot_id/bot_profile on user-token posts, the
real file_not_found stub, zero-fraction edited.ts) and applies the bot's reaction writes to
them, so later runs see the bot's own earlier reactions exactly as Slack would show them.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger
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


def _load(*parts: str) -> dict:
    return json.loads(FIXTURES.joinpath(*parts).read_text(encoding="utf-8"))


class RawSlack:
    """A SlackIO double that serves raw captured message dicts and records calls."""

    def __init__(self, messages: list[dict]) -> None:
        self.msgs: dict[str, dict] = {m["ts"]: copy.deepcopy(m) for m in messages}
        self.calls: list[tuple] = []

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
        if ts not in self.msgs:
            raise MessageNotFound("message_not_found")
        return copy.deepcopy(self.msgs[ts])

    def reactions_add(self, channel, ts, name):
        self.calls.append(("add", ts, name))
        m = self.msgs.get(ts)
        if m is None:
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
        if m is None:
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


# --- findings -----------------------------------------------------------------------------

def test_admin_selfie_reaction_with_skin_tone_is_not_seen(tmp_path):
    """Claim: an admin's selfie confirmation made with a skin-tone modifier is ignored.

    Slack reports a skin-toned reaction under its own name, `<emoji>::skin-tone-<n>`
    (reactions[].name), and the selfie emoji (the owner's `selfie`, U+1F933) is a skin-tone
    capable emoji: an admin who has set a default skin tone in Slack reacts with
    `selfie::skin-tone-3` every time. `_reaction_full_users` matches the configured name
    exactly, so the admin's reaction on a sib-tagged real photo never writes the REACTION
    `SelfieOverride`, and the selfie point the admin confirmed is never awarded.
    Violates 20 section 5.2.1 (an admin's selfie reaction on a sib-tagged row writes
    `SelfieOverride(value=True, by=<admin>, source=REACTION)`) and 00-data section 4 (the
    override decides the selfie class)."""
    msg = _load("history", "photo-and-tag.json")      # real phone-style upload, tags TARGET
    msg["reactions"] = [{"name": "selfie::skin-tone-3", "users": [ADMIN], "count": 1}]
    slack = RawSlack([msg])
    cfg = _config(
        groups={SNIPER: "fam", TARGET: "fam", ADMIN: None, THIRD: None},
        selfie_bonus=True, selfie_emoji="selfie", max_attempts=0,
    )

    result = _run(slack, cfg, tmp_path)
    assert result.exit_code == 0

    override = _row(tmp_path, msg["ts"]).selfie_override
    assert override is not None and override.value is True and override.by == ADMIN, (
        "the admin's skin-toned selfie reaction was not read as the selfie emoji"
    )


def test_text_blocks_disagree_audit_lists_a_message_that_is_never_a_row(tmp_path, capsys):
    """Claim: the `text_blocks_disagree` L8 audit category is filled from every parsed
    message, not from stored rows, so a text-only post that is never stored is audited.

    Since E-G2-1 a message posted through an app's user token (the real `tag-only` capture:
    `user` + `bot_id` + `bot_profile`) passes the human test, and app-authored posts carry
    `section` blocks (mrkdwn, no `user` elements) beside a `text` that holds `<@U...>`, so
    parse emits a mention-disagreement ParseAnomaly for them. Step 3 appends every such
    anomaly's ts to the audit list even though the message is text-only and is dropped by
    the keep filter. Violates 40 section 4.2 backfill (`text_blocks_disagree` = "rows whose
    `text` and `blocks` mention sets differed at parse time") and 20 section 2 step 3 ("stores
    the row normally, and adds it to the L8 audit list")."""
    msg = _load("history", "tag-only.json")            # real user-token text post (bot_id + user)
    assert msg.get("bot_id") and msg.get("user") == SNIPER and not msg.get("files")
    msg["blocks"] = [{
        "type": "section",
        "block_id": "sec1",
        "text": {"type": "mrkdwn", "text": "a reminder for the channel"},
    }]
    slack = RawSlack([msg])

    result = _run(slack, _config(), tmp_path, dry_run=True)
    assert result.exit_code == 0
    assert result.rows_scanned == 0                   # the text-only post is never a row

    err = capsys.readouterr().err
    assert f"AUDIT text_blocks_disagree ts={msg['ts']}" not in err, (
        "a message that is not a stored row is on the text_blocks_disagree audit list"
    )
