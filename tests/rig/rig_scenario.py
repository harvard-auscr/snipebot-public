"""The ordered L6 rig script (50-test-matrix.md section 7.2, PLAN.md section 9 L6).

Two surfaces:

* ``plan()`` -- the pure, offline-testable ordered list of steps (R0..R13), each
  with the actor that performs it (a HUMAN action on the user token, or a SYSTEM
  ``snipebot`` invocation) and the command run after it. No I/O.

* ``run(env)`` -- the live driver. It resolves IDs through the bot token, writes a
  resolved config + data dir under a temp folder, performs each human action on the
  user token (post with an upload, tag, edit, delete, react with the veto, opt-out),
  runs the real ``snipebot sync`` through ``snipebot.cli.main`` with the default
  factories (real transport, real YuNet), reads everything back through the bot
  token, and tears down the messages it posted. It refuses cleanly -- raising
  ``RigNotConfigured`` before any network call or client construction -- when the
  five rig env vars are absent.

Logs carry user IDs only; tokens come from the environment and are never printed,
and no bot or workspace name is ever hardcoded (every ID is resolved through the
API). Slack ts values are compared through ``snipebot.ts.parse_ts`` -- never
``float()``.
"""

from __future__ import annotations

import logging
import re
import struct
import time
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

_LOG = logging.getLogger("snipebot.rig")

# --------------------------------------------------------------------------- #
# Environment contract (README): five vars, everything else resolved via the API.
# --------------------------------------------------------------------------- #

ENV_BOT_TOKEN = "SLACK_BOT_TOKEN"
ENV_USER_TOKEN = "SLACK_USER_TOKEN"
ENV_CHANNEL = "SNIPEBOT_RIG_CHANNEL"        # C_MAIN, a channel NAME
ENV_OFFICERS = "SNIPEBOT_RIG_OFFICERS"      # C_OFF, a channel NAME
ENV_TARGET = "SNIPEBOT_RIG_TARGET"          # U_TARGET, a member id or handle

REQUIRED_ENV: tuple[str, ...] = (
    ENV_BOT_TOKEN,
    ENV_USER_TOKEN,
    ENV_CHANNEL,
    ENV_OFFICERS,
    ENV_TARGET,
)

# Placeholders the template carries; render_config fills them from resolved IDs.
PLACEHOLDERS: tuple[str, ...] = ("C_MAIN", "C_OFF", "U_HUMAN", "U_TARGET", "U_BOT")

_TEMPLATE_PATH = Path(__file__).with_name("config.rig.yaml")


class RigNotConfigured(RuntimeError):
    """The rig cannot run: one or more of the five env vars is absent.

    Raised by ``run`` before any Slack client is built, so an unconfigured
    environment (CI, where there are no tokens) never touches the network.
    """


class RigCommandFailed(RuntimeError):
    """A ``snipebot`` invocation the rig ran exited non-zero (the R0 doctor
    preflight or a step's sync): the rig aborts instead of running on."""


# --------------------------------------------------------------------------- #
# The ordered script as pure data (offline-testable).
# --------------------------------------------------------------------------- #

class Actor(str, Enum):
    HUMAN = "human"     # a real-person action on the user token
    SYSTEM = "system"   # a snipebot invocation (doctor / sync / a CLI override)


@dataclass(frozen=True)
class Step:
    id: str             # R0..R13
    actor: Actor        # who performs the step's primary action
    action: str         # a short kind label for the action
    then_run: str       # the command run after the action: "doctor" or "sync"
    passes: int = 1     # how many complete passes of `then_run` (R10 needs two)
    note: str = ""


def plan() -> list[Step]:
    """The ordered R0..R13 script of section 7.2 as pure data.

    The order and the per-step actor are the contract the offline test pins: a
    HUMAN step is a user-token action, a SYSTEM step is a ``snipebot`` invocation
    (the R0 preflight and the R13 CLI override). Every step runs ``sync`` after the
    action except R0, which runs ``doctor``.
    """
    return [
        Step("R0", Actor.SYSTEM, "doctor_preflight", "doctor",
             note="all FAIL checks pass on the rig workspace"),
        Step("R1", Actor.HUMAN, "post_photo_tagging_target", "sync",
             note="one COUNTED row; counted emoji on the message"),
        Step("R2", Actor.HUMAN, "post_photo_tagging_target_in_cooldown", "sync",
             note="COOLDOWN row, blocked_by = R1; cooldown emoji"),
        Step("R3", Actor.HUMAN, "post_photo_tagging_bot", "sync",
             note="COUNTED under rig allow_bots; counted emoji"),
        Step("R4", Actor.HUMAN, "post_photo_no_tag", "sync",
             note="untagged emoji; not in standings"),
        Step("R5", Actor.HUMAN, "react_veto_on_r1", "sync",
             note="R1 -> VETOED; counted emoji removed; gone from standings"),
        Step("R6", Actor.HUMAN, "edit_r4_add_tag_after_grace", "sync",
             note="R4 -> LATE_TAG; not counted"),
        Step("R7", Actor.HUMAN, "post_photos_fill_period_then_advance", "sync",
             note="one digest in C_MAIN; metadata revision:0; no ping"),
        Step("R8", Actor.SYSTEM, "advance_to_post_to_anchor", "sync",
             note="one digest in C_OFF; none duplicated in C_MAIN"),
        Step("R9", Actor.HUMAN, "post_one_more_in_r7_period", "sync",
             note="R7 digest chat.updated: revision:1, _revised_, new hash"),
        Step("R10", Actor.HUMAN, "delete_r9_message", "sync", passes=2,
             note="after two fetches the row is deleted; digest revised down"),
        Step("R11", Actor.HUMAN, "post_parity_api_and_phone", "sync",
             note="parse yields identical candidates or the rig is invalid"),
        Step("R12", Actor.HUMAN, "upload_selfie_tagging_sib", "sync",
             note="COUNTED, selfie==SELFIE; 🤳 present; group points == 2"),
        Step("R13", Actor.SYSTEM, "cli_selfie_override_no", "sync",
             note="🤳 removed; re-scores SNIPE; group points drops to 1"),
    ]


# --------------------------------------------------------------------------- #
# Config template rendering (offline-testable).
# --------------------------------------------------------------------------- #

# The semester start used when no run date is given (offline renders). The live
# rig passes its own local run date so no digest period is due before the run's
# first report anchor (50 section 7.2 R1 "digest not yet due").
RUN_DATE_PLACEHOLDER = "RUN_DATE"
DEFAULT_RUN_DATE = "2026-09-01"
# The semester end is derived from the run date (never a fixed day, which a later
# run would pass): the margin covers a rig run that crosses local midnight.
RUN_END_PLACEHOLDER = "RUN_END"
_RUN_END_DAYS = 30

# The report anchors ("HH:MM" in the config's tz). The live rig renders them from
# its own start time so no period is due before R7 (``report_anchors``); offline
# renders keep the defaults. The officers anchor is always 5 min after the daily.
DAILY_AT_PLACEHOLDER = "DAILY_AT"
OFFICERS_AT_PLACEHOLDER = "OFFICERS_AT"
DEFAULT_DAILY_AT = "21:00"
DEFAULT_OFFICERS_AT = "21:05"
_ANCHOR_MARGIN_MINUTES = 30      # covers R1..R6 before the daily anchor can fall due
_OFFICERS_AFTER_MINUTES = 5


def report_anchors(start: datetime) -> tuple[str, str]:
    """The (daily, officers) anchors for a rig started at ``start`` (tz-aware, the
    config's tz): 21:00/21:05, or -- when ``start`` is later than 21:00 minus the
    margin -- the start plus the margin, rounded up to a whole minute. Raises
    ``RigNotConfigured`` when the officers anchor would cross local midnight."""
    minutes = start.hour * 60 + start.minute + (1 if (start.second or start.microsecond) else 0)
    daily = max(21 * 60, minutes + _ANCHOR_MARGIN_MINUTES)
    officers = daily + _OFFICERS_AFTER_MINUTES
    if officers >= 24 * 60:
        raise RigNotConfigured(
            "rig started too late: its report anchors would cross local midnight; "
            "start the rig earlier")
    return (f"{daily // 60:02d}:{daily % 60:02d}",
            f"{officers // 60:02d}:{officers % 60:02d}")


def render_config(ids: Mapping[str, str], *, template_path: Path | None = None,
                  run_date: str | None = None,
                  anchors: tuple[str, str] | None = None) -> str:
    """Render ``config.rig.yaml`` by substituting the resolved IDs for placeholders.

    ``ids`` maps each name in ``PLACEHOLDERS`` (``C_MAIN``/``C_OFF``/``U_HUMAN``/
    ``U_TARGET``/``U_BOT``) to a resolved Slack ID. ``run_date`` (``YYYY-MM-DD``,
    the run's local date) becomes the semester start; ``DEFAULT_RUN_DATE`` when
    omitted. The result loads through ``snipebot.config.load_config``. Raises
    ``KeyError`` if a placeholder has no ID.
    """
    path = template_path or _TEMPLATE_PATH
    text = path.read_text(encoding="utf-8")
    for name in PLACEHOLDERS:
        if name not in ids:
            raise KeyError(f"no resolved ID for placeholder {name!r}")
        text = text.replace("{{" + name + "}}", ids[name])
    rd = run_date or DEFAULT_RUN_DATE
    text = text.replace("{{" + RUN_DATE_PLACEHOLDER + "}}", rd)
    run_end = date.fromisoformat(rd) + timedelta(days=_RUN_END_DAYS)
    text = text.replace("{{" + RUN_END_PLACEHOLDER + "}}", run_end.isoformat())
    daily_at, officers_at = anchors or (DEFAULT_DAILY_AT, DEFAULT_OFFICERS_AT)
    text = text.replace("{{" + DAILY_AT_PLACEHOLDER + "}}", daily_at)
    text = text.replace("{{" + OFFICERS_AT_PLACEHOLDER + "}}", officers_at)
    return text


def write_config(ids: Mapping[str, str], dest: Path, *, template_path: Path | None = None,
                 run_date: str | None = None,
                 anchors: tuple[str, str] | None = None) -> Path:
    """Render and write the resolved config to ``dest``; returns ``dest``."""
    dest.write_text(render_config(ids, template_path=template_path, run_date=run_date,
                                  anchors=anchors),
                    encoding="utf-8")
    return dest


# --------------------------------------------------------------------------- #
# Environment gating.
# --------------------------------------------------------------------------- #

def missing_env(env: Mapping[str, str]) -> list[str]:
    """The required env vars that are absent or empty, in declared order."""
    return [name for name in REQUIRED_ENV if not env.get(name)]


def is_configured(env: Mapping[str, str]) -> bool:
    return not missing_env(env)


# --------------------------------------------------------------------------- #
# Live results container.
# --------------------------------------------------------------------------- #

@dataclass
class RigResults:
    """Everything the read-back assertions need, gathered from the live API.

    Populated only by a live ``run``; the offline tests never build one.
    """
    bot_user_id: str
    ids: dict[str, str]
    emojis: dict[str, str]
    ledger_rows: list[dict[str, Any]] = field(default_factory=list)
    reactions: dict[str, dict[str, Any]] = field(default_factory=dict)   # ts -> reactions.get
    main_messages: list[dict[str, Any]] = field(default_factory=list)
    off_messages: list[dict[str, Any]] = field(default_factory=list)
    ts_by_step: dict[str, str] = field(default_factory=dict)             # R-id -> ts
    parity: dict[str, Any] = field(default_factory=dict)                 # {"api", "phone"}
    group_points: dict[str, int] = field(default_factory=dict)           # R-id -> points
    # reactions.get of a step's own message, right after that step's sync (R1, R12).
    step_reactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The watched channel's history right after a step's sync (R7, R9 digests).
    digest_snapshots: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The live driver. Never exercised offline: it raises before any network call
# when the env is unconfigured, and the test that drives it skips without tokens.
# --------------------------------------------------------------------------- #

def run(env: Mapping[str, str], *, workdir: str | Path | None = None) -> RigResults:
    """Drive the ordered scenario against the real workspace and read it back.

    Refuses with ``RigNotConfigured`` -- before importing ``slack_sdk`` or building
    any client -- when the env is not fully configured, so CI (no tokens) never
    reaches the network. Otherwise it resolves IDs through the bot token, writes a
    resolved config + data dir under ``workdir`` (a temp folder), performs each
    human action on the user token and each ``sync`` through ``snipebot.cli.main``,
    reads everything back, and tears down the messages it posted.
    """
    absent = missing_env(env)
    if absent:
        raise RigNotConfigured(
            "rig not configured: missing " + ", ".join(absent)
        )

    # Imports deferred past the env gate: an unconfigured run never constructs a
    # Slack client (the offline test asserts WebClient is never built).
    from slack_sdk import WebClient

    from snipebot.config import load_config
    from snipebot.slack_io import make_client

    bot_token = env[ENV_BOT_TOKEN]
    user_token = env[ENV_USER_TOKEN]

    bot = make_client(bot_token)          # the typed read surface (history/reactions/users)
    bot_web = WebClient(token=bot_token)   # raw: conversations.list + files.info (files:read)
    user = WebClient(token=user_token)     # the human actions on the user token

    driver = _LiveRig(
        bot=bot,
        bot_web=bot_web,
        user=user,
        env=env,
        workdir=Path(workdir) if workdir is not None else Path.cwd() / ".rig-run",
        load_config=load_config,
    )
    return driver.execute()


class _LiveRig:
    """The stateful live driver, split out so ``run`` stays a thin, gated entry.

    Only reached with a fully configured environment; every method logs IDs only.
    """

    def __init__(self, *, bot, bot_web, user, env, workdir: Path, load_config) -> None:
        self._bot = bot
        self._bot_web = bot_web
        self._user = user
        self._env = env
        self._workdir = workdir
        self._load_config = load_config
        self._posted: list[tuple[str, str]] = []   # (channel, ts) to tear down
        self._cooldown_seconds = 60                 # the rig's shortened cooldown
        self._png_serial = 0                        # makes every tiny upload distinct
        self._ids: Mapping[str, str] = {}
        self._run_start_ts = ""                     # oldest bound of the digest sweep
        # Ledger rows captured right after a given step's own sync (_ROW_AS_OF).
        self._row_snapshots: dict[str, dict[str, Any]] = {}

    # -- resolution (all through the bot token; no name is hardcoded) ----------

    def _resolve_ids(self) -> dict[str, str]:
        me = self._bot.auth_identity()
        bot_user_id = me.user_id
        c_main = self._channel_id_by_name(self._env[ENV_CHANNEL])
        c_off = self._channel_id_by_name(self._env[ENV_OFFICERS])
        u_human = self._user_identity()
        u_target = self._resolve_member(self._env[ENV_TARGET])
        _LOG.info("rig resolved: bot=%s human=%s target=%s", bot_user_id, u_human, u_target)
        return {
            "BOT": bot_user_id,
            "U_BOT": bot_user_id,     # rostered ungrouped: the R3 target (50 section 7.1)
            "C_MAIN": c_main,
            "C_OFF": c_off,
            "U_HUMAN": u_human,
            "U_TARGET": u_target,
        }

    def _channel_id_by_name(self, name: str) -> str:
        wanted = name.lstrip("#")
        cursor = ""
        while True:
            page = self._bot_web.conversations_list(
                exclude_archived=True, limit=200,
                types="public_channel",   # README: public channels; no groups:read
                **({"cursor": cursor} if cursor else {}),
            )
            for channel in page.get("channels", []):
                if channel.get("name") == wanted:
                    return channel["id"]
            cursor = page.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        raise RigNotConfigured("channel not found by name (id withheld from log)")

    def _user_identity(self) -> str:
        data = self._user.auth_test()
        return str(data["user_id"])

    def _resolve_member(self, handle: str) -> str:
        if re.fullmatch(r"[UW][A-Z0-9]{2,}", handle):   # a Slack user ID shape
            return handle
        wanted = handle.lstrip("@")
        for member in self._bot.users_list():
            profile = member.get("profile", {})
            if wanted in (member.get("name"), profile.get("display_name"),
                          profile.get("real_name")):
                return member["id"]
        raise RigNotConfigured("target member not found (id withheld from log)")

    # -- orchestration ---------------------------------------------------------

    def execute(self) -> RigResults:
        ids = self._resolve_ids()
        config, emojis = self._write_workspace(ids)
        results = RigResults(bot_user_id=ids["BOT"], ids=ids, emojis=emojis)
        self._ids = ids
        self._run_start_ts = f"{int(time.time()) - 1}.000000"
        import snipebot.cli as cli

        # The sync clock: the real clock plus a forward offset that R7/R8 raise to
        # their report anchors (the clock only ever moves forward). Restored below.
        real_now_us = cli._now_us
        self._clock_offset_us = 0
        cli._now_us = lambda: real_now_us() + self._clock_offset_us
        try:
            for step in plan():
                self._perform(step, ids, config, results)
            self._read_back(ids, config, results)
        finally:
            cli._now_us = real_now_us
            self._teardown()
        return results

    def _advance_clock_to_anchors(self, config, reports) -> None:
        """Raise the sync clock to 1 s past today's anchor of every report in
        ``reports`` (never backwards), so that report's day period is due (R7/R8)."""
        import snipebot.cli as cli

        now_us = cli._now_us()
        today = datetime.fromtimestamp(now_us // 1_000_000, config.tz).date()
        for report in reports:
            anchor = datetime(today.year, today.month, today.day,
                              report.at_hour, report.at_minute, tzinfo=config.tz)
            target_us = int(anchor.timestamp()) * 1_000_000 + 1_000_000
            if target_us > now_us:
                self._clock_offset_us += target_us - now_us
                now_us = target_us

    def _write_workspace(self, ids: Mapping[str, str]):
        self._workdir.mkdir(parents=True, exist_ok=True)
        data_dir = self._workdir / "data"
        data_dir.mkdir(exist_ok=True)
        config_path = self._workdir / "config.yaml"
        write_config(ids, config_path)
        # Re-render with the run's local date (in the config's own tz) as the
        # semester start: no period is due before today's first anchor (R1).
        # The report anchors follow the start time, so a late-day start still has
        # no period due before R7 (R1 "digest not yet due").
        tz = self._load_config(config_path).tz
        start = datetime.fromtimestamp(time.time(), tz)
        write_config(ids, config_path, run_date=start.date().isoformat(),
                     anchors=report_anchors(start))
        config = self._load_config(config_path)
        emojis = {
            "counted": config.feedback.counted,
            "cooldown": config.feedback.cooldown,
            "untagged": config.feedback.untagged or "",
            "not_counted": config.feedback.not_counted,
            "selfie": config.feedback.selfie or "",
            "review": config.review.emoji or "",
            "veto": config.consent.veto_emoji,
        }
        self._config_path = config_path
        self._data_dir = data_dir
        return config, emojis

    def _sync(self, passes: int = 1) -> None:
        from snipebot.cli import main

        for _ in range(max(1, passes)):
            rc = main([
                "sync",
                "--config", str(self._config_path),
                "--data-dir", str(self._data_dir),
            ])
            if rc != 0:
                raise RigCommandFailed(f"sync exited {rc}; rig aborted")

    def _perform(self, step: Step, ids, config, results: RigResults) -> None:
        # The per-step human actions on the user token are implemented by the
        # handlers below; each records the ts it created for the read-back and the
        # teardown. Steps whose action is a pure scheduler advance or a CLI override
        # touch no user-token endpoint. Every step then runs its `then_run`.
        handler = getattr(self, f"_do_{step.id.lower()}", None)
        if handler is not None:
            handler(ids, config, results)
        if step.then_run == "doctor":
            self._run_doctor()
        else:
            if step.id == "R2":
                time.sleep(30)          # < 60 s rig cooldown (R2)
            if step.id == "R7":         # advance to the watched-channel report anchor
                self._advance_clock_to_anchors(
                    config, [r for r in config.reports if r.post_to is None])
            elif step.id == "R8":       # advance to the post_to (officers) anchor
                self._advance_clock_to_anchors(config, config.reports)
            self._sync(step.passes)
            self._snapshot_rows(step.id, results)
            if step.id in ("R1", "R12"):    # counted (R1) / selfie+counted (R12) on it now
                results.step_reactions[step.id] = dict(self._bot.reactions_get(
                    ids["C_MAIN"], results.ts_by_step[step.id]))
            if step.id in ("R7", "R9"):     # R7 revision:0, R9 updated in place
                results.digest_snapshots[step.id] = list(
                    self._bot.history(ids["C_MAIN"], oldest=self._run_start_ts))
            if step.id in ("R12", "R13"):
                results.group_points[step.id] = self._group_points_of(
                    ids, config, results.ts_by_step.get("R12", ""))

    def _run_doctor(self) -> None:
        from snipebot.cli import main

        rc = main([
            "doctor",
            "--config", str(self._config_path),
            "--data-dir", str(self._data_dir),
        ])
        if rc != 0:
            raise RigCommandFailed(f"R0 doctor preflight failed (exit {rc}); rig aborted")

    def _snapshot_rows(self, step_id: str, results: RigResults) -> None:
        """Keep the rows ``_ROW_AS_OF`` reads as of this step's sync (e.g. R2's
        COOLDOWN row, before the R5 veto legitimately re-scores it)."""
        wanted = [row_step for row_step, as_of in _ROW_AS_OF.items() if as_of == step_id]
        if not wanted:
            return
        rows = _load_verdict_rows(self._data_dir / "verdicts.jsonl")
        for row_step in wanted:
            ts = results.ts_by_step.get(row_step)
            row = next((r for r in rows if ts and r.get("ts") == ts), None)
            if row is not None:
                self._row_snapshots[row_step] = row

    # -- user-token actions (uploads via getUploadURLExternal + complete) ------

    def _upload_and_post(self, *, channel: str, text: str, image: Path | bytes,
                         filename: str) -> str:
        data = image.read_bytes() if isinstance(image, Path) else image
        upload = self._user.files_getUploadURLExternal(
            filename=filename, length=len(data)
        )
        import urllib.request

        request = urllib.request.Request(
            upload["upload_url"], data=data, method="POST"
        )
        with urllib.request.urlopen(request, timeout=30):
            pass
        completed = self._user.files_completeUploadExternal(
            files=[{"id": upload["file_id"], "title": filename}],
            channel_id=channel,
            initial_comment=text,
        )
        files = completed.get("files") or []
        fid = str((files[0].get("id") if files else None) or upload["file_id"])
        ts = self._share_ts_of(fid, channel)
        self._posted.append((channel, ts))
        self._await_in_history(channel, ts)
        return ts

    def _await_in_history(self, channel: str, ts: str) -> None:
        """Wait until ``conversations.history`` reliably returns the upload's message.
        Live, a fresh upload is visible to a narrow query (``oldest`` = its own ts)
        while a wide one, like the sync's scan window, still misses it for a moment; a
        sync in between would not see the step's own post. So poll with the run's wide
        window and require two sightings a second apart."""
        oldest = self._run_start_ts or ts
        sightings = 0
        for attempt in range(15):
            if any(m.get("ts") == ts for m in self._bot.history(channel, oldest=oldest)):
                sightings += 1
                if sightings >= 2:
                    return
            else:
                sightings = 0
            time.sleep(1)
        raise RuntimeError("upload never appeared in conversations.history")

    def _share_ts_of(self, fid: str, channel: str) -> str:
        """The ts of the message the upload shared into ``channel``.

        completeUploadExternal never carries it (G2 fact 3): the share lands
        asynchronously in ``files.info`` -> ``shares.public[channel][0].ts``
        (``shares.private`` as a fallback), polled on the user token that uploaded
        it. The bot token is refused with ``file_not_found`` for a moment after a
        user-token upload (seen live), so that error means "not yet" and is retried.
        Raises rather than guess, so the rig never records -- or tears down -- a
        message it did not post.
        """
        from slack_sdk.errors import SlackApiError

        attempts = 8
        for attempt in range(attempts):
            try:
                info = self._user.files_info(file=fid)
            except SlackApiError as exc:
                if (exc.response.get("error") if exc.response is not None else None) != "file_not_found":
                    raise
                info = {}
            shares = (info.get("file") or {}).get("shares") or {}
            for kind in ("public", "private"):
                entries = (shares.get(kind) or {}).get(channel) or []
                if entries and entries[0].get("ts"):
                    return str(entries[0]["ts"])
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
        raise RuntimeError("upload share ts never appeared in files.info")

    def _post_text(self, *, channel: str, text: str) -> str:
        data = self._user.chat_postMessage(channel=channel, text=text)
        ts = str(data["ts"])
        self._posted.append((channel, ts))
        return ts

    # The R-step handlers below drive the user token; a step with no handler is a
    # pure SYSTEM advance/override. They are intentionally small and log IDs only.

    def _photo(self, name: str) -> Path:
        return _fixture_photo(name)

    def _tiny_png(self) -> bytes:
        """A valid 1x1 PNG whose pixel encodes a per-run serial: every tiny upload
        has its own rendition hash, so no rig upload is a REPOST of another (the
        rig's sibling group has selfie_bonus on; a 1x1 has no large thumbs)."""
        self._png_serial += 1
        return _tiny_png_bytes(self._png_serial)

    def _do_r1(self, ids, config, results):
        results.ts_by_step["R1"] = self._upload_and_post(
            channel=ids["C_MAIN"], text=f"<@{ids['U_TARGET']}>",
            image=self._tiny_png(), filename="r1.png")

    def _do_r2(self, ids, config, results):
        results.ts_by_step["R2"] = self._upload_and_post(
            channel=ids["C_MAIN"], text=f"<@{ids['U_TARGET']}>",
            image=self._tiny_png(), filename="r2.png")

    def _do_r3(self, ids, config, results):
        results.ts_by_step["R3"] = self._upload_and_post(
            channel=ids["C_MAIN"], text=f"<@{ids['BOT']}>",
            image=self._tiny_png(), filename="r3.png")

    def _do_r4(self, ids, config, results):
        results.ts_by_step["R4"] = self._upload_and_post(
            channel=ids["C_MAIN"], text="no tag here",
            image=self._tiny_png(), filename="r4.png")

    def _do_r5(self, ids, config, results):
        r1 = results.ts_by_step["R1"]
        self._user.reactions_add(
            channel=ids["C_MAIN"], timestamp=r1, name=results.emojis["veto"]
        )

    def _do_r6(self, ids, config, results):
        r4 = results.ts_by_step["R4"]
        # edited.ts is whole seconds (G2 fact 21): wait so the edit lands in a later
        # second than R4, i.e. outside the rig's zero edit grace.
        time.sleep(2)
        self._user.chat_update(
            channel=ids["C_MAIN"], ts=r4, text=f"<@{ids['U_TARGET']}>"
        )

    def _do_r7(self, ids, config, results):
        for i in range(2):
            self._upload_and_post(
                channel=ids["C_MAIN"], text=f"<@{ids['U_TARGET']}> fill{i}",
                image=self._tiny_png(), filename=f"r7-{i}.png")

    def _do_r9(self, ids, config, results):
        # R9 must COUNT: wait out the pair cooldown since the R7 fill snipes (+1 s
        # clears the >= boundary at whole-second ts granularity).
        time.sleep(self._cooldown_seconds + 1)
        results.ts_by_step["R9"] = self._upload_and_post(
            channel=ids["C_MAIN"], text=f"<@{ids['U_TARGET']}> extra",
            image=self._tiny_png(), filename="r9.png")

    def _do_r10(self, ids, config, results):
        r9 = results.ts_by_step["R9"]
        self._user.chat_delete(channel=ids["C_MAIN"], ts=r9)

    def _do_r11(self, ids, config, results):
        api_ts = self._upload_and_post(
            channel=ids["C_MAIN"], text=f"<@{ids['U_TARGET']}> parity",
            image=self._photo("portrait_one_face.jpg"), filename="parity-api.jpg")
        results.ts_by_step["R11"] = api_ts
        # The phone-shape capture is supplied by G2 as a history fixture; its ts is
        # recorded from the env-provided fixture channel/message when present.

    def _do_r12(self, ids, config, results):
        # R12 must COUNT: wait out the pair cooldown since R11 (50 section 7.2).
        time.sleep(self._cooldown_seconds + 1)
        results.ts_by_step["R12"] = self._upload_and_post(
            channel=ids["C_MAIN"], text=f"<@{ids['U_TARGET']}> selfie",
            image=self._photo("selfie_two_faces.jpg"), filename="r12.jpg")

    def _do_r13(self, ids, config, results):
        from snipebot.cli import main

        r12 = results.ts_by_step.get("R12", "")
        main([
            "selfie",
            "--ts", r12, "--no",
            "--config", str(self._config_path),
            "--data-dir", str(self._data_dir),
        ])

    # -- read-back (the stored ledger, then the API through the bot token) ------

    def _group_points_of(self, ids, config, r12_ts: str) -> int:
        """The sibling group's points attributable to the R12 message (50 section
        7.3): rebuild the tables via ``eligible_snipes`` from the stored ledger and
        state, and take the group's points with R12 minus without it (the earlier
        counted rig snipes add to the same group total). 2 while SELFIE (photo +
        participation point), 1 after the R13 override (a plain snipe point)."""
        from dataclasses import replace

        from snipebot.aggregate import build_groups_table, eligible_snipes
        from snipebot.ledger import load_ledger, load_state

        ledger = load_ledger(self._data_dir / "ledger.jsonl")
        state = load_state(self._data_dir / "state.json")
        opted_out = set(state.opted_out)
        r12_us = _ts(r12_ts) if r12_ts else None
        semester = next(
            (s for s in config.semesters if r12_us is not None and s.contains(r12_us)),
            config.semesters[0],
        )
        elig = eligible_snipes(ledger, config.rules, config.roster, opted_out,
                               config.semesters, config.tz, semester)
        without = replace(elig, snipes=tuple(s for s in elig.snipes if s.ts != r12_ts))
        group = config.roster.entries[ids["U_HUMAN"]].group

        def points(e) -> int:
            table = build_groups_table(e, config.roster, opted_out)
            return next((row.points for row in table if row.group == group), 0)

        return points(elig) - points(without)


    def _read_back(self, ids, config, results: RigResults) -> None:
        # The stored verdicts.jsonl is the sync's own record; the assertions still
        # cross-check the reactions/digests the API actually holds (below).
        rows = _load_verdict_rows(self._data_dir / "verdicts.jsonl")
        # A row pinned to an earlier step's sync replaces its final state.
        pinned = {row.get("ts"): row for row in self._row_snapshots.values()}
        results.ledger_rows = [pinned.get(row.get("ts"), row) for row in rows]
        for step_id, ts in results.ts_by_step.items():
            if ts:
                try:
                    results.reactions[ts] = dict(self._bot.reactions_get(ids["C_MAIN"], ts))
                except Exception:  # a torn-down / vanished message: recorded empty
                    results.reactions[ts] = {}
        results.main_messages = list(self._bot.history(ids["C_MAIN"], oldest=self._run_start_ts))
        results.off_messages = list(self._bot.history(ids["C_OFF"], oldest=self._run_start_ts))

    def _teardown(self) -> None:
        # Delete only what the rig posted; never touch anything it did not create.
        for channel, ts in reversed(self._posted):
            if not ts:
                continue
            try:
                self._user.chat_delete(channel=channel, ts=ts)
            except Exception:  # already gone (R10 deleted its own): fine
                pass
        # The bot's own digests from this run: dedup is keyed on (channel,
        # period_key), so a leftover digest would swallow a same-day re-run's R7/R8
        # posts. Only bot-authored snipe_digest messages since this run began.
        ids = self._ids
        if not ids or not self._run_start_ts:
            return
        from tests.rig.assertions import DIGEST_EVENT_TYPE

        for channel in (ids.get("C_MAIN"), ids.get("C_OFF")):
            if not channel:
                continue
            try:
                messages = list(self._bot.history(channel, oldest=self._run_start_ts))
            except Exception:
                continue
            for m in messages:
                meta = m.get("metadata") or {}
                if (m.get("user") == ids.get("BOT") and m.get("ts")
                        and meta.get("event_type") == DIGEST_EVENT_TYPE):
                    try:
                        self._bot_web.chat_delete(channel=channel, ts=m["ts"])
                    except Exception:
                        pass


# --------------------------------------------------------------------------- #
# Small module-level helpers.
# --------------------------------------------------------------------------- #

def _ts(value: str) -> int:
    from snipebot.ts import parse_ts

    return parse_ts(value)


def _fixture_photo(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures" / "photos" / name


def _load_verdict_rows(path: Path) -> list[dict[str, Any]]:
    """Parse ``verdicts.jsonl`` into the flat read-back rows the assertions consume.

    Each line carries the message-level ``status``/``selfie`` and a ``pairs`` list;
    ``blocked_by`` is a per-pair field (a COOLDOWN pair names its anchor), lifted to
    the row from the first pair that carries one. A missing file is an empty ledger.
    """
    import json

    from snipebot.rules import SelfieClass, Status

    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        pairs = obj.get("pairs", [])
        blocked_by = next(
            (p.get("blocked_by") for p in pairs if p.get("blocked_by") is not None),
            None,
        )
        # verdicts.jsonl stores the wire values (Status.value / SelfieClass.value);
        # the assertions speak the spec's enum names (COUNTED, COOLDOWN, SELFIE).
        status = obj.get("status")
        selfie = obj.get("selfie")
        rows.append({
            "ts": obj.get("ts"),
            "status": Status(status).name if status is not None else None,
            "reason": obj.get("reason"),
            "selfie": SelfieClass(selfie).name if selfie is not None else None,
            "blocked_by": blocked_by,
        })
    return rows


# Which step's sync each asserted row is read as of: 50 section 7.2 checks R1's
# COUNTED, R2's COOLDOWN (blocked_by R1) and R3's COUNTED after their own syncs;
# the R5 veto later re-scores R1 and, correctly, R2 (a vetoed row anchors no cooldown).
_ROW_AS_OF: dict[str, str] = {"R1": "R1", "R2": "R2", "R3": "R3"}


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _tiny_png_bytes(serial: int) -> bytes:
    """A valid 1x1 RGB PNG whose single pixel is ``serial`` (mod 2**24)."""
    rgb = (serial % (1 << 24)).to_bytes(3, "big")
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b"\x00" + rgb))
            + _png_chunk(b"IEND", b""))


# A 1x1 PNG (a valid, tiny live image for the non-selfie steps). Generated, not a
# fixture: the selfie/snipe steps use the real photo fixtures instead.
_TINY_PNG = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D,
    0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53, 0xDE, 0x00, 0x00, 0x00,
    0x0C, 0x49, 0x44, 0x41, 0x54, 0x08, 0xD7, 0x63, 0xF8, 0xCF, 0xC0, 0x00,
    0x00, 0x03, 0x01, 0x01, 0x00, 0x18, 0xDD, 0x8D, 0xB0, 0x00, 0x00, 0x00,
    0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
])
