"""Red-team wave 4, surface rig-offline, round 3.

The L6 rig driver (``tests/rig/rig_scenario.run``) is driven fully offline: the bot
token's typed surface and the real ``snipebot sync`` run against an in-process
``FakeSlack`` world, the user-token actions (the v2 two-step upload, edit, delete, the
veto reaction) are applied to that same world, the wall clock is a fake that advances
only on ``time.sleep`` and on each action, the upload POST is captured in memory, and
``doctor`` is stubbed. Nothing here reaches the network.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.rig import assertions as A
from tests.rig import rig_scenario as R

HUMAN = "U0AAA001"        # U_HUMAN: the user-token holder, sniper and sole admin
TARGET = "U0AAA002"       # U_TARGET: the sib who never acts
BOT = "U0BOT01"           # the bot token's own user id
C_MAIN = "C0MAIN01"
C_OFF = "C0OFF001"
NY = ZoneInfo("America/New_York")

BOT_TOKEN = "fixture-bot-token"
USER_TOKEN = "fixture-user-token"

IDS = {"C_MAIN": C_MAIN, "C_OFF": C_OFF, "U_HUMAN": HUMAN, "U_TARGET": TARGET,
       "U_BOT": BOT}      # U_BOT: rostered ungrouped (E-W4-20c)


def _us(y, mo, d, h, mi) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=NY).timestamp()) * 1_000_000


AFTERNOON_US = _us(2026, 9, 25, 14, 0)


def _fmt(us: int) -> str:
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


# --------------------------------------------------------------------------- #
# The offline world (the same shape as round 2's harness, kept self-contained).
# --------------------------------------------------------------------------- #

class _Faces(dict):
    """sha256 -> face count; any image not listed has no face."""

    def __missing__(self, key):
        return 0


def _face_counts() -> _Faces:
    out = _Faces()
    for name, n in (("portrait_one_face.jpg", 1), ("selfie_two_faces.jpg", 2)):
        out[hashlib.sha256(R._fixture_photo(name).read_bytes()).hexdigest()] = n
    return out


class Rig:
    def __init__(self, start_us: int, *, sync_seconds: int = 5) -> None:
        from tests.fake_slack import FakeSlack, FakeUser

        self.clock_us = start_us
        self.sync_seconds = sync_seconds
        self.slack = FakeSlack(
            now=_fmt(start_us), bot_user_id=BOT,
            users={BOT: FakeUser(id=BOT, is_bot=True), HUMAN: FakeUser(id=HUMAN),
                   TARGET: FakeUser(id=TARGET)},
            channels=(C_MAIN, C_OFF), bot_member_of=(C_MAIN, C_OFF),
            channel_meta={C_MAIN: {"name": "rig-main"}, C_OFF: {"name": "rig-officers"}},
        )
        self.upload_bytes: dict[str, bytes] = {}
        self.shares: dict[str, tuple[str, str]] = {}
        self.uploads: list[dict[str, Any]] = []
        self.cli_calls: list[list[str]] = []
        self.doctor_rc = 0
        self.cli_rc: dict[str, int] = {}      # command -> forced exit code (not run)
        self._n = 0

    def time(self) -> float:
        return self.clock_us / 1_000_000

    def sleep(self, seconds: float) -> None:
        self.clock_us += int(round(seconds * 1_000_000))

    def at(self) -> str:
        self.slack.as_of(_fmt(self.clock_us))
        return _fmt(self.clock_us)

    def mint(self) -> str:
        self.at()
        ts = self.slack._mint_ts()
        self.clock_us += 1_000_000
        return ts


class _SlackProxy:
    def __init__(self, rig: Rig) -> None:
        self._rig = rig

    def __getattr__(self, name):
        attr = getattr(self._rig.slack, name)
        if callable(attr):
            def call(*a, **k):
                self._rig.at()
                return attr(*a, **k)
            return call
        return attr


class _BotWeb:
    def __init__(self, rig: Rig) -> None:
        self.rig = rig

    def conversations_list(self, **_: Any):
        return {"ok": True, "channels": [
            {"id": C_MAIN, "name": "rig-main", "is_private": False},
            {"id": C_OFF, "name": "rig-officers", "is_private": False},
        ], "response_metadata": {"next_cursor": ""}}

    def files_info(self, *, file: str, **_: Any):
        channel, ts = self.rig.shares[file]
        return {"ok": True, "file": {"id": file, "shares": {"public": {channel: [{"ts": ts}]}}}}

    def chat_delete(self, *, channel: str, ts: str, **_: Any):
        self.rig.slack.delete_message(at=self.rig.mint(), ts=ts, channel=channel)
        return {"ok": True}


class _UserWeb:
    def __init__(self, rig: Rig) -> None:
        self.rig = rig

    def files_info(self, *, file: str, **_: Any):
        # The uploader reads its own share back (the rig polls the user token).
        channel, ts = self.rig.shares[file]
        return {"ok": True, "file": {"id": file, "shares": {"public": {channel: [{"ts": ts}]}}}}

    def auth_test(self, **_: Any):
        return {"ok": True, "user_id": HUMAN, "team_id": "T0TEAM"}

    def files_getUploadURLExternal(self, *, filename: str, length: int, **_: Any):
        self.rig._n += 1
        fid = f"F0FILE{self.rig._n:03d}"
        return {"ok": True, "upload_url": f"https://fixture.invalid/upload/{fid}",
                "file_id": fid}

    def files_completeUploadExternal(self, *, files, channel_id: str,
                                     initial_comment: str = "", **_: Any):
        fid = files[0]["id"]
        data = self.rig.upload_bytes[fid]
        ts = self.rig.mint()
        photo = {"id": fid, "mimetype": "image/jpeg", "filetype": "jpg",
                 "name": f"photo-{self.rig._n}.jpg", "size": len(data),
                 "original_w": 1, "original_h": 1, "mode": "hosted",
                 "url_private_download": f"https://fixture.invalid/files/{fid}/dl",
                 "_bytes": data}
        self.rig.slack.post(at=ts, user=HUMAN, channel=channel_id, text=initial_comment,
                            files=[photo])
        self.rig.shares[fid] = (channel_id, ts)
        self.rig.uploads.append({"file_id": fid, "ts": ts})
        return {"ok": True, "files": [{"id": fid, "title": files[0].get("title", "")}]}

    def chat_update(self, *, channel: str, ts: str, text: str = "", **_: Any):
        self.rig.slack.edit(at=self.rig.mint(), ts=ts, channel=channel, user=HUMAN, text=text)
        return {"ok": True, "channel": channel, "ts": ts}

    def chat_delete(self, *, channel: str, ts: str, **_: Any):
        self.rig.slack.delete_message(at=self.rig.mint(), ts=ts, channel=channel)
        return {"ok": True}

    def reactions_add(self, *, channel: str, timestamp: str, name: str, **_: Any):
        self.rig.slack.react(at=self.rig.mint(), ts=timestamp, channel=channel,
                             user=HUMAN, name=name)
        return {"ok": True}


def _install(monkeypatch, rig: Rig) -> None:
    import slack_sdk
    import snipebot.cli
    import snipebot.slack_io
    from snipebot.faces import FakeFaceDetector

    proxy = _SlackProxy(rig)
    bot_web = _BotWeb(rig)
    user_web = _UserWeb(rig)
    monkeypatch.setattr(slack_sdk, "WebClient",
                        lambda *a, token="", **k: bot_web if token == BOT_TOKEN else user_web)
    monkeypatch.setattr(snipebot.slack_io, "make_client", lambda token, **k: proxy)
    monkeypatch.setattr(time, "time", rig.time)
    monkeypatch.setattr(time, "sleep", rig.sleep)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *a):
            return b""

    def _urlopen(request, *a, **k):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        rig.upload_bytes[url.rsplit("/", 1)[-1]] = bytes(request.data)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    real_main = snipebot.cli.main
    faces = _face_counts()

    def _main(argv=None, **_: Any) -> int:
        argv = list(argv or [])
        rig.cli_calls.append(argv)
        if argv and argv[0] == "doctor":
            rig.clock_us += rig.sync_seconds * 1_000_000
            return rig.doctor_rc
        if argv and argv[0] in rig.cli_rc:
            return rig.cli_rc[argv[0]]
        rig.at()
        rc = real_main(argv, slack_factory=lambda: proxy,
                       detector_factory=lambda: FakeFaceDetector(faces))
        rig.clock_us += rig.sync_seconds * 1_000_000
        return rc

    monkeypatch.setattr(snipebot.cli, "main", _main)


def _env() -> dict[str, str]:
    return {
        R.ENV_BOT_TOKEN: BOT_TOKEN,
        R.ENV_USER_TOKEN: USER_TOKEN,
        R.ENV_CHANNEL: "rig-main",
        R.ENV_OFFICERS: "rig-officers",
        R.ENV_TARGET: TARGET,
    }


def _run(monkeypatch, tmp_path: Path, rig: Rig):
    _install(monkeypatch, rig)
    return R.run(_env(), workdir=tmp_path / "rig")


# --------------------------------------------------------------------------- #
# 1. The template's fixed semester end: the rig cannot run after 2026-12-20.
# --------------------------------------------------------------------------- #

def test_rig_config_loads_for_a_run_after_the_template_semester_end(tmp_path):
    """The rendered rig config must load on any run date, not only until 2026-12-20.

    ``_write_workspace`` renders the semester start from the run's own local date
    (50 section 7.2 R1 "digest not yet due"), but ``config.rig.yaml`` hardcodes
    ``end: 2026-12-20``. From 2026-12-21 on, start > end and ``load_config`` raises
    ``InvalidValueError('semesters[0]: start is after end')`` inside ``execute`` before
    R0, so the L6 rig (the pre-prod gate, 50 section 8 "the rig green
    end-to-end") can never be run again without hand-editing the template.
    """
    from snipebot.config import load_config

    dest = tmp_path / "config.yaml"
    dest.write_text(R.render_config(IDS, run_date="2026-12-21"), encoding="utf-8")
    config = load_config(dest)
    assert config.semesters


# --------------------------------------------------------------------------- #
# 2. Reactions are read back only once, after R13.
# --------------------------------------------------------------------------- #

def test_live_selfie_check_turns_red_when_the_bot_never_awards_the_selfie_emoji(
        monkeypatch, tmp_path):
    """The live rig must catch a bot that never places the selfie emoji on R12.

    50 section 7.2 R12: "the selfie emoji present (with counted)"; 7.3: the selfie
    helpers read the emoji from reactions.get, "asserting it is present after R12 and
    gone after the R13 --no". ``_read_back`` calls reactions.get once, after R13, and the
    rig keeps no per-step reaction snapshot (only ledger rows are pinned), so
    ``test_rig_selfie`` can only check that the emoji is ABSENT. With the award broken
    (the bot never adds the selfie emoji) the whole live selfie check still passes. The
    same holds for R1's "counted emoji is on the message": ``test_rig_reactions`` only
    asserts it is absent after R5, so with reaction convergence skipped it passes too,
    and the CTL-RIG-NOREACT control (7.3: "R1/R5's reaction assertions must turn red")
    cannot turn the live row red.
    """
    import snipebot.sync as sync
    from tests.rig import test_rig as live

    real_desired = sync._desired_reactions

    def _no_selfie_award(mv, row, config):
        return real_desired(mv, row, config) - {config.feedback.selfie}

    monkeypatch.setattr(sync, "_desired_reactions", _no_selfie_award)
    results = _run(monkeypatch, tmp_path, Rig(AFTERNOON_US))

    with pytest.raises(AssertionError):
        live.test_rig_selfie(results)


def test_live_digest_check_turns_red_when_the_r9_revision_never_lands(monkeypatch, tmp_path):
    """The live rig must catch a digest that is never ``chat.update``d at R9.

    50 section 7.2 R7: exactly one digest, metadata ``revision:0``; R9: "R7's digest is
    chat.updated in place: revision:1, _revised_ marker, new numbers_hash". The rig reads
    the channels once, after R13 (``_read_back``), by which time R10..R13 have revised the
    digest several more times, and it keeps no per-step digest snapshot, so
    ``test_rig_digest_posted_in_channel`` can only check that some revision exists. With
    the in-place revision broken (chat.update of a digest does nothing) the digest stays
    at revision 0 with R7's numbers and the live digest check still passes: the R9 revise
    path, the core of digest dedup, is never verified against Slack.
    """
    from tests.rig import test_rig as live

    rig = Rig(AFTERNOON_US)
    rig.slack.update_message = lambda channel, ts, **_: ts      # the break: no revise
    results = _run(monkeypatch, tmp_path, rig)

    with pytest.raises(AssertionError):
        live.test_rig_digest_posted_in_channel(results)


# --------------------------------------------------------------------------- #
# 4. SNIPEBOT_RIG_TARGET given as a handle that starts with U or W.
# --------------------------------------------------------------------------- #

def test_a_target_handle_starting_with_capital_u_resolves_to_the_member_id():
    """SNIPEBOT_RIG_TARGET is "the second member's id or handle" (README); a handle must
    resolve to the member's ID through users.list.

    ``_resolve_member`` returns the value verbatim whenever it starts with "U" or "W", so
    a display-name handle such as "User-2" is taken as a Slack user ID: it is written
    into the roster and every tag as ``<@User-2>`` (or the rendered config is rejected as
    "not a user ID"), and users.list is never consulted.
    """

    class _Bot:
        def users_list(self):
            return [
                {"id": HUMAN, "name": "user-1", "profile": {"display_name": "user-1"}},
                {"id": TARGET, "name": "user-2", "profile": {"display_name": "User-2"}},
            ]

    live = R._LiveRig(bot=_Bot(), bot_web=None, user=None, env={}, workdir=Path("."),
                      load_config=None)
    assert live._resolve_member("User-2") == TARGET


# --------------------------------------------------------------------------- #
# 5. README setup never puts U_TARGET in the watched channel: R0 doctor FAILs.
# --------------------------------------------------------------------------- #

def _setup_steps(readme: str) -> list[str]:
    section = readme.split("## Setting up the workspace", 1)[1].split("\n## ", 1)[0]
    return [s for s in re.split(r"\n(?=\d+\. )", section) if re.match(r"\d+\. ", s.strip())]


def test_readme_setup_adds_the_target_to_the_watched_channel(tmp_path):
    """Following the README's G1 setup, R0 ``doctor`` must pass (50 section 7.2 R0).

    DOC-ROSTER-RESOLVE is a FAIL check: every roster ID must be a member of the watched
    channel (conversations.members). The rig roster is U_HUMAN and U_TARGET. The README
    has U_TARGET accept a WORKSPACE invite (step 2), then U_HUMAN creates the two
    channels (step 3; a new channel's only member is its creator) and invites only the
    bot (step 7). No step adds U_TARGET to the watched channel, so on a workspace set up
    as written R0 fails DOC-ROSTER-RESOLVE and the rig aborts with RigCommandFailed.
    """
    from snipebot import doctor
    from snipebot.config import load_config

    dest = tmp_path / "config.yaml"
    dest.write_text(R.render_config(IDS), encoding="utf-8")
    config = load_config(dest)

    class _Slack:
        def users_list(self):
            return [{"id": HUMAN}, {"id": TARGET}, {"id": BOT, "is_bot": True}]

        def conversations_members(self, channel):
            return [HUMAN, BOT]          # the creator and the /invite'd bot

    # The precondition: a watched channel without U_TARGET fails R0's FAIL check.
    assert not doctor._check_roster_resolve(_Slack(), config).ok

    readme = (Path(R.__file__).with_name("README.md")).read_text(encoding="utf-8")
    steps = _setup_steps(readme)
    assert steps, "README setup steps not found"
    # A sentence that names U_TARGET, a channel, and adds/invites/joins it there.
    sentences = [s for step in steps for s in re.split(r"(?<=[.;])\s+", step)]
    adds_target = [
        s for s in sentences
        if "U_TARGET" in s and "channel" in s.lower()
        and re.search(r"\b(add|adds|added|invite|invites|invited|join|joins)\b", s, re.I)
    ]
    assert adds_target, "no README setup step adds U_TARGET to the watched channel"
