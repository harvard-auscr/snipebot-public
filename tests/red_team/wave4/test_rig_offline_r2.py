"""Red-team wave 4, surface rig-offline, round 2.

The L6 rig driver (``tests/rig/rig_scenario.run``) is driven fully offline end to end:
the bot token's typed surface and the real ``snipebot sync`` run against an in-process
``FakeSlack`` world, the user-token actions (the v2 two-step upload, edit, delete, the
veto reaction) are applied to that same world, the wall clock is a fake that advances
only on ``time.sleep`` and on each action, the upload POST is captured in memory, and
``doctor`` is stubbed. Nothing here reaches the network.
"""

from __future__ import annotations

import hashlib
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


def _us(y, mo, d, h, mi) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=NY).timestamp()) * 1_000_000


# 2026-09-25 14:00 America/New_York: an ordinary afternoon run, before the anchors.
AFTERNOON_US = _us(2026, 9, 25, 14, 0)


def _fmt(us: int) -> str:
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


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
    """One offline world: a FakeSlack, a clock, and the two token fakes."""

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
        self.upload_bytes: dict[str, bytes] = {}      # file id -> uploaded bytes
        self.shares: dict[str, tuple[str, str]] = {}  # file id -> (channel, ts)
        self.uploads: list[dict[str, Any]] = []
        self.cli_calls: list[list[str]] = []
        self.doctor_rc = 0
        self.sync_rc: int | None = None
        self.syncs: list[dict[str, Any]] = []
        self._n = 0

    def bot_posts(self) -> list[tuple[str, str]]:
        """(channel, ts) of every message the bot has posted (the digests)."""
        return [(e["channel"], e["ts"]) for e in self.slack._events
                if e["kind"] == "post" and e["actor"] == BOT]

    # -- clock ------------------------------------------------------------------
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
    """The typed bot-token surface: the FakeSlack, kept in step with the fake clock."""

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
        self.rig.uploads.append({"file_id": fid, "ts": ts, "text": initial_comment,
                                 "sha": hashlib.sha256(data).hexdigest()})
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

    def chat_postMessage(self, *, channel: str, text: str = "", **_: Any):
        ts = self.rig.mint()
        self.rig.slack.post(at=ts, user=HUMAN, channel=channel, text=text)
        return {"ok": True, "channel": channel, "ts": ts}


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
        rig.at()
        rc = real_main(argv, slack_factory=lambda: proxy,
                       detector_factory=lambda: FakeFaceDetector(faces))
        rig.syncs.append({"argv": argv, "uploads_before": len(rig.uploads),
                          "bot_posts": rig.bot_posts()})
        rig.clock_us += rig.sync_seconds * 1_000_000
        return rc if rig.sync_rc is None else rig.sync_rc

    monkeypatch.setattr(snipebot.cli, "main", _main)


def _env() -> dict[str, str]:
    return {
        R.ENV_BOT_TOKEN: BOT_TOKEN,
        R.ENV_USER_TOKEN: USER_TOKEN,
        R.ENV_CHANNEL: "rig-main",
        R.ENV_OFFICERS: "rig-officers",
        R.ENV_TARGET: TARGET,
    }


def _run(monkeypatch, tmp_path: Path, rig: Rig, name: str = "rig"):
    _install(monkeypatch, rig)
    return R.run(_env(), workdir=tmp_path / name)


def _distinct_tiny_images(monkeypatch) -> None:
    """Give every tiny rig upload its own bytes (isolates a finding from the
    identical-bytes REPOST one)."""
    counter = {"n": 0}

    def _tiny(self):
        counter["n"] += 1
        return R._TINY_PNG + counter["n"].to_bytes(4, "big")

    monkeypatch.setattr(R._LiveRig, "_tiny_png", _tiny, raising=False)


# --------------------------------------------------------------------------- #
# 1. Every sibling-tagged rig upload is the same image: REPOST, not the script.
# --------------------------------------------------------------------------- #

def test_rig_uploads_are_not_scored_as_reposts_of_each_other(monkeypatch, tmp_path):
    """R2/R4(after R6)/R7/R9 must score as the script says, not as REPOSTs of R1.

    Spec 50 section 7 puts U_HUMAN and U_TARGET in ONE sibling group with
    ``selfie_bonus: true``, so every U_HUMAN -> U_TARGET upload is sib-tagged and has its
    rendition hashed (20 section 5.2.2); rules gate REPOST (a sib-tagged message whose
    rendition hash was already seen this semester) BEFORE the veto, cooldown and
    LATE_TAG checks. The rig uploads the identical ``_TINY_PNG`` bytes for R1, R2, R4,
    both R7 fills and R9 (a 1x1 PNG has no thumb_480+, G2 fact 13, so the hash is of
    the original), so R2 scores REPOST instead of 50 section 7.2's COOLDOWN, R4 scores
    REPOST instead of R6's LATE_TAG, the R7 fills never count, and R9 never revises
    R7's digest (R9: revision:1).
    """
    rig = Rig(AFTERNOON_US)
    results = _run(monkeypatch, tmp_path, rig)

    rows = results.ledger_rows
    reposts = [r["ts"] for r in rows if r.get("reason") == "repost"]
    assert reposts == [], f"rig uploads scored as REPOST: {reposts}"
    r4 = A.find_row(rows, results.ts_by_step["R4"])
    assert r4.get("reason") == "late_tag", f"R4 scored {r4.get('reason')} after R6"


# --------------------------------------------------------------------------- #
# 2. The single end-of-run ledger read-back vs the R5 veto.
# --------------------------------------------------------------------------- #

def test_r2_cooldown_verdict_survives_the_r5_veto_in_the_read_back(monkeypatch, tmp_path):
    """The read-back must show R2 as COOLDOWN blocked_by R1 (50 section 7.2 R2).

    Rules (00 section 4 gate precedence, 20 section 5): a VETOED message is gated before
    the cooldown sweep, so it anchors no cooldown. Once R5's admin veto lands on R1, the
    next sync re-scores R2 as COUNTED with no blocked_by, and its hourglass becomes the
    counted emoji. The rig reads the ledger rows only once, after R13 (``_read_back``),
    so ``test_rig_ledger_rows``' R2 assertions (COOLDOWN, blocked_by = R1) can never pass
    against a correct sync: the R2 row must be captured after R2's own sync. (Images are
    made distinct here so the identical-bytes REPOST finding does not mask this one.)
    """
    _distinct_tiny_images(monkeypatch)
    rig = Rig(AFTERNOON_US)
    results = _run(monkeypatch, tmp_path, rig)

    rows = results.ledger_rows
    ids = results.ts_by_step
    A.assert_status(rows, ids["R2"], "COOLDOWN")
    A.assert_blocked_by(rows, ids["R2"], ids["R1"])


# --------------------------------------------------------------------------- #
# 3. The digest due time vs the time of day the rig is started.
# --------------------------------------------------------------------------- #

def test_no_digest_is_posted_at_r1_when_the_rig_starts_in_the_evening(monkeypatch, tmp_path):
    """R1's sync must post no digest ("digest not yet due"), whatever the start time.

    Spec 50 section 7.2 R1: "digest not yet due"; R7: advance to the report anchor and
    exactly one digest, revision:0. The template's anchors are fixed (daily 21:00,
    officers 21:05, America/New_York), the semester starts on the run's own local date,
    and ``_advance_clock_to_anchors`` only ever moves the clock forward. Started at
    21:30 local, today's daily period is already due at R1 (20 section 6: most recent
    due period within 24 h of its anchor), so R1's sync posts the daily digest, R7's
    advance is a no-op and R7 only revises the digest R1 posted.
    """
    _distinct_tiny_images(monkeypatch)
    rig = Rig(_us(2026, 9, 25, 21, 30))
    results = _run(monkeypatch, tmp_path, rig)

    r1_sync = next(s for s in rig.syncs if s["uploads_before"] >= 1)
    assert results.ts_by_step["R1"] == rig.uploads[0]["ts"]
    assert r1_sync["bot_posts"] == [], (
        f"R1's sync already posted {len(r1_sync['bot_posts'])} digest(s)")


# --------------------------------------------------------------------------- #
# 4. The R0 preflight's exit code.
# --------------------------------------------------------------------------- #

def test_a_failing_r0_doctor_does_not_let_the_rig_run_on(monkeypatch, tmp_path):
    """R0 "all FAIL checks pass on the rig workspace" must be enforced.

    Spec 50 section 7.2 R0 and 40 section 4.4: ``doctor`` exits DOCTOR_FAILED (10) when a
    FAIL check fails. ``_run_doctor`` (and ``_sync``) discard ``snipebot.cli.main``'s
    return code, and ``RigResults`` records no doctor outcome, so a doctor FAIL on the
    rig workspace is invisible: the rig posts, syncs and returns results as if R0 passed
    (the same holds for a sync that exits non-zero).
    """
    rig = Rig(AFTERNOON_US)
    rig.doctor_rc = 10
    _install(monkeypatch, rig)
    with pytest.raises(Exception):
        R.run(_env(), workdir=tmp_path / "rig")
    assert rig.uploads == [], "the rig posted into the workspace after R0 failed"


# --------------------------------------------------------------------------- #
# 5. Teardown leaves the run's digests behind: a same-day re-run never posts one.
# --------------------------------------------------------------------------- #

def test_a_same_day_rerun_still_posts_its_own_r7_and_r8_digests(monkeypatch, tmp_path):
    """A second rig run on the same day must exercise the R7 post and the R8 post_to.

    Spec 50 section 7.2 R7 ("exactly one digest in C_MAIN ... revision:0") and R8
    ("exactly one digest in C_OFF"). Digest dedup is derived from the channels, keyed
    on (channel, period_key) (20 section 6, sync.post_digests), and the period keys
    (daily:<date>, officers:<date>) do not depend on the run. ``_teardown`` deletes only
    the user-token posts, so the bot's digests from the first run stay in C_MAIN and
    C_OFF; a re-run the same day (routine after a failed run) finds them, Pass A skips
    both posts, and Pass B chat.updates the old digests with the new run's numbers.
    The post and post_to paths are never exercised and the rig's digest checks pass on
    the previous run's messages.
    """
    _distinct_tiny_images(monkeypatch)
    rig = Rig(AFTERNOON_US)
    _install(monkeypatch, rig)
    R.run(_env(), workdir=tmp_path / "rig1")
    before = len(rig.bot_posts())
    rig.clock_us += 60_000_000
    R.run(_env(), workdir=tmp_path / "rig2")
    posted = rig.bot_posts()[before:]
    channels = sorted(ch for ch, _ in posted)
    assert channels == [C_MAIN, C_OFF], (
        f"second run posted digests in {channels}, expected one in each channel")

