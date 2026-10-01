"""Regression tests for the wave-4 sync rulings E-W4-2, -3, -11, -21, -24, -29, -30, -31,
driven through `run_sync` against `FakeSlack` with files-mode persistence under `tmp_path`.
Offline and deterministic; no network, no git.
"""

from __future__ import annotations

from pathlib import Path

from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger, load_state
from snipebot.parse import VetoSource
from snipebot.rules import MessageVerdict, PairVerdict, Reason, SelfieClass, Status
from snipebot.sync import Command, SyncResult, _desired_reactions, run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import SEMESTER, make_config, mkts, roster_of

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
OTHER = "U0AAA003"
ADMIN = "U0AAA009"
THIRD = "U0AAA010"
VETO = "no_entry_sign"


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


def _world(now: str) -> FakeSlack:
    users = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in (SNIPER, TARGET, OTHER, ADMIN, THIRD):
        users[uid] = FakeUser(id=uid)
    return FakeSlack(now=now, bot_user_id=BOT, users=users)


def _groups(**over: str | None) -> dict[str, str | None]:
    groups: dict[str, str | None] = {SNIPER: None, TARGET: None, OTHER: None,
                                     ADMIN: None, THIRD: None}
    groups.update(over)
    return groups


def _config(groups: dict[str, str | None] | None = None, **kw):
    return make_config(roster=roster_of(groups or _groups()), admins=(ADMIN,), **kw)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack: FakeSlack, cfg, tmp_path: Path, now: str, detector=None, **kw) -> SyncResult:
    slack.as_of(now)
    led, st = _paths(tmp_path)
    kw.setdefault("no_post", True)
    return run_sync(
        slack, cfg, detector=detector or FakeFaceDetector({}), ledger_path=led,
        state_path=st, now_us=parse_ts(now), **kw,
    )


def _row(tmp_path: Path, ts: str):
    led, _ = _paths(tmp_path)
    return next(r for r in load_ledger(led) if r.ts == ts)


def _verdict_status(tmp_path: Path, ts: str) -> str:
    import json
    led, _ = _paths(tmp_path)
    for line in led.with_name("verdicts.jsonl").read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("ts") == ts:
            return obj["status"]
    raise AssertionError("no verdict for the row")


class _CountingDetector(FakeFaceDetector):
    def __init__(self) -> None:
        super().__init__({})
        self.calls = 0

    def count_faces(self, image_bytes: bytes) -> int:
        self.calls += 1
        return 2


# --- E-W4-2: skin-tone reactions match on the base name ---------------------------------

def test_skin_tone_veto_reaction_counts_as_veto(tmp_path):
    """E-W4-2: an admin's `<veto>::skin-tone-<n>` reaction is a REACTION veto."""
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(1)])
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN,
                name=f"{VETO}::skin-tone-4")
    assert _run(slack, _config(), tmp_path, mkts(2026, 9, 18, 12)).exit_code == 0
    assert [(v.by, v.source) for v in _row(tmp_path, ts).vetoes] == \
        [(ADMIN, VetoSource.REACTION)]
    assert _verdict_status(tmp_path, ts) != "counted"


def test_skin_tone_admin_selfie_reaction_writes_override(tmp_path):
    """E-W4-2: an admin's `selfie::skin-tone-<n>` on a sib-tagged row writes the REACTION
    selfie override, exactly as the plain `selfie` name would."""
    cfg = _config(_groups(**{SNIPER: "fam", TARGET: "fam"}), selfie_bonus=True,
                  selfie_emoji="selfie", max_attempts=0)
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(2)])
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN,
                name="selfie::skin-tone-2")
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12), no_react=True).exit_code == 0
    override = _row(tmp_path, ts).selfie_override
    assert override is not None and override.value is True and override.by == ADMIN
    assert override.source == VetoSource.REACTION


# --- E-W4-3: the opt-out message is read every run ---------------------------------------

def test_optout_message_outside_fetch_range_is_read(tmp_path):
    """E-W4-3: an opt-out message older than the fetch range is still read with
    reactions_get, so a reaction placed on it later opts the reactor out."""
    slack = _world(mkts(2026, 9, 1, 9))
    optout_ts = slack.post(at=mkts(2026, 9, 1, 9), user=ADMIN, channel=CHANNEL,
                           text="react to leave")
    cfg = _config(optout_message_ts=(optout_ts,))
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 11)).exit_code == 0
    slack.react(at=mkts(2026, 9, 25, 11, 30), ts=optout_ts, channel=CHANNEL, user=THIRD,
                name="wave")
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 12)).exit_code == 0
    _, st = _paths(tmp_path)
    assert THIRD in load_state(st).opted_out


def test_unreadable_optout_message_is_skipped_with_warn(tmp_path, capsys):
    """E-W4-3: a configured opt-out message that cannot be read (deleted) is skipped and
    counted in one WARN; the run neither errors nor aborts, and the other message is read."""
    slack = _world(mkts(2026, 9, 18, 9))
    gone = slack.post(at=mkts(2026, 9, 18, 9), user=ADMIN, channel=CHANNEL, text="leave")
    live = slack.post(at=mkts(2026, 9, 18, 9, 1), user=ADMIN, channel=CHANNEL, text="leave")
    slack.react(at=mkts(2026, 9, 18, 9, 30), ts=live, channel=CHANNEL, user=THIRD,
                name="wave")
    slack.delete_message(at=mkts(2026, 9, 18, 9, 40), ts=gone, channel=CHANNEL)
    cfg = _config(optout_message_ts=(gone, live))
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12))
    assert r.exit_code == 0 and r.ledger_written
    _, st = _paths(tmp_path)
    assert THIRD in load_state(st).opted_out
    warns = [ln for ln in capsys.readouterr().err.splitlines() if "optout_unreadable" in ln]
    assert len(warns) == 1 and "WARN" in warns[0] and "skipped=1" in warns[0]


# --- E-W4-24: the opt-out log line carries counts only ------------------------------------

def test_optout_log_line_carries_counts_only(tmp_path, capsys):
    """E-W4-24: `INFO optout observed=<n new> total=<n>`, never an opted-out ID."""
    slack = _world(mkts(2026, 9, 18, 9))
    optout_ts = slack.post(at=mkts(2026, 9, 18, 9), user=ADMIN, channel=CHANNEL,
                           text="react to leave")
    slack.react(at=mkts(2026, 9, 18, 9, 30), ts=optout_ts, channel=CHANNEL, user=THIRD,
                name="wave")
    slack.react(at=mkts(2026, 9, 18, 9, 31), ts=optout_ts, channel=CHANNEL, user=OTHER,
                name="wave")
    cfg = _config(optout_message_ts=(optout_ts,))
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12)).exit_code == 0
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "  optout" in ln]
    assert len(lines) == 1
    assert "observed=2" in lines[0] and "total=2" in lines[0]
    assert THIRD not in lines[0] and OTHER not in lines[0]


# --- E-W4-11: first-inserted rows are observed whatever their age -------------------------

def test_admin_selfie_reaction_on_first_inserted_old_row_is_observed(tmp_path):
    """E-W4-11: a go-live backfill inserts an old sib-tagged row for the first time; the
    admin's selfie reaction on it writes the REACTION override."""
    cfg = _config(_groups(**{SNIPER: "fam", TARGET: "fam"}), selfie_bonus=True,
                  selfie_emoji="selfie", max_attempts=0)
    slack = _world(mkts(2026, 9, 2, 12))
    ts = slack.post(at=mkts(2026, 9, 2, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(3)])
    slack.react(at=mkts(2026, 9, 2, 11), ts=ts, channel=CHANNEL, user=ADMIN, name="selfie")
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 12), command=Command.BACKFILL,
             backfill_from_us=SEMESTER.start_us, no_react=True)
    assert r.exit_code == 0
    override = _row(tmp_path, ts).selfie_override
    assert override is not None and override.by == ADMIN


def test_stored_row_below_scan_floor_keeps_stored_veto_facts(tmp_path):
    """E-W4-11: a row already stored and now below the scan floor keeps its stored reaction
    facts; a veto reaction placed after its scan window is not picked up by a backfill."""
    cfg = _config()
    slack = _world(mkts(2026, 9, 2, 12))
    ts = slack.post(at=mkts(2026, 9, 2, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(4)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 2, 12)).exit_code == 0
    slack.react(at=mkts(2026, 9, 24, 11), ts=ts, channel=CHANNEL, user=ADMIN, name=VETO)
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 25, 12), command=Command.BACKFILL,
             backfill_from_us=SEMESTER.start_us, no_react=True)
    assert r.exit_code == 0
    assert _row(tmp_path, ts).vetoes == ()
    assert _verdict_status(tmp_path, ts) == "counted"


# --- E-W4-21: step 7 never aborts a run -------------------------------------------------

def _snipe_world() -> tuple[FakeSlack, str]:
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(5)])
    slack.post(at=mkts(2026, 9, 18, 10, 30), user=THIRD, channel=CHANNEL,
               text=f"<@{OTHER}>", files=[_photo(6)])
    return slack, ts


def test_reaction_api_error_is_logged_and_skipped(tmp_path, capsys):
    """E-W4-21: a SlackAPIError on one reaction is `WARN reaction_failed ts= error=` and
    skipped; the next message still gets its reaction, the run persists, exit 0."""
    slack, ts = _snipe_world()
    slack.faults.reaction_error(method="reactions_add", error="invalid_name", times=1)
    r = _run(slack, _config(), tmp_path, mkts(2026, 9, 18, 12))
    assert r.exit_code == 0 and r.ledger_written
    assert r.reactions_added == 1
    err = capsys.readouterr().err
    assert any("WARN" in ln and "reaction_failed" in ln and f"ts={ts}" in ln
               and "error=invalid_name" in ln for ln in err.splitlines())


def test_auth_error_ends_step7_with_one_warn(tmp_path, capsys):
    """E-W4-21: an auth error ends step 7 for this run with one WARN; persist still runs,
    exit 0, and reactions converge on a later run."""
    slack, _ = _snipe_world()
    slack.faults.reaction_error(method="reactions_add", error="invalid_auth", times=1)
    r = _run(slack, _config(), tmp_path, mkts(2026, 9, 18, 12))
    assert r.exit_code == 0 and r.ledger_written
    assert r.reactions_added == 0
    stops = [ln for ln in capsys.readouterr().err.splitlines() if "reactions_stopped" in ln]
    assert len(stops) == 1 and "WARN" in stops[0]
    r2 = _run(slack, _config(), tmp_path, mkts(2026, 9, 18, 12, 10))
    assert r2.exit_code == 0 and r2.reactions_added == 2


def test_rate_limited_ends_step7_and_digests_still_run(tmp_path):
    """E-W4-21: RateLimited after the bounded retries ends step 7; step 8 persists and step
    9 still posts its due digest; exit 0."""
    slack, _ = _snipe_world()
    slack.as_of(mkts(2026, 9, 18, 21, 30))
    slack.faults.rate_limit(method="reactions_add", retry_after_seconds=1, times=50)
    r = _run(slack, _config(), tmp_path, mkts(2026, 9, 18, 21, 30), no_post=False)
    assert r.exit_code == 0 and r.ledger_written
    assert r.reactions_added == 0
    assert r.digests_posted == 1


# --- E-W4-29 / E-W4-30: sync._sib_tagged ------------------------------------------------

def test_self_tag_alone_never_fetches_a_photo(tmp_path):
    """E-W4-29: a sender who tags themself (plus a cross-group target) is not sib-tagged,
    so no image is fetched or face-counted."""
    cfg = _config(_groups(**{SNIPER: "fam", OTHER: "red"}), selfie_bonus=True,
                  selfie_emoji="selfie")
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{SNIPER}> <@{OTHER}>", files=[_photo(7)])
    det = _CountingDetector()
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12), detector=det)
    assert r.exit_code == 0
    assert det.calls == 0 and _row(tmp_path, ts).face_counts == {}


def test_opted_out_sib_never_fetches_a_photo(tmp_path):
    """E-W4-30: an opted-out sib target does not make the message sib-tagged: no photo is
    fetched or face-counted, and the admin selfie reaction writes no override."""
    cfg = _config(_groups(**{SNIPER: "fam", TARGET: "fam"}), selfie_bonus=True,
                  selfie_emoji="selfie", seed_opted_out=(TARGET,))
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(8)])
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN, name="selfie")
    det = _CountingDetector()
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12), detector=det)
    assert r.exit_code == 0
    row = _row(tmp_path, ts)
    assert det.calls == 0 and row.face_counts == {} and row.selfie_override is None


def test_selfie_command_on_opted_out_sib_row_exits_2(tmp_path):
    """E-W4-30: `selfie` on a row whose only sib target has opted out is not sib-tagged."""
    cfg = _config(_groups(**{SNIPER: "fam", TARGET: "fam"}), selfie_bonus=True,
                  selfie_emoji="selfie", seed_opted_out=(TARGET,), max_attempts=0)
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}>", files=[_photo(9)])
    r = _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12), command=Command.SELFIE,
             selfie_ts=ts, selfie_value=True, selfie_by=ADMIN)
    assert r.exit_code == 2


# --- E-W4-31: the selfie emoji only on an awarded selfie point --------------------------

def test_selfie_emoji_only_when_a_selfie_point_is_awarded(tmp_path):
    """E-W4-31: a SELFIE-class message whose intra-group pairs are all not counted gets no
    selfie emoji; one with a COUNTED intra-group pair (`PairVerdict.selfie`) does."""
    cfg = _config(_groups(**{SNIPER: "fam", TARGET: "fam"}), selfie_bonus=True,
                  selfie_emoji="selfie", max_attempts=0)
    slack = _world(mkts(2026, 9, 18, 12))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL,
                    text=f"<@{TARGET}> <@{OTHER}>", files=[_photo(10)])
    assert _run(slack, cfg, tmp_path, mkts(2026, 9, 18, 12), no_react=True).exit_code == 0
    row = _row(tmp_path, ts)

    def mv(sib_pair: PairVerdict) -> MessageVerdict:
        plain = PairVerdict(ts=ts, target=OTHER, status=Status.COUNTED,
                            reason=Reason.COUNTED, blocked_by=None, selfie=False)
        return MessageVerdict(ts=ts, status=Status.COUNTED, reason=Reason.COUNTED,
                              selfie=SelfieClass.SELFIE, pairs=(sib_pair, plain))

    blocked = PairVerdict(ts=ts, target=TARGET, status=Status.COOLDOWN,
                          reason=Reason.COOLDOWN, blocked_by=mkts(2026, 9, 18, 9, 55),
                          selfie=False)
    awarded = PairVerdict(ts=ts, target=TARGET, status=Status.COUNTED,
                          reason=Reason.COUNTED, blocked_by=None, selfie=True)
    assert "selfie" not in _desired_reactions(mv(blocked), row, cfg)
    assert "selfie" in _desired_reactions(mv(awarded), row, cfg)
