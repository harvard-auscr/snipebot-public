"""Regression tests for the wave-4 sync rulings E-W4-4, -12, -15, -16, -17, -22, -23, -34,
-35, driven through `run_sync` against `FakeSlack` with files-mode persistence under
`tmp_path`. Offline and deterministic; no network, no git.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snipebot.config import Cadence, ReportSpec, Section, Weekday
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger, load_state, save_state
from snipebot.sync import Command, SyncResult, _period_key_fits, run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import make_config, mkts, roster_of, us

CHANNEL = "C0MAIN01"
OFF = "C0OFF001"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
OTHER = "U0AAA003"
ADMIN = "U0AAA009"
THIRD = "U0AAA010"


def _photo(n: int) -> dict:
    data = f"photo-bytes-{n}".encode()
    return {
        "id": f"F0FILE{n:03d}",
        "mimetype": "image/jpeg",
        "name": f"photo-{n}.jpg",
        "size": len(data),
        "original_w": 100,
        "original_h": 100,
        "thumb_1024": f"https://fixture.invalid/thumb/{n}",
        "url_private_download": f"https://fixture.invalid/dl/{n}",
        "_bytes": data,
    }


def _world(now: str, **kw) -> FakeSlack:
    users = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in (SNIPER, TARGET, OTHER, ADMIN, THIRD):
        users[uid] = FakeUser(id=uid, display_name=uid)
    return FakeSlack(now=now, bot_user_id=BOT, users=users, **kw)


def _config(groups: dict[str, str | None] | None = None, **kw):
    groups = groups or {SNIPER: None, TARGET: None, OTHER: None, ADMIN: None, THIRD: None}
    return make_config(roster=roster_of(groups), admins=(ADMIN,), **kw)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack: FakeSlack, cfg, tmp_path: Path, now: str, detector=None, **kw) -> SyncResult:
    slack.as_of(now)
    led, st = _paths(tmp_path)
    kw.setdefault("no_post", True)
    kw.setdefault("no_react", True)
    return run_sync(
        slack, cfg, detector=detector or FakeFaceDetector({}), ledger_path=led,
        state_path=st, now_us=parse_ts(now), **kw,
    )


def _row(tmp_path: Path, ts: str):
    led, _ = _paths(tmp_path)
    return next(r for r in load_ledger(led) if r.ts == ts)


def _verdict(tmp_path: Path, ts: str) -> dict:
    led, _ = _paths(tmp_path)
    for line in led.with_name("verdicts.jsonl").read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("ts") == ts:
            return obj
    raise AssertionError("no verdict for the row")


def _daily(name: str = "daily", post_to: str | None = None) -> ReportSpec:
    return ReportSpec(name=name, cadence=Cadence.DAILY, at_hour=21, at_minute=0,
                      weekday=None, post_to=post_to, sections=(Section.DAY,), top_n=5)


def _digest_meta(report: str, period_key: str, channel: str = CHANNEL) -> dict:
    return {"event_type": "snipe_digest", "event_payload": {
        "report": report, "period_key": period_key, "channel": channel,
        "semester": "fall-2026", "numbers_hash": "0" * 64, "revision": 0,
    }}


def _bot_messages(slack: FakeSlack, channel: str = CHANNEL) -> list[dict]:
    return [m for m in slack.history(channel, mkts(2026, 9, 1)) if m.get("user") == BOT]


# --- E-W4-4: a stored row returned as a non-candidate takes a miss --------------------

def _tombstone_history(slack: FakeSlack, ts: str):
    real_history = slack.history

    def history(channel, oldest, latest=None):
        out = []
        for m in real_history(channel, oldest, latest):
            if m.get("ts") == ts:
                m = {"type": "message", "subtype": "tombstone", "hidden": True,
                     "text": "This message was deleted.", "ts": ts, "thread_ts": ts,
                     "reply_count": 1}
            out.append(m)
        return out
    return history


def test_tombstone_parent_takes_a_miss_then_is_deleted_at_two(tmp_path):
    """E-W4-4: a stored row whose ts comes back as a `subtype: tombstone` parent takes a
    miss exactly as if it were absent: missing_runs 1, then deleted at 2."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(1)])
    slack.post(at=mkts(2026, 9, 18, 10, 30), user=THIRD, channel=CHANNEL, text="other")
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12)).exit_code == 0
    assert _verdict(tmp_path, ts)["status"] == "counted"

    slack.history = _tombstone_history(slack, ts)  # type: ignore[method-assign]
    r1 = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 13))
    assert r1.exit_code == 0 and r1.newly_deleted == 0
    assert _row(tmp_path, ts).missing_runs == 1
    r2 = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 14))
    assert r2.exit_code == 0 and r2.newly_deleted == 1
    row = _row(tmp_path, ts)
    assert row.deleted and row.missing_runs == 2
    assert _verdict(tmp_path, ts)["status"] != "counted"


def test_tombstone_parent_keeps_its_stored_veto_facts(tmp_path):
    """E-W4-4: the tombstone is not observed by step 5, so the row's REACTION veto stays
    unchanged on its pending-miss run, as an unreturned row's facts do (20 §4.2)."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(2)])
    slack.post(at=mkts(2026, 9, 18, 10, 30), user=THIRD, channel=CHANNEL, text="other")
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN,
                name="no_entry_sign")
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12)).exit_code == 0
    vetoes = _row(tmp_path, ts).vetoes
    assert vetoes

    slack.history = _tombstone_history(slack, ts)  # type: ignore[method-assign]
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 13)).exit_code == 0
    row = _row(tmp_path, ts)
    assert row.missing_runs == 1 and row.vetoes == vetoes


# --- E-W4-12: Pass B skips a digest whose period key predates the cadence -------------

def test_period_key_fits_current_cadence_only():
    """E-W4-12: a period key parses only under the cadence that minted it."""
    daily = _daily("standings")
    weekly = ReportSpec(name="standings", cadence=Cadence.WEEKLY, at_hour=20, at_minute=0,
                        weekday=Weekday.SUN, post_to=None, sections=(Section.WEEK,),
                        top_n=5)
    final = ReportSpec(name="standings", cadence=Cadence.FINAL, at_hour=20, at_minute=0,
                       weekday=None, post_to=None, sections=(Section.SEMESTER,), top_n=5)
    assert _period_key_fits(daily, "standings:2026-09-18", "fall-2026")
    assert not _period_key_fits(daily, "standings:2026-W38", "fall-2026")
    assert not _period_key_fits(daily, "other:2026-09-18", "fall-2026")
    assert not _period_key_fits(daily, "standings:2026-02-30", "fall-2026")
    assert _period_key_fits(weekly, "standings:2026-W38", "fall-2026")
    assert not _period_key_fits(weekly, "standings:2026-09-18", "fall-2026")
    assert not _period_key_fits(weekly, "standings:2026-W99", "fall-2026")
    assert _period_key_fits(final, "standings:fall-2026", "fall-2026")
    assert not _period_key_fits(final, "standings:2026-09-18", "fall-2026")


def test_pass_b_skips_digest_minted_under_old_cadence_with_warn(tmp_path, capsys):
    """E-W4-12: after a report's cadence changes (daily -> weekly, same name), the old
    daily digest in the scan window is skipped with a WARN, never re-rendered or an error."""
    roster = {SNIPER: "fam", TARGET: "fam"}
    weekly = ReportSpec(name="standings", cadence=Cadence.WEEKLY, at_hour=20, at_minute=0,
                        weekday=Weekday.SUN, post_to=None, sections=(Section.WEEK,),
                        top_n=5)
    cfg_daily = _config(roster, reports=(_daily("standings"),))
    cfg_weekly = _config(roster, reports=(weekly,))
    slack = _world(mkts(2026, 9, 18, 21, 30))
    slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(3)])
    r1 = _run(slack, cfg_daily, tmp_path, mkts(2026, 9, 18, 21, 30), no_post=False)
    assert r1.exit_code == 0 and r1.digests_posted == 1

    slack.post(at=mkts(2026, 9, 19, 9), user=TARGET, channel=CHANNEL,
               text=f"<@{SNIPER}>", files=[_photo(4)])
    capsys.readouterr()
    r2 = _run(slack, cfg_weekly, tmp_path, mkts(2026, 9, 19, 10), no_post=False)
    assert r2.exit_code == 0 and r2.digests_revised == 0
    err = capsys.readouterr().err
    assert "WARN  digest_skipped" in err and "reason=cadence" in err


# --- E-W4-15: a narrow backfill leaves the watermark ---------------------------------

def test_narrow_backfill_leaves_watermark_and_full_fetch_advances_it(tmp_path):
    """E-W4-15: a complete fetch whose `oldest` lies after the stored watermark (a narrow
    `backfill --from X`) leaves the watermark; one reaching back to it moves it to now."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 2, 12))
    slack.post(at=mkts(2026, 9, 2, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(5)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 2, 12)).exit_code == 0
    _, st = _paths(tmp_path)
    assert load_state(st).watermark == mkts(2026, 9, 2, 12)

    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 12), command=Command.BACKFILL,
             backfill_from_us=us(2026, 9, 22))
    assert r.exit_code == 0
    assert load_state(st).watermark == mkts(2026, 9, 2, 12)

    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 13), command=Command.BACKFILL,
             backfill_from_us=us(2026, 9, 1))
    assert r.exit_code == 0
    assert load_state(st).watermark == mkts(2026, 9, 25, 13)


# --- E-W4-16: a zero-message fetch infers no misses ----------------------------------

def test_zero_message_fetches_leave_missing_runs_unchanged(tmp_path):
    """E-W4-16: a complete fetch returning zero messages changes no row's missing_runs."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 18, 12))
    a = slack.post(at=mkts(2026, 9, 17, 10), user=SNIPER, channel=CHANNEL,
                   text=f"<@{TARGET}>", files=[_photo(6)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12)).exit_code == 0
    slack.faults.vanish(ts=a, for_fetches=2)
    for minute in (10, 20):
        r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12, minute))
        assert r.exit_code == 0 and r.newly_deleted == 0
        assert _row(tmp_path, a).missing_runs == 0
    assert _verdict(tmp_path, a)["status"] == "counted"


# --- E-W4-17: fingerprints_at persisted and used by the guard ------------------------

def _dated_roster(join_us: int):
    from snipebot.config import Roster, RosterEntry
    return Roster(entries={
        SNIPER: RosterEntry(user=SNIPER, join_us=0, group=None, is_bot=False),
        TARGET: RosterEntry(user=TARGET, join_us=0, group=None, is_bot=False),
        OTHER: RosterEntry(user=OTHER, join_us=join_us, group=None, is_bot=False),
        ADMIN: RosterEntry(user=ADMIN, join_us=0, group=None, is_bot=False),
    }, count_intra_group=True)


def test_state_records_fingerprints_at_newest_row(tmp_path):
    """E-W4-17: state.json gains `fingerprints_at`, the H the fingerprints were computed
    over (the ledger's newest row)."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 18, 12))
    slack.post(at=mkts(2026, 9, 17, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(7)])
    b = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                   text=f"<@{OTHER}>", files=[_photo(8)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12)).exit_code == 0
    _, st = _paths(tmp_path)
    assert load_state(st).fingerprints_at == b


def test_guard_recomputes_at_stored_fingerprints_at(tmp_path):
    """E-W4-17: with a ledger newer than the stored fingerprints (a files-mode crash
    between the renames), the guard recomputes at `fingerprints_at`, so a dated roster
    addition between the two H values does not trip it; the run converges."""
    cfg = make_config(roster=_dated_roster(us(2026, 9, 10)), admins=(ADMIN,))
    slack = _world(mkts(2026, 9, 8, 12))
    a = slack.post(at=mkts(2026, 9, 8, 10), user=SNIPER, channel=CHANNEL,
                   text=f"<@{TARGET}>", files=[_photo(9)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 8, 12)).exit_code == 0
    _, st = _paths(tmp_path)
    old_state = load_state(st)
    assert old_state.fingerprints_at == a

    b = slack.post(at=mkts(2026, 9, 11, 10), user=SNIPER, channel=CHANNEL,
                   text=f"<@{OTHER}>", files=[_photo(10)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 11, 12)).exit_code == 0
    save_state(st, old_state)          # the state rename never happened
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 11, 12, 10))
    assert r.exit_code == 0
    assert load_state(st).fingerprints_at == b


def test_guard_still_refuses_undated_roster_addition(tmp_path):
    """E-W4-17: recomputing at the stored `fingerprints_at` still refuses (exit 3) a player
    added without `from:`, which would re-judge existing rows."""
    cfg = make_config(roster=_dated_roster(us(2026, 9, 10)), admins=(ADMIN,))
    slack = _world(mkts(2026, 9, 8, 12))
    slack.post(at=mkts(2026, 9, 8, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(11)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 8, 12)).exit_code == 0
    undated = make_config(roster=_dated_roster(0), admins=(ADMIN,))
    assert _run(slack, undated, tmp_path, mkts(2026, 9, 8, 12, 10)).exit_code == 3


# --- E-W4-22: only this bot's digests feed dedup and revision ------------------------

def test_person_posted_digest_is_ignored_by_both_passes(tmp_path):
    """E-W4-22: a `snipe_digest` message posted by a person does not satisfy Pass A dedup
    (the bot still posts its own digest) and is never revised by Pass B."""
    cfg = _config(reports=(_daily(),))
    slack = _world(mkts(2026, 9, 18, 21, 30))
    slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(12)])
    slack.post(at=mkts(2026, 9, 18, 21, 10), user=THIRD, channel=CHANNEL, text="digest",
               metadata=_digest_meta("daily", "daily:2026-09-18"))
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 21, 30), no_post=False)
    assert r.exit_code == 0
    assert (r.digests_posted, r.digests_revised) == (1, 0)
    assert len(_bot_messages(slack)) == 1


def test_digest_with_foreign_bot_id_is_ignored(tmp_path):
    """E-W4-22: a digest carrying another app's `bot_id` (a person's user token) next to a
    human `user` is not this bot's: the bot posts its own."""
    cfg = _config(reports=(_daily(),))
    slack = _world(mkts(2026, 9, 18, 21, 30))
    slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(13)])
    human = slack.post(at=mkts(2026, 9, 18, 21, 10), user=THIRD, channel=CHANNEL,
                       text="digest", metadata=_digest_meta("daily", "daily:2026-09-18"))
    real_history = slack.history

    def history(channel, oldest, latest=None):
        out = real_history(channel, oldest, latest)
        for m in out:
            if m.get("ts") == human:
                m["bot_id"] = "B0USRAPP"
        return out

    slack.history = history  # type: ignore[method-assign]
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 21, 30), no_post=False)
    assert r.exit_code == 0 and r.digests_posted == 1


def test_own_digest_still_deduplicates(tmp_path):
    """E-W4-22: the bot's own digest still satisfies Pass A dedup on the next run."""
    cfg = _config(reports=(_daily(),))
    slack = _world(mkts(2026, 9, 18, 21, 30))
    slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
               text=f"<@{TARGET}>", files=[_photo(14)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 21, 30),
                no_post=False).digests_posted == 1
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 21, 40), no_post=False)
    assert r.exit_code == 0 and r.digests_posted == 0
    assert len(_bot_messages(slack)) == 1


# --- E-W4-23: nothing writes to Slack while paused -----------------------------------

def test_paused_admin_command_updates_ledger_but_writes_nothing_to_slack(tmp_path):
    """E-W4-23: while `enabled: false`, an admin command (backfill) still updates the
    ledger, but places no reaction and posts no digest."""
    cfg = _config(reports=(_daily(),), enabled=False)
    slack = _world(mkts(2026, 9, 18, 21, 30))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(15)])
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 21, 30), command=Command.BACKFILL,
             backfill_from_us=us(2026, 9, 1), no_react=False, no_post=False)
    assert r.exit_code == 0 and r.ledger_written
    assert (r.reactions_added, r.digests_posted) == (0, 0)
    assert _row(tmp_path, ts).ts == ts
    msg = next(m for m in slack.history(CHANNEL, mkts(2026, 9, 1)) if m["ts"] == ts)
    assert not any(BOT in rx.get("users", []) for rx in msg.get("reactions", []))
    assert _bot_messages(slack) == []


# --- E-W4-34: an unreadable post_to channel skips only its reports' digests ----------

@pytest.mark.parametrize("channels,member_of", [
    ((CHANNEL, OFF), (CHANNEL,)),       # not_in_channel
    ((CHANNEL,), (CHANNEL,)),           # channel_not_found
])
def test_unreadable_post_to_skips_its_digests_only(tmp_path, capsys, channels, member_of):
    """E-W4-34: a report whose post_to cannot be read is skipped with one WARN; scoring
    and the watched channel's digest proceed, and the run exits 0."""
    cfg = _config(reports=(_daily(), _daily("officers", post_to=OFF)))
    slack = _world(mkts(2026, 9, 18, 21, 30), channels=channels, bot_member_of=member_of)
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(16)])
    capsys.readouterr()
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 21, 30), no_post=False)
    assert r.exit_code == 0 and r.ledger_written
    assert r.digests_posted == 1
    assert _verdict(tmp_path, ts)["status"] == "counted"
    err = capsys.readouterr().err
    assert err.count("WARN  post_to_unreadable") == 1


def test_unreadable_watched_channel_still_aborts(tmp_path):
    """E-W4-34: only the watched channel's fetch failing aborts the run (exit 5)."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 18, 12), channels=(CHANNEL,), bot_member_of=())
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12)).exit_code == 5


# --- E-W4-35: a faces failure is a failed attempt, never an abort --------------------

class _cv2_error(Exception):
    """Stands in for cv2.error."""


class _FailingDetector(FakeFaceDetector):
    def __init__(self, exc: BaseException) -> None:
        super().__init__({})
        self.exc = exc

    def count_faces(self, image_bytes: bytes) -> int:
        raise self.exc


@pytest.mark.parametrize("exc", [
    FileNotFoundError("model"), OSError("model unreadable"), _cv2_error("decode"),
    ImportError("pillow_heif"),
])
def test_faces_failure_is_a_failed_attempt_not_an_abort(tmp_path, exc):
    """E-W4-35: a detector fault (model file missing or unreadable, cv2.error, a
    pillow_heif import error) records a failed detection attempt and the run proceeds."""
    cfg = _config({SNIPER: "fam", TARGET: "fam"}, selfie_bonus=True)
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(17)])
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12), detector=_FailingDetector(exc))
    assert r.exit_code == 0 and r.ledger_written
    row = _row(tmp_path, ts)
    assert row.detect_attempts == 1 and row.face_counts == {}
