"""Red-team wave 4, surface rig-offline, round 1.

Every test drives the L6 rig driver (``tests/rig/rig_scenario.run``) or its config
template fully offline: the Slack clients are in-process fakes shaped like the real
Web API responses recorded at gate G2, the ``snipebot`` CLI is a recording stub, the
wall clock is a fake that advances only on ``time.sleep`` and the stubbed commands,
and the upload POST is stubbed. Nothing here reaches the network.
"""

from __future__ import annotations

import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from tests.rig import rig_scenario as R

ROOT = Path(__file__).resolve().parents[3]

HUMAN = "U0AAA001"        # U_HUMAN: the user-token holder, sniper and sole admin
TARGET = "U0AAA002"       # U_TARGET: the sib who never acts
THIRD = "U0AAA010"        # a third party whose message predates the rig run
BOT = "U0BOT01"           # the bot token's own user id
C_MAIN = "C0MAIN01"
C_OFF = "C0OFF001"
NY = ZoneInfo("America/New_York")

# 2026-09-25 14:00 America/New_York: an ordinary afternoon run, before the 21:00 anchor.
START_US = int(datetime(2026, 9, 25, 14, 0, tzinfo=NY).timestamp()) * 1_000_000
SYNC_SECONDS = 5          # a fast real sync pass (fetch + converge)


def _ts_of(us: int) -> str:
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


# --------------------------------------------------------------------------- #
# The fake world: one clock, the channel contents, and what each token did.
# --------------------------------------------------------------------------- #

@dataclass
class World:
    now_us: int = START_US
    share_delay_us: int = 0               # >0: the share lands asynchronously (G2 fact 3)
    messages: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {C_MAIN: [], C_OFF: []})
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    uploads: list[dict[str, Any]] = field(default_factory=list)
    edits: list[dict[str, Any]] = field(default_factory=list)
    deletes: list[tuple[str, str]] = field(default_factory=list)
    cli_calls: list[dict[str, Any]] = field(default_factory=list)
    manifest_bot_scopes: frozenset[str] = frozenset()
    _last_us: int = 0

    # -- clock -----------------------------------------------------------------
    def time(self) -> float:
        return self.now_us / 1_000_000

    def sleep(self, seconds: float) -> None:
        self.now_us += int(round(seconds * 1_000_000))

    def next_ts_us(self) -> int:
        us = max(self.now_us, self._last_us + 1)
        self._last_us = us
        return us

    # -- visibility --------------------------------------------------------------
    def visible(self, channel: str) -> list[dict[str, Any]]:
        out = [m for m in self.messages.get(channel, []) if m["visible_at"] <= self.now_us]
        out.sort(key=lambda m: m["ts_us"], reverse=True)    # newest first, like Slack
        return [
            {k: v for k, v in m.items() if k not in ("visible_at", "ts_us")} for m in out
        ]


class FakeUserWeb:
    """The user-token WebClient: uploads (v2 two-step), edits, deletes, reactions."""

    def __init__(self, world: World) -> None:
        self.w = world

    def auth_test(self, **_: Any) -> dict[str, Any]:
        return {"ok": True, "user_id": HUMAN, "team_id": "T0TEAM"}

    def files_getUploadURLExternal(self, *, filename: str, length: int, **_: Any):
        fid = f"F0FILE{len(self.w.files) + 1:03d}"
        self.w.files[fid] = {"id": fid, "channel": None, "ts": None}
        return {"ok": True, "upload_url": f"https://fixture.invalid/upload/{fid}",
                "file_id": fid}

    def files_completeUploadExternal(self, *, files, channel_id: str,
                                     initial_comment: str = "", **_: Any):
        fid = files[0]["id"]
        us = self.w.next_ts_us()
        ts = _ts_of(us)
        visible_at = us + self.w.share_delay_us
        self.w.files[fid].update(channel=channel_id, ts=ts, visible_at=visible_at)
        self.w.messages[channel_id].append({
            "type": "message", "ts": ts, "ts_us": us, "user": HUMAN,
            "text": initial_comment, "files": [{"id": fid}], "visible_at": visible_at,
        })
        self.w.uploads.append({"file_id": fid, "ts": ts, "ts_us": us,
                               "text": initial_comment, "channel": channel_id})
        # Real shape: the completed file carries its id and title only -- NO share ts.
        return {"ok": True, "files": [{"id": fid, "title": files[0].get("title", "")}]}

    def files_info(self, *, file: str, **_: Any):
        return _files_info(self.w, file)

    def chat_update(self, *, channel: str, ts: str, text: str = "", **_: Any):
        # Slack's edited.ts has a zero fraction (G2 fact 21).
        self.w.edits.append({"channel": channel, "ts": ts, "text": text,
                             "edit_ts": f"{self.w.now_us // 1_000_000}.000000"})
        return {"ok": True, "channel": channel, "ts": ts}

    def chat_delete(self, *, channel: str, ts: str, **_: Any):
        self.w.deletes.append((channel, ts))
        msgs = self.w.messages.get(channel, [])
        for m in msgs:
            if m["ts"] == ts:
                msgs.remove(m)
                return {"ok": True}
        raise RuntimeError("message_not_found")

    def reactions_add(self, *, channel: str, timestamp: str, name: str, **_: Any):
        return {"ok": True}

    def chat_postMessage(self, *, channel: str, text: str = "", **_: Any):
        us = self.w.next_ts_us()
        ts = _ts_of(us)
        self.w.messages[channel].append({
            "type": "message", "ts": ts, "ts_us": us, "user": HUMAN, "text": text,
            "bot_id": "B0BOT", "app_id": "A0APP", "visible_at": us,
        })
        return {"ok": True, "channel": channel, "ts": ts}


def _files_info(world: World, fid: str) -> dict[str, Any]:
    f = world.files.get(fid)
    if f is None:
        raise RuntimeError("file_not_found")
    shares: dict[str, Any] = {}
    if f.get("ts") and f["visible_at"] <= world.now_us:
        shares = {"public": {f["channel"]: [{"ts": f["ts"], "channel_name": "rig-main"}]}}
    return {"ok": True, "file": {"id": fid, "shares": shares}}


class FakeBotWeb:
    """The raw bot-token WebClient (name resolution, files.info with files:read)."""

    def __init__(self, world: World) -> None:
        self.w = world

    def conversations_list(self, *, types: str = "public_channel", **_: Any):
        wanted = {t.strip() for t in types.split(",") if t.strip()}
        if "private_channel" in wanted and "groups:read" not in self.w.manifest_bot_scopes:
            from slack_sdk.errors import SlackApiError

            raise SlackApiError("missing_scope", {"ok": False, "error": "missing_scope",
                                                  "needed": "groups:read"})
        return {"ok": True, "channels": [
            {"id": C_MAIN, "name": "rig-main", "is_private": False},
            {"id": C_OFF, "name": "rig-officers", "is_private": False},
        ], "response_metadata": {"next_cursor": ""}}

    def files_info(self, *, file: str, **_: Any):
        return _files_info(self.w, file)

    def chat_delete(self, *, channel: str, ts: str, **_: Any):
        self.w.deletes.append((channel, ts))
        return {"ok": True}


class FakeBotIO:
    """The typed bot-token surface (``snipebot.slack_io.SlackIO``)."""

    def __init__(self, world: World) -> None:
        self.w = world

    def auth_identity(self):
        from snipebot.slack_io import AuthIdentity

        return AuthIdentity(user_id=BOT, bot_id="B0BOT", team_id="T0TEAM",
                            url="https://fixture.invalid/")

    def history(self, channel: str, oldest: str, latest: str | None = None):
        return self.w.visible(channel)

    def reactions_get(self, channel: str, ts: str):
        return {"type": "message", "ts": ts, "reactions": []}

    def users_list(self):
        return [{"id": TARGET, "name": "user-2", "profile": {"display_name": "user-2"}}]

    def files_info(self, file: str):
        return _files_info(self.w, file)["file"]


def _manifest_bot_scopes() -> frozenset[str]:
    manifest = yaml.safe_load((ROOT / "slack-app-manifest.yaml").read_text(encoding="utf-8"))
    return frozenset(manifest["oauth_config"]["scopes"]["bot"])


def _install(monkeypatch, world: World, *, cli_rc=None) -> None:
    """Wire the fakes in: clients, clock, upload POST and the snipebot CLI."""
    import slack_sdk
    import snipebot.cli
    import snipebot.slack_io

    bot_web = FakeBotWeb(world)
    user_web = FakeUserWeb(world)
    bot_io = FakeBotIO(world)

    def _web_client(*a: Any, token: str = "", **k: Any):
        return bot_web if token == "fixture-bot-token" else user_web

    monkeypatch.setattr(slack_sdk, "WebClient", _web_client)
    monkeypatch.setattr(snipebot.slack_io, "make_client", lambda token, **k: bot_io)
    monkeypatch.setattr(time, "time", world.time)
    monkeypatch.setattr(time, "sleep", world.sleep)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *a):
            return b""

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())

    def _main(argv=None, **_: Any) -> int:
        argv = list(argv or [])
        cmd = argv[0] if argv else ""
        config_path = argv[argv.index("--config") + 1] if "--config" in argv else None
        world.cli_calls.append({
            "cmd": cmd, "at_us": world.now_us, "config": config_path,
            "now_us": snipebot.cli._now_us(),
            "uploads_before": len(world.uploads),
        })
        if cmd in ("sync", "doctor"):
            world.now_us += SYNC_SECONDS * 1_000_000
        if cli_rc is not None:
            return cli_rc(cmd)
        return 0

    monkeypatch.setattr(snipebot.cli, "main", _main)


def _env() -> dict[str, str]:
    return {
        R.ENV_BOT_TOKEN: "fixture-bot-token",
        R.ENV_USER_TOKEN: "fixture-user-token",
        R.ENV_CHANNEL: "rig-main",
        R.ENV_OFFICERS: "rig-officers",
        R.ENV_TARGET: TARGET,
    }


def _world(**kw: Any) -> World:
    world = World(**kw)
    world.manifest_bot_scopes = kw.get("manifest_bot_scopes", frozenset(
        {"channels:read", "groups:read", "channels:history", "files:read",
         "users:read", "reactions:read", "reactions:write", "chat:write"}))
    # A third party's message already in the channel before the rig starts.
    us = START_US - 600_000_000
    world.messages[C_MAIN].append({
        "type": "message", "ts": _ts_of(us), "ts_us": us, "user": THIRD,
        "text": "hello", "visible_at": us,
    })
    world._last_us = START_US - 1
    return world


def _run(monkeypatch, tmp_path: Path, world: World, **kw: Any):
    _install(monkeypatch, world, **kw)
    return R.run(_env(), workdir=tmp_path / "rig")


def _tagged(world: World, user: str) -> list[dict[str, Any]]:
    return [u for u in world.uploads if f"<@{user}>" in u["text"]]


# --------------------------------------------------------------------------- #
# 1. The share ts of an upload.
# --------------------------------------------------------------------------- #

def test_upload_share_ts_comes_from_files_info_not_the_newest_history_message(
        monkeypatch, tmp_path):
    """The rig must record the ts of the message its own upload created.

    Real-API fact (G2 feed item 3): ``files.completeUploadExternal`` / files_upload_v2
    returns the file object with NO share ts; the share appears asynchronously and must
    be read from ``files.info`` -> ``shares.public[channel][0].ts`` (polled). The rig
    instead falls back to the NEWEST message in ``conversations.history`` read at once,
    before the share has landed, so R1's ts is a third party's older message: every
    R1/R2/R5 assertion reads the wrong row, and teardown ``chat.delete``s a message the
    rig never created (README: "deletes the messages it posted"; rig_scenario
    ``_teardown``: "never touch anything it did not create").
    """
    world = _world(share_delay_us=2_000_000)
    foreign = world.messages[C_MAIN][0]["ts"]
    results = _run(monkeypatch, tmp_path, world)

    r1_real = world.uploads[0]["ts"]
    assert results.ts_by_step["R1"] == r1_real
    assert all(ts != foreign for _, ts in world.deletes), (
        "teardown deleted a message the rig did not post")


# --------------------------------------------------------------------------- #
# 2. Post spacing vs the 60 s rig cooldown.
# --------------------------------------------------------------------------- #

def test_counted_steps_are_spaced_past_the_rig_cooldown(monkeypatch, tmp_path):
    """R9 and R12 must be posted >= 60 s after the previous U_HUMAN->U_TARGET snipe.

    Spec 50 section 7.2: R9 "post one more counted snipe" (so R7's digest is revised to
    revision:1) and R12 "... COUNTED ... >= 60 s after R2's cooldown". The rig config
    sets ``cooldown.minutes: 1`` per pair, and R7/R9/R11/R12 all tag U_TARGET from
    U_HUMAN. The only wait the rig has is one ``sleep(30)`` placed AFTER R2 is posted;
    R9 goes up one R7+R8 sync later and R12 one R11 sync later (seconds), so both land
    inside the pair cooldown and score COOLDOWN: the digest never revises and the
    selfie round trip never counts. (R2 must still be < 60 s after R1.)
    """
    world = _world()
    _run(monkeypatch, tmp_path, world)

    tagged = _tagged(world, TARGET)
    by_text = {u["text"]: u for u in tagged}
    r1 = tagged[0]
    r2 = tagged[1]
    assert r2["ts_us"] - r1["ts_us"] < 60_000_000            # R2 is inside R1's cooldown

    def gap_before(upload: dict[str, Any]) -> int:
        earlier = [u for u in tagged if u["ts_us"] < upload["ts_us"]]
        return upload["ts_us"] - max(u["ts_us"] for u in earlier)

    r9 = next(u for t, u in by_text.items() if t.endswith(" extra"))
    r12 = next(u for t, u in by_text.items() if t.endswith(" selfie"))
    assert gap_before(r9) >= 60_000_000, "R9 posted inside the pair cooldown"
    assert gap_before(r12) >= 60_000_000, "R12 posted inside the pair cooldown"


# --------------------------------------------------------------------------- #
# 3. R6: the tag edit must land after the edit grace window.
# --------------------------------------------------------------------------- #

def test_r6_tag_edit_lands_outside_the_configured_edit_grace(monkeypatch, tmp_path):
    """R6 must edit R4 AFTER the grace window, or R4 never becomes LATE_TAG.

    Spec 50 section 7.2 R6: "edit R4 to add a tag after the grace window -> R4 LATE_TAG".
    rules._late_tag: late iff edited.ts > ts + edit_grace. The rig template sets
    ``edit_grace_minutes: 10`` but the rig edits R4 two short syncs after posting it
    (seconds later), so the edit is inside grace and R4 is re-scored as an ordinary
    tagged snipe instead of LATE_TAG. Slack's edited.ts has a zero fraction (G2 fact 21),
    so the comparison uses whole seconds.
    """
    from snipebot.config import load_config
    from snipebot.ts import parse_ts

    world = _world()
    _run(monkeypatch, tmp_path, world)

    r4 = next(u for u in world.uploads if "<@" not in u["text"])
    edit = next(e for e in world.edits if e["ts"] == r4["ts"])
    config_path = next(c["config"] for c in world.cli_calls if c["config"])
    grace_us = load_config(Path(config_path)).rules.entries[0].edit_grace_us
    assert parse_ts(edit["edit_ts"]) > parse_ts(r4["ts"]) + grace_us, (
        "R6 edit falls inside the edit grace window: R4 cannot score LATE_TAG")


# --------------------------------------------------------------------------- #
# 4. The digest due time vs the run time.
# --------------------------------------------------------------------------- #

def test_digest_due_state_matches_r1_and_r7_regardless_of_run_time(monkeypatch, tmp_path):
    """R1's sync must see no digest due; R7's sync must see the daily digest due for
    the day R7's snipes were posted.

    Spec 50 section 7.2: R1 "digest not yet due"; R7 "post several photos to fill a due
    period; advance to the report anchor ... exactly one digest in C_MAIN for the day
    period key"; R9 revises THAT digest. sync posts ``periods.most_recent_due`` within
    24 h of its anchor. The template has a fixed semester start (2026-09-01) and a
    21:00 daily anchor, and the rig never advances the sync clock (``snipebot.cli``'s
    ``_now_us``) nor renders the anchor relative to the run: at a 14:00 run, R1's sync
    already posts YESTERDAY's daily digest, and R7's sync sees yesterday's period, so
    R7/R9's snipes can never move the digest the rig asserts on.
    """
    from snipebot.config import load_config
    from snipebot.periods import most_recent_due

    world = _world()
    _run(monkeypatch, tmp_path, world)

    syncs = [c for c in world.cli_calls if c["cmd"] == "sync"]
    r1_sync = next(c for c in syncs if c["uploads_before"] >= 1)
    fill_count = max(i for i, u in enumerate(world.uploads) if "fill" in u["text"]) + 1
    r7_sync = next(c for c in syncs if c["uploads_before"] >= fill_count)

    def due(call, name):
        cfg = load_config(Path(call["config"]))
        report = next(r for r in cfg.reports if r.name == name)
        d = most_recent_due(report, call["now_us"], cfg.tz, cfg.semesters)
        if d is None or call["now_us"] - d.anchor_us >= 86_400_000_000:
            return None
        return d.period_key

    cfg = load_config(Path(r1_sync["config"]))
    assert all(due(r1_sync, r.name) is None for r in cfg.reports), (
        "a digest is already due at R1's sync")
    fill_ts_us = next(u for u in world.uploads if "fill" in u["text"])["ts_us"]
    fill_day = datetime.fromtimestamp(fill_ts_us // 1_000_000, tz=timezone.utc
                                      ).astimezone(cfg.tz).date().isoformat()
    assert due(r7_sync, "daily") == f"daily:{fill_day}", (
        "R7's sync does not see the daily period holding R7's snipes as due")


# --------------------------------------------------------------------------- #
# Real run_sync against FakeSlack under the rendered rig config.
# --------------------------------------------------------------------------- #

def _sync_one_upload(tmp_path: Path, text: str) -> tuple[str, Path]:
    """Render the rig template from the ids the rig resolves, post one photo from
    U_HUMAN with ``text``, run the real ``run_sync`` once; return (ts, verdicts path)."""
    import hashlib

    from snipebot.config import load_config
    from snipebot.faces import FakeFaceDetector
    from snipebot.sync import run_sync
    from snipebot.ts import parse_ts
    from tests.fake_slack import FakeSlack, FakeUser

    ids = {"C_MAIN": C_MAIN, "C_OFF": C_OFF, "U_HUMAN": HUMAN, "U_TARGET": TARGET,
           "BOT": BOT, "U_BOT": BOT}
    cfg = load_config(R.write_config(ids, tmp_path / "config.yaml"))

    now = "1790340000.000001"
    posted = "1790339000.000001"
    users = {BOT: FakeUser(id=BOT, is_bot=True), HUMAN: FakeUser(id=HUMAN),
             TARGET: FakeUser(id=TARGET)}
    slack = FakeSlack(now=now, bot_user_id=BOT, users=users,
                      channels=(C_MAIN, C_OFF), bot_member_of=(C_MAIN, C_OFF))
    data = b"rig-photo-1"
    photo = {"id": "F0FILE001", "mimetype": "image/png", "name": "photo-1.png",
             "size": len(data), "original_w": 100, "original_h": 100,
             "thumb_1024": "https://fixture.invalid/files/F0FILE001",
             "url_private_download": "https://fixture.invalid/files/F0FILE001/dl",
             "_bytes": data}
    ts = slack.post(at=posted, user=HUMAN, channel=C_MAIN, text=text, files=[photo])

    d = tmp_path / "data"
    d.mkdir()
    one_face = {hashlib.sha256(data).hexdigest(): 1}     # a plain snipe of a sib
    run_sync(slack, cfg, detector=FakeFaceDetector(one_face),
             ledger_path=d / "ledger.jsonl", state_path=d / "state.json",
             now_us=parse_ts(now), no_post=True)
    return ts, d / "verdicts.jsonl"


def test_ledger_read_back_matches_the_assertion_vocabulary(tmp_path):
    """A COUNTED snipe in the real verdicts.jsonl must pass ``assert_status(..., "COUNTED")``.

    Spec 50 section 7.2 states every ledger expectation in the enum vocabulary
    ("status COUNTED", "COOLDOWN", "selfie == SELFIE"), and the rig's own live test calls
    ``assert_status(rows, ts, "COUNTED")`` / ``"COOLDOWN"``. But verdicts.jsonl stores the
    wire values (ledger.dumps_verdict_row: ``status.value`` = "counted", selfie "snipe")
    and ``_load_verdict_rows`` copies them verbatim, so every ledger assertion in the
    live rig fails against a correct ledger (R1, R2, R3, R12, R13).
    """
    from tests.rig import assertions as A

    ts, verdicts = _sync_one_upload(tmp_path, f"<@{TARGET}>")
    rows = R._load_verdict_rows(verdicts)
    A.assert_status(rows, ts, "COUNTED")


def test_r3_bot_target_counts_under_the_rendered_rig_config(tmp_path):
    """R3 (a photo tagging U_BOT) must score COUNTED under the rendered rig config.

    Spec 50 section 7.2 R3: "row COUNTED (rig allow_bots:true)". rules.evaluate gates
    ``roster.is_member_at(target)`` BEFORE ``allow_bots``; the roster is built only from
    ``players`` in the config, and the template lists U_HUMAN and U_TARGET only (there
    is no U_BOT placeholder), so the bot target is TARGET_OFF_ROSTER and allow_bots is
    never reached. Driven through the real ``run_sync`` against FakeSlack with the
    template rendered from the ids the rig resolves.
    """
    import json

    from snipebot.rules import Status

    ts, verdicts = _sync_one_upload(tmp_path, f"<@{BOT}>")
    rows = [json.loads(line) for line in
            verdicts.read_text(encoding="utf-8").splitlines() if line.strip()]
    row = next(r for r in rows if r["ts"] == ts)
    assert row["status"] == Status.COUNTED.value, (
        f"R3 scored {row['status']} / {row.get('reason')}")
# --------------------------------------------------------------------------- #
# 6. Channel-name resolution vs the manifest's scopes.
# --------------------------------------------------------------------------- #

def test_channel_name_resolution_needs_no_scope_beyond_the_manifest(monkeypatch, tmp_path):
    """Resolving SNIPEBOT_RIG_CHANNEL/OFFICERS names must work with the manifest's bot
    scopes.

    Real-API fact (G2 feed item 6): listing ``private_channel`` needs ``groups:read``,
    which ``slack-app-manifest.yaml`` does not grant (channels:* only; the README tells
    the owner to create PUBLIC channels). The rig calls ``conversations.list`` with
    ``types="public_channel,private_channel"`` on the bot token, so Slack answers
    ``missing_scope`` and the rig dies resolving the channel names before R0.
    """
    world = _world(manifest_bot_scopes=_manifest_bot_scopes())
    assert "groups:read" not in world.manifest_bot_scopes
    results = _run(monkeypatch, tmp_path, world)
    assert results.ids["C_MAIN"] == C_MAIN
    assert results.ids["C_OFF"] == C_OFF


# --------------------------------------------------------------------------- #
# 7. The R12/R13 group-points read-back is never populated.
# --------------------------------------------------------------------------- #

def test_group_points_read_back_is_recorded_for_r12_and_r13(monkeypatch, tmp_path):
    """The rig must record the sibling group's points after R12 and after R13.

    Spec 50 section 7.3: the selfie helpers "read the sibling group's points from the
    tables rebuilt via eligible_snipes, asserting 2 after R12 and 1 after R13".
    ``RigResults.group_points`` (R-id -> points) is the field the live test consumes,
    but ``run`` never writes it, so ``test_rig_selfie`` skips both points checks
    silently: the fetch -> count -> award -> override round trip is never checked.
    """
    world = _world()
    results = _run(monkeypatch, tmp_path, world)
    assert "R12" in results.group_points
    assert "R13" in results.group_points
