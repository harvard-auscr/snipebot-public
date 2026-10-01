"""Regression tests for the owner decision of 2026-09-30, E-W4-42: `players.mode: auto`
makes the roster automatic. Every non-bot user except USLACKBOT is a player; groups and
their dated `from:` joins still apply to grouped users; `extras` is forbidden; the
`players` fingerprint covers the mode and the grouped entries only; reports, exports and
doctor follow. `listed` (the default) is unchanged.

Offline only: config files and a FakeSlack world under tmp_path, files persistence.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from snipebot import cli, doctor
from snipebot.aggregate import UNGROUPED, eligible_snipes
from snipebot.cli import Exit, main
from snipebot.config import (
    INT_MIN_TS,
    AutoRosterExtrasError,
    FingerprintGuardError,
    InvalidValueError,
    RosterMode,
    compute_fingerprints,
    fingerprint_guard,
    load_config,
)
from snipebot.export import build_all_tables
from snipebot.faces import FakeFaceDetector
from snipebot.parse import Candidate
from snipebot.rules import Reason, Status, evaluate
from snipebot.ts import US_PER_SECOND, parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SIB1 = "U0AAA001"        # grouped
SIB2 = "U0AAA002"        # grouped
LATE = "U0AAA003"        # grouped, joins 2026-09-20
LOOSE = "U0AAA004"       # ungrouped human, in users.list
ROBOT = "U0AAA005"       # an integration account users.list flags as a bot
NEWBIE = "U0AAA006"      # joins the channel between two syncs
STRANGER = "U0AAA007"    # never seen by users.list at all
ADMIN = "U0AAA009"
SLACKBOT = "USLACKBOT"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.000000"


def _us(y, mo, d, h=0, mi=0, s=0) -> int:
    return _secs(y, mo, d, h, mi, s) * US_PER_SECOND


NOW_TS = _ts(2026, 9, 18, 12)


# --------------------------------------------------------------------------- builders

def _config_dict(*, mode: str | None = "auto", groups: dict | None = None,
                 extras: list | None = None, allow_bots: bool = False) -> dict:
    players: dict = {"count_intra_group": True}
    if mode is not None:
        players["mode"] = mode
    players["groups"] = groups if groups is not None else {
        "sib": [SIB1, SIB2, {"id": LATE, "from": "2026-09-20"}]}
    if extras is not None:
        players["extras"] = extras
    return {
        "enabled": True,
        "persistence": "files",
        "slack": {"channel": CHANNEL},
        "timezone": "UTC",
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}],
        "rules": {
            "cooldown": {"minutes": 15, "scope": "pair", "rejected_attempts_reset": False},
            "multi_tag": "per_target",
            "max_targets_per_message": None,
            "edit_grace_minutes": 10,
            "max_snipes_per_target_per_day": None,
            "allow_self": False,
            "allow_bots": allow_bots,
            "count_thread_replies": False,
            "count_image_links": False,
            "allow_video": False,
            "selfie_bonus": False,
        },
        "players": players,
        "consent": {
            "veto": {"emoji": "no_entry_sign", "by": ["admins"]},
            "optout_messages": [],
            "opted_out": [],
        },
        "admins": [ADMIN],
        "feedback": {
            "reactions": {
                "counted": "white_check_mark",
                "cooldown": "hourglass_flowing_sand",
                "untagged": None,
                "not_counted": "x",
                "selfie": None,
            },
            "review": {"min_targets": 5, "emoji": "question"},
        },
        "reports": [],
    }


def _write(path: Path, **kw) -> Path:
    path.write_text(yaml.safe_dump(_config_dict(**kw), sort_keys=False), encoding="utf-8")
    return path


def _load(tmp_path: Path, is_bot: dict | None = None, name: str = "config.yaml", **kw):
    return load_config(_write(tmp_path / name, **kw), is_bot=is_bot)


def _cand(ts: str, sender: str, *targets: str) -> Candidate:
    return Candidate(
        ts=ts, sender=sender, subtype=None, thread_ts=None, targets=tuple(targets),
        live_images=1, live_image_ids=(f"F{ts}",), live_videos=0, linked_images=0,
        last_edit_ts=None, file_sigs=(), vetoes=(), missing_runs=0,
        first_seen_targets=frozenset(targets), first_sight_edited=False,
        target_edited_in=(),
    )


def _judge(config, facts, opted_out=frozenset()):
    return {mv.ts: mv for mv in evaluate(facts, config.rules, config.roster, opted_out,
                                         config.semesters, config.tz)}


BOTS_MAP = {BOT: True, ROBOT: True, SIB1: False, SIB2: False, LATE: False,
            LOOSE: False, NEWBIE: False, ADMIN: False}


# --------------------------------------------------------------------------- config

def test_mode_defaults_to_listed_and_accepts_both_values(tmp_path):
    assert _load(tmp_path, mode=None).roster.mode is RosterMode.LISTED
    assert _load(tmp_path, mode="listed").roster.mode is RosterMode.LISTED
    auto = _load(tmp_path, mode="auto")
    assert auto.roster.mode is RosterMode.AUTO
    assert set(auto.roster.entries) == {SIB1, SIB2, LATE}


@pytest.mark.parametrize("bad", ["Auto", "everyone", "", 1, True, None, ["auto"]])
def test_mode_rejects_anything_else(tmp_path, bad):
    doc = _config_dict(mode="auto")
    doc["players"]["mode"] = bad
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with pytest.raises(InvalidValueError, match="players.mode"):
        load_config(path)


def test_extras_forbidden_under_auto(tmp_path):
    with pytest.raises(AutoRosterExtrasError, match="players.extras"):
        _load(tmp_path, mode="auto", extras=[LOOSE])
    # AutoRosterExtrasError is an InvalidValueError, so `config invalid` (exit 2) as usual
    assert issubclass(AutoRosterExtrasError, InvalidValueError)


def test_empty_or_absent_extras_and_no_groups_are_fine_under_auto(tmp_path):
    assert _load(tmp_path, mode="auto", extras=[]).roster.mode is RosterMode.AUTO
    bare = _load(tmp_path, mode="auto", groups={})
    assert bare.roster.entries == {}
    assert bare.roster.is_member_at(LOOSE, _us(2026, 9, 18))


def test_extras_still_fine_under_listed(tmp_path):
    cfg = _load(tmp_path, mode="listed", extras=[LOOSE])
    assert cfg.roster.group_of(LOOSE) is None and LOOSE in cfg.roster.entries


def test_cli_reports_extras_under_auto_as_config_invalid(tmp_path, capsys):
    cfg = _write(tmp_path / "config.yaml", mode="auto", extras=[LOOSE])
    data = tmp_path / "data"
    data.mkdir()
    rc = main(["report", "--by", "person", "--config", str(cfg), "--data-dir", str(data)])
    assert rc == int(Exit.CONFIG_INVALID)
    assert "players.extras" in capsys.readouterr().err


# --------------------------------------------------------------------------- evaluate

def test_unlisted_and_unknown_humans_play_under_auto(tmp_path):
    cfg = _load(tmp_path, BOTS_MAP)
    t1, t2, t3 = _ts(2026, 9, 18, 9), _ts(2026, 9, 18, 10), _ts(2026, 9, 18, 11)
    got = _judge(cfg, [_cand(t1, LOOSE, SIB1), _cand(t2, STRANGER, LOOSE),
                       _cand(t3, SIB2, STRANGER)])
    for ts in (t1, t2, t3):
        assert got[ts].status is Status.COUNTED, got[ts]


def test_the_same_snipes_are_off_roster_under_listed(tmp_path):
    cfg = _load(tmp_path, BOTS_MAP, mode="listed")
    t1, t2 = _ts(2026, 9, 18, 9), _ts(2026, 9, 18, 10)
    got = _judge(cfg, [_cand(t1, LOOSE, SIB1), _cand(t2, SIB2, STRANGER)])
    assert got[t1].reason is Reason.SENDER_OFF_ROSTER
    assert got[t2].pairs[0].reason is Reason.TARGET_OFF_ROSTER


@pytest.mark.parametrize("allow_bots", [False, True])
def test_bot_target_is_off_roster_like_an_unlisted_user(tmp_path, allow_bots):
    cfg = _load(tmp_path, BOTS_MAP, allow_bots=allow_bots)
    ts = _ts(2026, 9, 18, 9)
    mv = _judge(cfg, [_cand(ts, LOOSE, ROBOT, SIB1)])[ts]
    reasons = {p.target: p.reason for p in mv.pairs}
    # TARGET_OFF_ROSTER keeps its place ahead of TARGET_IS_BOT; allow_bots never rescues it
    assert reasons == {ROBOT: Reason.TARGET_OFF_ROSTER, SIB1: Reason.COUNTED}


def test_bot_sender_is_off_roster(tmp_path):
    cfg = _load(tmp_path, BOTS_MAP)
    ts = _ts(2026, 9, 18, 9)
    assert _judge(cfg, [_cand(ts, ROBOT, SIB1)])[ts].reason is Reason.SENDER_OFF_ROSTER


def test_a_grouped_bot_is_off_roster_too(tmp_path):
    cfg = _load(tmp_path, {**BOTS_MAP, SIB2: True}, allow_bots=True)
    ts = _ts(2026, 9, 18, 9)
    mv = _judge(cfg, [_cand(ts, SIB1, SIB2)])[ts]
    assert mv.pairs[0].reason is Reason.TARGET_OFF_ROSTER


def test_slackbot_is_never_a_player(tmp_path):
    cfg = _load(tmp_path, {})  # not flagged by any map, and still never a player
    t1, t2 = _ts(2026, 9, 18, 9), _ts(2026, 9, 18, 10)
    got = _judge(cfg, [_cand(t1, SLACKBOT, SIB1), _cand(t2, SIB1, SLACKBOT)])
    assert got[t1].reason is Reason.SENDER_OFF_ROSTER
    assert got[t2].pairs[0].reason is Reason.TARGET_OFF_ROSTER


def test_grouped_dated_join_dates_the_group_only(tmp_path):
    # a human plays at every ts under auto; `from:` dates only their group membership,
    # so before it they play ungrouped and are never off-roster
    cfg = _load(tmp_path, BOTS_MAP)
    before_s, before_t = _ts(2026, 9, 18, 9), _ts(2026, 9, 18, 10)
    after_s, after_t = _ts(2026, 9, 21, 9), _ts(2026, 9, 21, 10)
    got = _judge(cfg, [_cand(before_s, LATE, LOOSE), _cand(before_t, LOOSE, LATE),
                       _cand(after_s, LATE, LOOSE), _cand(after_t, LOOSE, LATE)])
    for ts in (before_s, before_t, after_s, after_t):
        assert got[ts].status is Status.COUNTED, ts
    # the group itself is dated: LATE -> SIB1 is cross-group (not sib-tagged) before
    # the join and intra-group after it
    assert cfg.roster.group_of(LATE, _us(2026, 9, 18)) is None
    assert cfg.roster.group_of(LATE, _us(2026, 9, 21)) == "sib"
    assert cfg.roster.group_of(LATE) == "sib"
    # an ungrouped player has no join date: counted on the first semester day
    first = _ts(2026, 9, 1, 0, 0, 1)
    assert _judge(cfg, [_cand(first, NEWBIE, LOOSE)])[first].status is Status.COUNTED


def test_opt_out_still_wins_under_auto(tmp_path):
    cfg = _load(tmp_path, BOTS_MAP)
    t1, t2 = _ts(2026, 9, 18, 9), _ts(2026, 9, 18, 10)
    got = _judge(cfg, [_cand(t1, STRANGER, SIB1), _cand(t2, SIB1, STRANGER)],
                 opted_out=frozenset({STRANGER}))
    assert got[t1].reason is Reason.SENDER_OPTED_OUT
    assert got[t2].pairs[0].reason is Reason.TARGET_OPTED_OUT


def test_self_snipe_and_cooldown_unchanged_under_auto(tmp_path):
    cfg = _load(tmp_path, BOTS_MAP)
    t1, t2, t3 = _ts(2026, 9, 18, 9), _ts(2026, 9, 18, 9, 5), _ts(2026, 9, 18, 9, 10)
    got = _judge(cfg, [_cand(t1, LOOSE, LOOSE), _cand(t2, LOOSE, STRANGER),
                       _cand(t3, LOOSE, STRANGER)])
    assert got[t1].pairs[0].reason is Reason.SELF_SNIPE
    assert got[t2].status is Status.COUNTED
    assert got[t3].status is Status.COOLDOWN


# --------------------------------------------------------------------------- fingerprints

H = _us(2026, 9, 25)


def test_auto_fingerprint_ignores_the_bot_map_and_new_people(tmp_path):
    base = compute_fingerprints(_load(tmp_path, BOTS_MAP), H)
    for is_bot in (None, {}, {**BOTS_MAP, LOOSE: True}, {**BOTS_MAP, NEWBIE: True,
                   STRANGER: False, "U0AAA008": True}, {**BOTS_MAP, SIB1: True}):
        assert compute_fingerprints(_load(tmp_path, is_bot), H) == base, is_bot


def test_switching_modes_changes_the_players_fingerprint(tmp_path):
    auto = compute_fingerprints(_load(tmp_path, BOTS_MAP, mode="auto"), H)
    listed = compute_fingerprints(_load(tmp_path, BOTS_MAP, mode="listed"), H)
    assert auto["players"] != listed["players"]
    assert auto["groups"] == listed["groups"]
    assert auto["rules"] == listed["rules"] and auto["semesters"] == listed["semesters"]
    # even with no groups at all, and on an empty H
    for h in (H, None):
        a = compute_fingerprints(_load(tmp_path, mode="auto", groups={}), h)
        b = compute_fingerprints(_load(tmp_path, mode="listed", groups={}), h)
        assert a["players"] != b["players"]


def test_switching_modes_trips_the_guard(tmp_path):
    listed = _load(tmp_path, BOTS_MAP, mode="listed")
    stored = compute_fingerprints(listed, H)
    with pytest.raises(FingerprintGuardError, match="players"):
        fingerprint_guard(_load(tmp_path, BOTS_MAP, mode="auto"), [H], stored, H)
    stored_auto = compute_fingerprints(_load(tmp_path, BOTS_MAP, mode="auto"), H)
    with pytest.raises(FingerprintGuardError, match="sync --reevaluate"):
        fingerprint_guard(listed, [H], stored_auto, H)


def test_auto_fingerprint_still_tracks_grouped_entries(tmp_path):
    base = compute_fingerprints(_load(tmp_path, BOTS_MAP), H)["players"]
    regrouped = compute_fingerprints(_load(tmp_path, BOTS_MAP, groups={
        "sib": [SIB1, {"id": LATE, "from": "2026-09-20"}], "other": [SIB2]}), H)["players"]
    moved = compute_fingerprints(_load(tmp_path, BOTS_MAP, groups={
        "sib": [SIB1, SIB2, {"id": LATE, "from": "2026-09-19"}]}), H)["players"]
    future = compute_fingerprints(_load(tmp_path, BOTS_MAP, groups={
        "sib": [SIB1, SIB2, {"id": LATE, "from": "2026-09-20"},
                {"id": LOOSE, "from": "2026-10-01"}]}), H)["players"]
    assert regrouped != base and moved != base
    assert future == base   # a grouped addition dated after H is excluded, as under listed


def test_listed_fingerprint_is_the_old_shape_byte_for_byte(tmp_path):
    cfg = _load(tmp_path, BOTS_MAP, mode="listed", extras=[LOOSE])
    old = sorted(
        ({"user": e.user, "join_us": e.join_us, "group": e.group, "is_bot": e.is_bot}
         for e in cfg.roster.entries.values() if e.join_us <= H),
        key=lambda o: o["user"])
    want = hashlib.sha256(json.dumps(old, ensure_ascii=True, separators=(",", ":"))
                          .encode("ascii")).hexdigest()
    assert compute_fingerprints(cfg, H)["players"] == want
    assert compute_fingerprints(_load(tmp_path, BOTS_MAP, mode=None, extras=[LOOSE]),
                                H)["players"] == want


# --------------------------------------------------------------------------- CLI end to end

def _users(*, newbie: bool) -> dict:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SIB1: FakeUser(id=SIB1, display_name="user-1"),
        SIB2: FakeUser(id=SIB2, display_name="user-2"),
        LATE: FakeUser(id=LATE, display_name="user-3"),
        LOOSE: FakeUser(id=LOOSE, display_name="user-4"),
        ROBOT: FakeUser(id=ROBOT, display_name="user-5", is_bot=True),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9"),
    }
    if newbie:
        users[NEWBIE] = FakeUser(id=NEWBIE, display_name="user-6")
    return users


class _Clock:
    def __init__(self, ts: str) -> None:
        self.us = parse_ts(ts)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(NOW_TS)
    monkeypatch.setattr(cli, "_now_us", lambda: c.us)
    return c


_FILE_N = [0]


def _photo(slack: FakeSlack, ts: str, sender: str, *targets: str) -> None:
    _FILE_N[0] += 1
    n = _FILE_N[0]
    slack.post(at=ts, user=sender, channel=CHANNEL,
               text=" ".join(f"<@{t}>" for t in targets),
               files=[image_file(f"F0FILE{n:03d}", b"snap%d" % n, name=f"photo-{n}.png")])


def _cli(slack: FakeSlack, *argv: str) -> int:
    return main(list(argv), slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({}))


def _argv(cfg: Path, data: Path) -> list[str]:
    return ["--config", str(cfg), "--data-dir", str(data)]


def _verdicts(data: Path) -> dict[str, dict]:
    rows = [json.loads(line) for line in
            (data / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()]
    return {r["ts"]: r for r in rows}


S1 = _ts(2026, 9, 18, 9)     # LOOSE (ungrouped) snipes SIB1
S2 = _ts(2026, 9, 18, 10)    # SIB1 snipes ROBOT (bot) and LOOSE
S3 = _ts(2026, 9, 18, 11)    # SIB2 snipes STRANGER (absent from users.list)
S4 = _ts(2026, 9, 19, 9)     # NEWBIE, new to the channel, snipes SIB2


def _synced_world(tmp_path: Path, clock: _Clock) -> tuple[Path, Path, FakeSlack]:
    cfg = _write(tmp_path / "config.yaml", mode="auto")
    data = tmp_path / "data"
    data.mkdir()
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=_users(newbie=False))
    _photo(slack, S1, LOOSE, SIB1)
    _photo(slack, S2, SIB1, ROBOT, LOOSE)
    _photo(slack, S3, SIB2, STRANGER)
    assert _cli(slack, "sync", "--no-react", "--no-post", *_argv(cfg, data)) == 0
    return cfg, data, slack


def test_sync_under_auto_counts_a_newly_joined_poster_without_a_config_change(
        tmp_path, clock):
    cfg, data, slack = _synced_world(tmp_path, clock)
    v = _verdicts(data)
    assert v[S1]["status"] == "counted"
    assert {p["target"]: p["reason"] for p in v[S2]["pairs"]} == {
        ROBOT: "target_off_roster", LOOSE: "counted"}
    assert v[S3]["status"] == "counted"
    config_before = cfg.read_bytes()
    players_fp = json.loads((data / "state.json").read_text(encoding="utf-8"))[
        "fingerprints"]["players"]

    # someone new joins the workspace and the channel, and posts a snipe
    slack.users[NEWBIE] = FakeUser(id=NEWBIE, display_name="user-6")
    later = _ts(2026, 9, 19, 12)
    slack._now = later
    clock.us = parse_ts(later)
    _photo(slack, S4, NEWBIE, SIB2)
    assert _cli(slack, "sync", "--no-react", "--no-post", *_argv(cfg, data)) == 0

    assert cfg.read_bytes() == config_before
    v = _verdicts(data)
    assert v[S4]["status"] == "counted" and v[S4]["pairs"][0]["target"] == SIB2
    state = json.loads((data / "state.json").read_text(encoding="utf-8"))
    assert state["fingerprints"]["players"] == players_fp
    # the users cache now carries the bot map whole, so report/export judge as sync did
    cache = json.loads((data / "users.json").read_text(encoding="utf-8"))
    assert cache[ROBOT]["is_bot"] is True and cache[BOT]["is_bot"] is True
    assert cache[NEWBIE]["is_bot"] is False


def test_switching_a_synced_ledger_to_listed_exits_3(tmp_path, clock, capsys):
    cfg, data, slack = _synced_world(tmp_path, clock)
    _write(cfg, mode="listed", extras=[LOOSE])
    capsys.readouterr()
    rc = _cli(slack, "sync", "--no-react", "--no-post", *_argv(cfg, data))
    assert rc == int(Exit.GUARD_REFUSED)
    assert "players" in capsys.readouterr().err


def _named(slack: FakeSlack, cfg: Path, data: Path) -> None:
    """`roster` refreshes the users.json names (40 §4.2) and, under auto, the whole bot map."""
    assert _cli(slack, "roster", *_argv(cfg, data)) == 0
    cache = json.loads((data / "users.json").read_text(encoding="utf-8"))
    assert cache[ROBOT] == {"is_bot": True}
    assert cache[LOOSE] == {"display_name": "user-4", "is_bot": False}


def test_report_people_table_under_auto(tmp_path, clock, capsys):
    cfg, data, slack = _synced_world(tmp_path, clock)
    _named(slack, cfg, data)
    capsys.readouterr()
    assert main(["report", "--by", "person", *_argv(cfg, data)]) == int(Exit.OK)
    out = capsys.readouterr().out
    for name in ("user-1", "user-2", "user-4", f"[{STRANGER}]"):
        assert name in out, out
    # a bot never appears; grouped LATE has no activity and is not listed
    assert "user-5" not in out and "user-3" not in out, out


def test_export_people_and_groups_under_auto(tmp_path, clock):
    cfg, data, slack = _synced_world(tmp_path, clock)
    _named(slack, cfg, data)
    out_dir = tmp_path / "out"
    assert main(["export", "--out", str(out_dir), *_argv(cfg, data)]) == int(Exit.OK)
    with open(out_dir / "fall-2026_people.csv", encoding="utf-8-sig", newline="") as fh:
        people = {row["person"]: row for row in csv.DictReader(fh)}
    assert set(people) == {"user-1", "user-2", "user-4", f"[{STRANGER}]"}, people
    assert people["user-4"]["group"] == UNGROUPED
    assert people["user-1"]["group"] == "sib"
    with open(out_dir / "fall-2026_groups.csv", encoding="utf-8-sig", newline="") as fh:
        groups = {row["group"]: row for row in csv.DictReader(fh)}
    assert groups["sib"]["members"] == "3"            # grouped members, as today
    assert groups[UNGROUPED]["members"] == "2"        # LOOSE and STRANGER were active


def test_groups_table_ungrouped_members_are_the_active_ungrouped_people(tmp_path):
    cfg = _load(tmp_path, BOTS_MAP)
    facts = [_cand(_ts(2026, 9, 18, 9), LOOSE, SIB1),
             _cand(_ts(2026, 9, 18, 10), SIB1, STRANGER),
             _cand(_ts(2026, 9, 18, 11), NEWBIE, ROBOT)]       # bot target: no snipe
    sem = cfg.semesters[0]
    elig = eligible_snipes(facts, cfg.rules, cfg.roster, frozenset(), cfg.semesters,
                           cfg.tz, sem)
    tables = build_all_tables(elig, cfg.roster, frozenset())
    groups = {r.group: r for r in tables["groups"]}
    assert groups[UNGROUPED].members == 2               # NEWBIE made nothing that counted
    assert groups["sib"].members == 3
    assert {r.person for r in tables["people"]} == {LOOSE, SIB1, STRANGER}
    # nobody active ungrouped: the (ungrouped) row is absent, as with no extras today
    elig2 = eligible_snipes(facts[:0], cfg.rules, cfg.roster, frozenset(), cfg.semesters,
                            cfg.tz, sem)
    assert UNGROUPED not in {r.group for r in build_all_tables(
        elig2, cfg.roster, frozenset())["groups"]}


# --------------------------------------------------------------------------- doctor

def _doctor_args(cfg: Path, data: Path, *, offline: bool, cache=None):
    return argparse.Namespace(config=str(cfg), data_dir=str(data), offline=offline,
                              json=True, is_bot_cache=cache)


def _checks(capsys) -> dict[str, dict]:
    out = capsys.readouterr().out
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return {r["id"]: r for r in rows}


def test_doctor_roster_checks_cover_grouped_members_only_under_auto(tmp_path):
    cfg = _load(tmp_path, BOTS_MAP)
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=_users(newbie=False),
                      channel_members={CHANNEL: [BOT, SIB1, SIB2, LATE]})
    # LOOSE and STRANGER are neither listed nor checked; the grouped three resolve
    assert doctor._check_roster_resolve(slack, cfg).ok
    slack.channel_members[CHANNEL].remove(LATE)
    assert not doctor._check_roster_resolve(slack, cfg).ok
    # no groups: passes vacuously
    bare = _load(tmp_path, BOTS_MAP, groups={})
    assert doctor._check_roster_resolve(slack, bare).ok
    assert doctor._check_roster_bot(slack, bare, _us(2026, 9, 18)).ok
    # a grouped bot warns even with allow_bots (it is off-roster under auto)
    slack.users[SIB2] = FakeUser(id=SIB2, display_name="user-2", is_bot=True)
    allow = _load(tmp_path, BOTS_MAP, allow_bots=True)
    assert not doctor._check_roster_bot(slack, allow, _us(2026, 9, 18)).ok
    # ...while listed keeps its allow_bots pass
    listed = _load(tmp_path, BOTS_MAP, mode="listed", allow_bots=True)
    assert doctor._check_roster_bot(slack, listed, _us(2026, 9, 18)).ok


def test_online_doctor_under_auto_agrees_with_sync(tmp_path, clock, capsys):
    cfg, data, slack = _synced_world(tmp_path, clock)
    capsys.readouterr()
    doctor.run(_doctor_args(cfg, data, offline=False), slack_factory=lambda: slack,
               now_us=clock.us)
    checks = _checks(capsys)
    for cid in ("DOC-CONFIG-PARSE", "DOC-VERDICTS-FRESH", "DOC-LEDGER-INTEGRITY",
                "DOC-FINGERPRINT-PLAYERS", "DOC-ROSTER-BOT"):
        assert checks[cid]["ok"], checks[cid]


def test_offline_doctor_under_auto_judges_bots_from_the_users_cache(tmp_path, clock, capsys):
    cfg, data, _slack = _synced_world(tmp_path, clock)
    cache = cli._cached_is_bot(argparse.Namespace(data_dir=str(data)))
    assert cache.get(ROBOT) is True
    capsys.readouterr()
    doctor.run(_doctor_args(cfg, data, offline=True, cache=cache), now_us=clock.us)
    checks = _checks(capsys)
    assert checks["DOC-VERDICTS-FRESH"]["ok"], checks["DOC-VERDICTS-FRESH"]
    assert checks["DOC-FINGERPRINT-PLAYERS"]["ok"]
    # without the cache the bot target would count and the verdicts look stale
    doctor.run(_doctor_args(cfg, data, offline=True, cache=None), now_us=clock.us)
    assert not _checks(capsys)["DOC-VERDICTS-FRESH"]["ok"]


def test_auto_roster_bots_hold_only_the_map_bots(tmp_path):
    roster = _load(tmp_path, BOTS_MAP).roster
    assert roster.bots == frozenset({BOT, ROBOT})
    assert _load(tmp_path, None).roster.bots == frozenset()
    assert _load(tmp_path, BOTS_MAP, mode="listed").roster.bots == frozenset()
    assert roster.entries[LATE].join_us != INT_MIN_TS
