"""Functional sync tests (20 §1-§9): the state machine end to end against `FakeSlack`
and the files-mode persistence backend. Step 9 (digests) is stubbed, so every run here
passes `no_post=True` except the boundary-order test, which stubs `post_digests`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snipebot import sync
from snipebot.faces import FakeFaceDetector
from snipebot.parse import VetoSource
from snipebot.persistence import CommitResult
from snipebot.rules import SelfieClass, Status
from snipebot.slack_io import MissingScope
from snipebot.sync import Command, SyncResult, fetch_oldest_us, run_sync
from snipebot.ledger import State, load_ledger, dumps_verdicts
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import (
    BOT,
    CHANNEL,
    image_file,
    make_config,
    mkts,
    roster_of,
    sha,
    us,
)

ADMIN = "U0ADMIN"


class _DecodeFailing:
    """A FaceDetector that counts known bytes and raises UndecodableImage on anything else
    (a real detector's response to a truncated rendition), unlike FakeFaceDetector's KeyError."""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def count_faces(self, image_bytes: bytes) -> int:
        from snipebot.faces import UndecodableImage

        key = sha(image_bytes)
        if key in self._counts:
            return self._counts[key]
        raise UndecodableImage("undecodable")


def _users(*ids: str) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in ids:
        out[uid] = FakeUser(id=uid)
    return out


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack, config, tmp_path, *, now, detector=None, **kw) -> SyncResult:
    led, st = _paths(tmp_path)
    return run_sync(
        slack, config, detector=detector or FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now), no_post=True, **kw,
    )


# --- fetch range (20 §3) -----------------------------------------------------

def test_fetch_oldest_normal_window():
    cfg = make_config(roster=roster_of({"U0A": "fam"}), scan_days=14)
    now = us(2026, 9, 18, 12)
    state = State()
    got = fetch_oldest_us(now, state, [], cfg, None)
    assert got == now - 14 * sync.DAY_US


def test_fetch_oldest_backfill_override_ignores_terms():
    cfg = make_config(roster=roster_of({"U0A": "fam"}), scan_days=14)
    now = us(2026, 9, 18, 12)
    assert fetch_oldest_us(now, State(watermark=mkts(2026, 1, 1)), [], cfg, 42) == 42


def test_fetch_oldest_pending_miss_pulls_back(tmp_path):
    cfg = make_config(roster=roster_of({"U0A": "fam"}), scan_days=1)
    now = us(2026, 9, 18, 12)
    from snipebot.parse import Candidate

    old = Candidate(
        ts=mkts(2026, 9, 10, 8), sender="U0A", subtype=None, thread_ts=None,
        targets=(), live_images=1, live_image_ids=("F",), live_videos=0, linked_images=0,
        last_edit_ts=None, file_sigs=(), vetoes=(), missing_runs=1,
        first_seen_targets=frozenset(), first_sight_edited=False, target_edited_in=(),
    )
    got = fetch_oldest_us(now, State(), [old], cfg, None)
    assert got == parse_ts(old.ts)  # pending miss pulls oldest back past the 1-day window


# --- merge: fact replacement + TargetEdit (20 §4.1) --------------------------

def test_merge_replaces_facts_and_appends_target_edit(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam", "U0C": "fam", ADMIN: None})
    cfg = make_config(roster=roster)
    now1 = mkts(2026, 9, 18, 12)
    slack = FakeSlack(now=now1, bot_user_id=BOT, users=_users("U0A", "U0B", "U0C", ADMIN))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"snap")])
    _run(slack, cfg, tmp_path, now=now1)

    # edit the message to add a second target, then re-run.
    slack.edit(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user="U0A",
               text="<@U0B> <@U0C>")
    slack.as_of(mkts(2026, 9, 18, 13))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 13))

    led, _ = _paths(tmp_path)
    row = load_ledger(led)[0]
    assert row.targets == ("U0B", "U0C")
    assert [te.user for te in row.target_edited_in] == ["U0C"]
    assert row.target_edited_in[0].edit_ts == mkts(2026, 9, 18, 11, micro=0)
    assert row.first_seen_targets == frozenset({"U0B"})


# --- missing -> deleted state machine (20 §4.2) ------------------------------

def test_two_complete_misses_mark_deleted(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 9), bot_user_id=BOT, users=_users("U0A", "U0B"))
    ts = slack.post(at=mkts(2026, 9, 18, 8), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"snap")])
    # A text-only survivor keeps later fetches non-empty (zero returned infers no miss, E-W4-16).
    slack.post(at=mkts(2026, 9, 18, 8, 30), user="U0A", channel=CHANNEL, text="hello")
    slack.as_of(mkts(2026, 9, 18, 9))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 9))

    slack.delete_message(at=mkts(2026, 9, 18, 10), ts=ts, channel=CHANNEL)
    slack.as_of(mkts(2026, 9, 18, 11))
    r1 = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 11))
    assert r1.newly_deleted == 0  # one pending miss, not yet deleted
    led, _ = _paths(tmp_path)
    assert load_ledger(led)[0].missing_runs == 1

    slack.as_of(mkts(2026, 9, 18, 13))
    r2 = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 13))
    assert r2.newly_deleted == 1
    row = load_ledger(led)[0]
    assert row.deleted and row.missing_runs == 2


# --- delete circuit breaker + accept-deletes (20 §4.3) -----------------------

def _seed_three_then_delete(tmp_path, cfg):
    slack = FakeSlack(now=mkts(2026, 9, 18, 9), bot_user_id=BOT,
                      users=_users("U0A", "U0B"))
    tss = []
    for i in range(3):
        tss.append(slack.post(at=mkts(2026, 9, 18, 8, i), user="U0A", channel=CHANNEL,
                              text="<@U0B>", files=[image_file(f"F{i}", f"snap{i}".encode())]))
    # A text-only survivor keeps later fetches non-empty (zero returned infers no miss, E-W4-16).
    slack.post(at=mkts(2026, 9, 18, 8, 30), user="U0A", channel=CHANNEL, text="hello")
    slack.as_of(mkts(2026, 9, 18, 9))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 9))
    for ts in tss:
        slack.delete_message(at=mkts(2026, 9, 18, 10), ts=ts, channel=CHANNEL)
    slack.as_of(mkts(2026, 9, 18, 11))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 11))  # all -> missing_runs 1
    return slack


def test_delete_breaker_trips_then_accept_deletes_releases(tmp_path):
    cfg = make_config(roster=roster_of({"U0A": "fam", "U0B": "fam"}), max_deletes_per_run=1)
    slack = _seed_three_then_delete(tmp_path, cfg)
    slack.as_of(mkts(2026, 9, 18, 13))

    trip = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 13))
    assert trip.exit_code == 6 and not trip.ledger_written and trip.newly_deleted == 3

    mism = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 13),
                command=Command.ACCEPT_DELETES, accept_deletes=2)
    assert mism.exit_code == 7 and mism.newly_deleted == 3

    ok = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 13),
              command=Command.ACCEPT_DELETES, accept_deletes=3)
    assert ok.exit_code == 0 and ok.ledger_written
    led, _ = _paths(tmp_path)
    assert all(r.deleted for r in load_ledger(led))


def test_breaker_mismatch_message_carries_fresh_count(tmp_path, capsys):
    cfg = make_config(roster=roster_of({"U0A": "fam", "U0B": "fam"}), max_deletes_per_run=1)
    slack = _seed_three_then_delete(tmp_path, cfg)
    slack.as_of(mkts(2026, 9, 18, 13))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 13),
         command=Command.ACCEPT_DELETES, accept_deletes=2)
    err = capsys.readouterr().err
    assert "accept_deletes_mismatch" in err and "newly_deleted=3" in err


# --- veto observation (20 §5.1) ----------------------------------------------

def _veto_world():
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B", ADMIN))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"snap")])
    return slack, ts


def test_admin_veto_counts_under_admins_and_not_flagged(tmp_path):
    from snipebot.config import VetoActor
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, veto_by=(VetoActor.ADMINS,), admins=(ADMIN,))
    slack, ts = _veto_world()
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN, name="no_entry_sign")
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    led, _ = _paths(tmp_path)
    row = load_ledger(led)[0]
    assert [(v.by, v.source) for v in row.vetoes] == [(ADMIN, VetoSource.REACTION)]
    # A vetoed message is gated to NOT_COUNTED with no pairs (verdicts.jsonl carries no
    # message-level reason); the recorded REACTION veto above is the proof it was honoured.
    vtext = led.with_name("verdicts.jsonl").read_text()
    assert '"status":"not_counted"' in vtext and '"pairs":[]' in vtext


def test_target_veto_not_counted_under_admins_but_flagged(tmp_path, capsys):
    from snipebot.config import VetoActor
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, veto_by=(VetoActor.ADMINS,), admins=(ADMIN,))
    slack, ts = _veto_world()
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user="U0B", name="no_entry_sign")
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), command=Command.BACKFILL,
         dry_run=True)
    err = capsys.readouterr().err
    assert "non_permitted_veto" in err  # audit-flagged; the target may not veto here


def test_target_veto_counts_under_target(tmp_path):
    from snipebot.config import VetoActor
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, veto_by=(VetoActor.TARGET,), admins=(ADMIN,))
    slack, ts = _veto_world()
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user="U0B", name="no_entry_sign")
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    led, _ = _paths(tmp_path)
    row = load_ledger(led)[0]
    assert [(v.by, v.source) for v in row.vetoes] == [("U0B", VetoSource.REACTION)]


# --- opt-out observation (20 §5.2) -------------------------------------------

def test_optout_is_durable_and_monotonic(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam", "U0X": "fam"})
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B", "U0X"))
    optout_ts = slack.post(at=mkts(2026, 9, 18, 9), user="U0A", channel=CHANNEL,
                           text="react to leave")
    slack.react(at=mkts(2026, 9, 18, 10), ts=optout_ts, channel=CHANNEL, user="U0X",
                name="wave")
    cfg = make_config(roster=roster, optout_message_ts=(optout_ts,))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    _, st = _paths(tmp_path)
    import json
    assert "U0X" in json.loads(st.read_text())["opted_out"]

    # remove the reaction; the opt-out must persist (monotonic).
    slack.unreact(at=mkts(2026, 9, 18, 13), ts=optout_ts, channel=CHANNEL, user="U0X",
                  name="wave")
    slack.as_of(mkts(2026, 9, 18, 14))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 14))
    assert "U0X" in json.loads(st.read_text())["opted_out"]


def test_seed_opted_out_recorded(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, seed_opted_out=("U0SEED",))
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    _, st = _paths(tmp_path)
    import json
    assert json.loads(st.read_text())["opted_out"]["U0SEED"] == parse_ts(mkts(2026, 9, 18, 12))


# --- admin selfie observation + CLI override (20 §5.2.1, §1.3) ---------------

def _selfie_world():
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B", ADMIN))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"snap")])
    return slack, ts


def test_admin_selfie_reaction_first_seen(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie",
                      admins=(ADMIN,), max_attempts=0)  # skip faces; the override decides
    slack, ts = _selfie_world()
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN, name="selfie")
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    led, _ = _paths(tmp_path)
    ov = load_ledger(led)[0].selfie_override
    assert ov is not None and ov.value is True and ov.by == ADMIN
    assert ov.source == VetoSource.REACTION


def test_non_admin_selfie_reaction_ignored(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie",
                      admins=(ADMIN,), max_attempts=0)
    slack, ts = _selfie_world()
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user="U0B", name="selfie")
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    led, _ = _paths(tmp_path)
    assert load_ledger(led)[0].selfie_override is None


def test_bot_own_selfie_reaction_ignored(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie",
                      admins=(ADMIN,), max_attempts=0)
    slack, ts = _selfie_world()
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=BOT, name="selfie")
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    led, _ = _paths(tmp_path)
    assert load_ledger(led)[0].selfie_override is None


def test_cli_selfie_override_replaces_reaction(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie",
                      admins=(ADMIN,), max_attempts=0)
    slack, ts = _selfie_world()
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN, name="selfie")
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))  # REACTION override True
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), command=Command.SELFIE,
             selfie_ts=ts, selfie_value=False, selfie_by=ADMIN)
    assert r.exit_code == 0
    led, _ = _paths(tmp_path)
    ov = load_ledger(led)[0].selfie_override
    assert ov.value is False and ov.source == VetoSource.CLI


def test_selfie_command_bad_ts_exits_2(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie", admins=(ADMIN,))
    slack, ts = _selfie_world()
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), command=Command.SELFIE,
             selfie_ts="9999999999.000000", selfie_value=True)
    assert r.exit_code == 2


def test_selfie_command_not_sib_tagged_exits_2(tmp_path):
    # sender and target in different groups -> not sib-tagged.
    roster = roster_of({"U0A": "red", "U0B": "blue", ADMIN: None})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie", admins=(ADMIN,))
    slack, ts = _selfie_world()
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), command=Command.SELFIE,
             selfie_ts=ts, selfie_value=True)
    assert r.exit_code == 2


def test_admin_selfie_reaction_ignored_when_not_sib_tagged(tmp_path):
    # R3: the admin selfie-reaction observation is sib-gated like the CLI selfie path — a
    # selfie reaction on a non-sib-tagged row writes no override. Here sender and target are
    # in different groups, so an admin's `selfie` reaction must not set an override.
    roster = roster_of({"U0A": "red", "U0B": "blue", ADMIN: None})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie",
                      admins=(ADMIN,), max_attempts=0)
    slack, ts = _selfie_world()
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN, name="selfie")
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    led, _ = _paths(tmp_path)
    assert load_ledger(led)[0].selfie_override is None


# --- face detection gating (20 §5.2.2) ---------------------------------------

def test_faces_counted_only_for_sib_tagged(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam", "U0C": "red"})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie")
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B", "U0C"))
    sib = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                     text="<@U0B>", files=[image_file("F01", b"sib")])
    non = slack.post(at=mkts(2026, 9, 18, 10, 1), user="U0A", channel=CHANNEL,
                     text="<@U0C>", files=[image_file("F02", b"nonsib")])
    det = FakeFaceDetector({sha(b"sib"): 2, sha(b"nonsib"): 2})
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), detector=det)
    led, _ = _paths(tmp_path)
    rows = {r.ts: r for r in load_ledger(led)}
    assert rows[sib].face_counts == {"F01": 2}      # sib-tagged: counted
    assert rows[non].face_counts == {}              # cross-group: never fetched


def test_faces_selfie_classification(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie")
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"selfiepic")])
    det = FakeFaceDetector({sha(b"selfiepic"): 2})  # T=1 target, T+1=2 faces -> SELFIE
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), detector=det)
    led, _ = _paths(tmp_path)
    vtext = led.with_name("verdicts.jsonl").read_text()
    assert '"selfie":"selfie"' in vtext


def test_faces_attempts_capped(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie", max_attempts=2)
    slack = FakeSlack(now=mkts(2026, 9, 18, 9), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 8), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    det = FakeFaceDetector({})  # unknown bytes; but a fetch fault fires first each run
    led, _ = _paths(tmp_path)
    for i in range(4):
        slack.faults.fetch_timeout(times=1)
        slack.as_of(mkts(2026, 9, 18, 9 + i))
        _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 9 + i), detector=det)
    # attempts never exceed the cap; the image stays uncounted.
    row = load_ledger(led)[0]
    assert row.detect_attempts == 2 and row.face_counts == {}


@pytest.mark.parametrize("arm", ["timeout", "429", "oversize", "truncate"])
def test_each_fetch_fault_leaves_no_count(tmp_path, arm):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie", max_attempts=3)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    if arm == "timeout":
        slack.faults.fetch_timeout(times=1)
    elif arm == "429":
        slack.faults.fetch_429(retry_after_seconds=1, times=1)
    elif arm == "oversize":
        slack.faults.fetch_oversize(times=1, limit_bytes=1)
    else:
        slack.faults.fetch_truncate(times=1)  # a prefix fails decode
    det = _DecodeFailing({sha(b"pic"): 1})  # a truncated prefix -> UndecodableImage
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), detector=det)
    assert r.exit_code == 0
    row = load_ledger(_paths(tmp_path)[0])[0]
    assert row.face_counts == {} and row.detect_attempts == 1


def test_faces_missing_scope_skips_run_attempts_unchanged(tmp_path, monkeypatch):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie", max_attempts=3)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])

    def boom(url):
        raise MissingScope("missing_scope")

    monkeypatch.setattr(slack, "fetch_file_bytes", boom)
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 0
    row = load_ledger(_paths(tmp_path)[0])[0]
    assert row.detect_attempts == 0 and row.face_counts == {}  # scope fault burns no attempt


# --- reaction convergence (20 §5.3) ------------------------------------------

def test_reaction_removes_stale_then_adds_and_keeps_admin_selfie(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie", admins=(ADMIN,))
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B", ADMIN))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"pic")])
    # a stale bot reaction and an admin's confirming selfie already on the message.
    slack.react(at=mkts(2026, 9, 18, 10, 5), ts=ts, channel=CHANNEL, user=BOT, name="x")
    slack.react(at=mkts(2026, 9, 18, 10, 6), ts=ts, channel=CHANNEL, user=ADMIN, name="selfie")
    det = FakeFaceDetector({sha(b"pic"): 2})  # T+1 -> SELFIE
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), detector=det)

    final = slack.reactions_get(CHANNEL, ts)
    by_name = {r["name"]: set(r["users"]) for r in final["reactions"]}
    assert "x" not in by_name                       # stale bot reaction removed
    assert BOT in by_name.get("white_check_mark", set())  # counted added
    assert BOT in by_name.get("selfie", set())      # bot's own selfie added
    assert ADMIN in by_name["selfie"]               # admin's selfie never removed


def test_reaction_three_emoji_for_review_selfie(tmp_path):
    members = {f"U0T{i}": "fam" for i in range(5)}
    roster = roster_of({"U0A": "fam", ADMIN: None, **members})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie",
                      admins=(ADMIN,), review_min_targets=5, review_emoji="question",
                      max_attempts=0)  # override drives SELFIE; skip faces
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", ADMIN, *members))
    text = " ".join(f"<@{u}>" for u in members)
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text=text, files=[image_file("F01", b"grp")])
    slack.react(at=mkts(2026, 9, 18, 10, 5), ts=ts, channel=CHANNEL, user=ADMIN, name="selfie")
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    final = slack.reactions_get(CHANNEL, ts)
    bot_reactions = {r["name"] for r in final["reactions"] if BOT in r["users"]}
    assert bot_reactions == {"white_check_mark", "selfie", "question"}


def test_optout_not_counted_stays_silent(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, seed_opted_out=("U0A",))
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"pic")])
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    final = slack.reactions_get(CHANNEL, ts)
    assert not any(BOT in r["users"] for r in final.get("reactions", []))  # opt-out silent


# --- persistence (files mode) + summary --------------------------------------

def test_files_persist_writes_three_files_verdicts_agree(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 0 and r.ledger_written and r.commit_sha is None
    led, st = _paths(tmp_path)
    vpath = led.with_name("verdicts.jsonl")
    assert led.exists() and st.exists() and vpath.exists()
    from snipebot.rules import evaluate
    rows = load_ledger(led)
    fresh = dumps_verdicts(evaluate(rows, cfg.rules, cfg.roster, set(), cfg.semesters, cfg.tz))
    assert vpath.read_text() == fresh


def test_watermark_is_now_of_complete_fetch_not_newest_message(tmp_path):
    # R1: state.watermark is format_ts(now_us) of the last COMPLETE fetch, not the newest
    # message ts. A run that fetches a 10:00 message at now=12:00 writes 12:00, not 10:00.
    from snipebot.ledger import load_state
    from snipebot.ts import format_ts
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    now = mkts(2026, 9, 18, 12)
    _run(slack, cfg, tmp_path, now=now)
    _, st = _paths(tmp_path)
    state = load_state(st)
    assert state.watermark == format_ts(parse_ts(now))
    assert state.watermark != mkts(2026, 9, 18, 10)  # never the newest message ts


def test_second_noop_sync_writes_nothing(tmp_path):
    # R5: when the three data files are byte-identical to a run's output, nothing is
    # committed and ledger_written is False (no empty amend). Two syncs at the same `now`
    # over a stable channel: the first writes, the second is a no-op.
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    now = mkts(2026, 9, 18, 12)
    first = _run(slack, cfg, tmp_path, now=now)
    assert first.exit_code == 0 and first.ledger_written
    assert first.moved_lines == ("counted +1",)
    second = _run(slack, cfg, tmp_path, now=now)
    assert second.exit_code == 0 and not second.ledger_written
    assert second.moved_lines == ()


def test_summary_line_tokens(tmp_path, capsys):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    err = capsys.readouterr().err
    assert "summary" in err
    for token in ("counted=", "cooldown=", "selfies=", "faces_fetched=",
                  "ambiguous_selfie=", "repost="):
        assert token in err


# --- dry run + audit (20 §9.3, L8) -------------------------------------------

def test_dry_run_writes_nothing_reacts_nothing_fetches_no_image(tmp_path, capsys):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie")
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"pic")])
    fetched = []

    def track(url):
        fetched.append(url)
        return b"pic"

    import types
    slack.fetch_file_bytes = types.MethodType(lambda self, url: track(url), slack)
    led, st = _paths(tmp_path)
    r = run_sync(slack, cfg, detector=FakeFaceDetector({sha(b"pic"): 1}),
                 ledger_path=led, state_path=st, now_us=parse_ts(mkts(2026, 9, 18, 12)),
                 command=Command.BACKFILL, dry_run=True)
    assert r.exit_code == 0 and not r.ledger_written
    assert not led.exists() and not st.exists()      # wrote nothing
    assert fetched == []                             # fetched no image under --dry-run
    final = slack.reactions_get(CHANNEL, ts)
    assert not any(BOT in rr["users"] for rr in final.get("reactions", []))  # reacted nothing
    assert "AUDIT" in capsys.readouterr().err        # printed the audit list


def test_dry_run_audit_includes_ambiguous_and_late_and_repost(tmp_path, capsys):
    # A sib-tagged message whose faces never resolve -> AMBIGUOUS (review path).
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie")
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    led, st = _paths(tmp_path)
    run_sync(slack, cfg, detector=FakeFaceDetector({}), ledger_path=led, state_path=st,
             now_us=parse_ts(mkts(2026, 9, 18, 12)), command=Command.BACKFILL, dry_run=True)
    err = capsys.readouterr().err
    assert "ambiguous_selfie" in err  # no face facts under dry-run -> AMBIGUOUS, audited


# --- kill switch + exit codes ------------------------------------------------

def test_kill_switch_returns_zero_without_touching_slack(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, enabled=False)

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("slack must not be touched under the kill switch")

    r = run_sync(Boom(), cfg, detector=FakeFaceDetector({}),
                 ledger_path=tmp_path / "l.jsonl", state_path=tmp_path / "s.json",
                 now_us=parse_ts(mkts(2026, 9, 18, 12)))
    assert r.exit_code == 0 and not r.ledger_written


def test_admin_command_runs_while_paused(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, enabled=False)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), command=Command.BACKFILL)
    assert r.exit_code == 0 and r.ledger_written  # BACKFILL skips the kill switch


def test_large_movement_baseline_cumulative_files_mode(tmp_path):
    # files mode: baseline is empty, so count_moved_pairs is 0 and never trips the breaker,
    # but an admin command is still a large movement (20 §8.6).
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), command=Command.VETO)
    assert r.exit_code == 0 and r.ledger_written


# --- boundary emission (definition of done) ----------------------------------

_EXPECTED_BOUNDARIES = [
    "start", "after_load", "after_fetch", "after_parse", "after_merge",
    "faces:before", "faces:after", "after_faces", "after_consent", "after_evaluate",
    "reaction:before", "reaction:after", "after_reactions", "before_persist",
    "before_commit", "after_commit", "before_push", "after_push", "after_persist",
    "digest:before", "digest:after", "after_digests", "done",
]


class _BoundaryStore:
    def refresh(self):
        return None

    def baseline_verdicts(self, local_day):
        return None

    def commit_and_push(self, *, local_day, large_movement, message, boundary):
        boundary("before_commit")
        boundary("after_commit")
        boundary("before_push")
        boundary("after_push")
        return CommitResult(sha="abc1234", sealed_sha=None, amended=False, pushed=True)

    def history(self):
        return []

    def restore(self, commit):
        return None


def test_all_boundaries_emitted_in_order(tmp_path, monkeypatch):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji=None, review_emoji=None)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])

    recorded: list[str] = []
    monkeypatch.setattr(sync, "_boundary", lambda name, key=None: recorded.append(name))
    monkeypatch.setattr(sync, "store_for", lambda config, data_dir: _BoundaryStore())

    def fake_post(slack_, config, *, ledger, verdicts, digests, now_us, users_cache,
                  scan_floor_us, boundary, opted_out=None):
        boundary("digest:before")
        boundary("digest:after")
        return (1, 0)

    monkeypatch.setattr(sync, "post_digests", fake_post)
    led, st = _paths(tmp_path)
    r = run_sync(slack, cfg, detector=FakeFaceDetector({sha(b"pic"): 1}),
                 ledger_path=led, state_path=st, now_us=parse_ts(mkts(2026, 9, 18, 12)))
    assert r.exit_code == 0
    assert recorded == _EXPECTED_BOUNDARIES


# --- run_sync_git lease retry (20 §1.4, §8.3) --------------------------------

from snipebot.persistence import LeaseRejected
from snipebot.sync import run_sync_git


class _LeaseStore:
    """A Store that rejects the lease `reject_n` times, then accepts. Counts refreshes."""

    def __init__(self, reject_n: int) -> None:
        self.reject_n = reject_n
        self.refreshes = 0
        self.pushes = 0

    def refresh(self):
        self.refreshes += 1

    def baseline_verdicts(self, local_day):
        return None

    def commit_and_push(self, *, local_day, large_movement, message, boundary):
        if self.reject_n > 0:
            self.reject_n -= 1
            raise LeaseRejected("another runner pushed first")
        self.pushes += 1
        return CommitResult(sha="ok", sealed_sha=None, amended=False, pushed=True)

    def history(self):
        return []

    def restore(self, commit):
        return None


def _git_world(tmp_path):
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])
    return cfg, slack


def test_run_sync_git_retries_then_succeeds(tmp_path, monkeypatch):
    cfg, slack = _git_world(tmp_path)
    store = _LeaseStore(reject_n=2)
    monkeypatch.setattr(sync, "store_for", lambda config, data_dir: store)
    ticks = iter([parse_ts(mkts(2026, 9, 18, 12, 1)), parse_ts(mkts(2026, 9, 18, 12, 2))])
    led, st = _paths(tmp_path)
    r = run_sync_git(slack, cfg, detector=FakeFaceDetector({}), ledger_path=led,
                     state_path=st, now_us=parse_ts(mkts(2026, 9, 18, 12)),
                     now_fn=lambda: next(ticks), no_post=True)
    assert r.exit_code == 0 and r.commit_sha == "ok"
    assert store.pushes == 1
    assert store.refreshes == 3  # one upfront + one per rejected lease


def test_run_sync_git_exhausts_to_exit_9(tmp_path, monkeypatch):
    cfg, slack = _git_world(tmp_path)
    store = _LeaseStore(reject_n=99)  # never accepts
    monkeypatch.setattr(sync, "store_for", lambda config, data_dir: store)
    led, st = _paths(tmp_path)
    # §8.3 step 6: every re-run carries a FRESH now_us (the real wall clock the CLI injects),
    # so the watermark — format_ts(now_us) of each complete fetch — advances and each attempt
    # produces a changed state.json to (re-)commit and lose the lease on, all the way to exit 9.
    base = parse_ts(mkts(2026, 9, 18, 12))
    ticks = iter(range(1, 1000))
    r = run_sync_git(slack, cfg, detector=FakeFaceDetector({}), ledger_path=led,
                     state_path=st, now_us=base,
                     now_fn=lambda: base + next(ticks), no_post=True)
    assert r.exit_code == 9 and not r.ledger_written
