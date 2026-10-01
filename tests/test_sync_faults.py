"""L3-FA fault tolerance on the sync surface (50 §2.3): how `run_sync` tolerates what a
compliant `SlackIO` surfaces — a failed fetch aborts writing nothing, reaction faults are
absorbed, delete inference survives a transient vanish and respects the horizon, and every
per-image face fetch fault leaves the image uncounted without aborting the run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snipebot import sync
from snipebot.faces import FakeFaceDetector
from snipebot.slack_io import (
    RateLimited,
    SlackAPIError,
    SlackHTTPError,
    SlackPaginationError,
    SlackTransportError,
)
from snipebot.sync import Command, run_sync
from snipebot.ledger import load_ledger
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import BOT, CHANNEL, image_file, make_config, mkts, roster_of, sha

ADMIN = "U0ADMIN"


def _users(*ids: str) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in ids:
        out[uid] = FakeUser(id=uid)
    return out


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack, config, tmp_path, *, now, detector=None, **kw):
    led, st = _paths(tmp_path)
    return run_sync(
        slack, config, detector=detector or FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now), no_post=True, **kw,
    )


def _fam():
    return roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})


def _snipe(slack, at, *, fid="F01", data=b"pic"):
    return slack.post(at=at, user="U0A", channel=CHANNEL, text="<@U0B>",
                      files=[image_file(fid, data)])


# --- fetch aborts write nothing (exit 5) -------------------------------------

@pytest.mark.parametrize("exc", [
    SlackAPIError("internal_error"),
    SlackPaginationError("repeated cursor"),
    SlackHTTPError(500),
    SlackTransportError("no response"),
    RateLimited(1, "history"),
])
def test_history_error_aborts_with_exit_5(tmp_path, monkeypatch, exc):
    cfg = make_config(roster=_fam())
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    _snipe(slack, mkts(2026, 9, 18, 10))

    def boom(channel, oldest, latest=None):
        raise exc

    monkeypatch.setattr(slack, "history", boom)
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 5 and not r.ledger_written
    led, st = _paths(tmp_path)
    assert not led.exists() and not st.exists()


def test_fail_mid_page_aborts(tmp_path):
    cfg = make_config(roster=_fam())
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    _snipe(slack, mkts(2026, 9, 18, 10))
    slack.faults.empty_page_with_more(times=1)          # force a second page ...
    slack.faults.fail_mid_page(after_pages=1, error="internal_error")  # ... which fails
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 5 and not r.ledger_written


def test_history_rate_limit_exhausted_aborts(tmp_path):
    cfg = make_config(roster=_fam())
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    _snipe(slack, mkts(2026, 9, 18, 10))
    slack.faults.rate_limit(method="history", retry_after_seconds=1, times=99)  # over cap
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 5 and not r.ledger_written


# --- delete inference: vanish + horizon --------------------------------------

def test_vanish_then_reappear_resets_missing_runs(tmp_path):
    cfg = make_config(roster=_fam())
    slack = FakeSlack(now=mkts(2026, 9, 18, 9), bot_user_id=BOT, users=_users("U0A", "U0B"))
    ts = _snipe(slack, mkts(2026, 9, 18, 8))
    # A text-only survivor keeps later fetches non-empty (zero returned infers no miss, E-W4-16).
    slack.post(at=mkts(2026, 9, 18, 8, 30), user="U0A", channel=CHANNEL, text="hello")
    slack.as_of(mkts(2026, 9, 18, 9))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 9))

    slack.faults.vanish(ts=ts, for_fetches=1)            # one transient miss
    slack.as_of(mkts(2026, 9, 18, 10))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 10))
    led, _ = _paths(tmp_path)
    assert load_ledger(led)[0].missing_runs == 1

    slack.as_of(mkts(2026, 9, 18, 11))                   # reappears
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 11))
    row = load_ledger(led)[0]
    assert row.missing_runs == 0 and not row.deleted


def test_no_delete_inferred_past_horizon(tmp_path):
    cfg = make_config(roster=_fam(), history_horizon_days=90, scan_days=14)
    slack = FakeSlack(now=mkts(2026, 6, 1, 9), bot_user_id=BOT, users=_users("U0A", "U0B"),
                      horizon_days=90)
    old = _snipe(slack, mkts(2026, 6, 1, 8))
    slack.as_of(mkts(2026, 6, 1, 9))
    _run(slack, cfg, tmp_path, now=mkts(2026, 6, 1, 9))  # store the old row

    # far in the future: the old message has aged past the 90-day horizon, so it is neither
    # returned nor inferred deleted.
    slack.as_of(mkts(2026, 9, 30, 12))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 30, 12))
    led, _ = _paths(tmp_path)
    row = next(r for r in load_ledger(led) if r.ts == old)
    assert row.missing_runs == 0 and not row.deleted


def test_unreadable_optout_is_no_error(tmp_path):
    # an opt-out message ts that is not in the fetched history -> do nothing, do not error.
    cfg = make_config(roster=_fam(), optout_message_ts=("1000000000.000000",))
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    _snipe(slack, mkts(2026, 9, 18, 10))
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 0
    _, st = _paths(tmp_path)
    import json
    assert json.loads(st.read_text())["opted_out"] == {}


# --- reaction faults are tolerated -------------------------------------------

@pytest.mark.parametrize("method,error", [
    ("reactions_add", "already_reacted"),
    ("reactions_add", "message_not_found"),
    ("reactions_remove", "no_reaction"),
    ("reactions_remove", "message_not_found"),
])
def test_reaction_faults_tolerated(tmp_path, method, error):
    cfg = make_config(roster=_fam())
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    ts = _snipe(slack, mkts(2026, 9, 18, 10))
    if method == "reactions_remove":
        # a stale bot reaction to remove; the remove call hits the injected fault.
        slack.react(at=mkts(2026, 9, 18, 10, 5), ts=ts, channel=CHANNEL, user=BOT, name="x")
    slack.faults.reaction_error(method=method, error=error, times=5)
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 0 and r.ledger_written  # the untolerated-error path is not taken


def test_truncated_users_settles_with_reactions_get(tmp_path, monkeypatch):
    cfg = make_config(roster=_fam())
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    ts = _snipe(slack, mkts(2026, 9, 18, 10))
    # the bot already placed the counted emoji; a truncation hides the bot from history.
    slack.react(at=mkts(2026, 9, 18, 10, 5), ts=ts, channel=CHANNEL, user=BOT,
                name="white_check_mark")
    slack.faults.truncate_reaction_users(limit=0, drop_bot=True)

    calls = {"n": 0}
    real_get = slack.reactions_get

    def counting_get(channel, t):
        calls["n"] += 1
        return real_get(channel, t)

    monkeypatch.setattr(slack, "reactions_get", counting_get)
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 0
    assert calls["n"] >= 1  # one reactions_get settled the truncated presence
    final = real_get(CHANNEL, ts)
    marks = [rr for rr in final["reactions"] if rr["name"] == "white_check_mark"]
    assert marks and marks[0]["users"].count(BOT) == 1  # not double-added


def test_step5_reactions_get_rate_limited_exits_5(tmp_path):
    # An untolerated SlackError raised by a step-5 reactions_get (a truncated veto reaction
    # forcing the settle call, which then exhausts its rate-limit budget) fails the run
    # closed exactly like a step-2 fetch failure: exit 5, nothing persisted (E6).
    cfg = make_config(roster=_fam())
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B", ADMIN))
    ts = _snipe(slack, mkts(2026, 9, 18, 10))
    # an admin veto reaction, hidden behind a truncated user list so step 5 must settle it
    slack.react(at=mkts(2026, 9, 18, 10, 5), ts=ts, channel=CHANNEL, user=ADMIN,
                name="no_entry_sign")
    slack.faults.truncate_reaction_users(limit=0)
    slack.faults.rate_limit(method="reactions_get", retry_after_seconds=1, times=99)  # over cap
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 5 and not r.ledger_written
    led, st = _paths(tmp_path)
    assert not led.exists() and not st.exists()


# --- per-image face fetch faults (no count, run continues) -------------------

@pytest.mark.parametrize("arm", ["timeout", "429", "oversize"])
def test_face_fetch_fault_no_count_run_continues(tmp_path, arm):
    cfg = make_config(roster=_fam(), selfie_bonus=True, selfie_emoji="selfie", max_attempts=3)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    _snipe(slack, mkts(2026, 9, 18, 10))
    {
        "timeout": lambda: slack.faults.fetch_timeout(times=1),
        "429": lambda: slack.faults.fetch_429(retry_after_seconds=1, times=1),
        "oversize": lambda: slack.faults.fetch_oversize(times=1, limit_bytes=1),
    }[arm]()
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12),
             detector=FakeFaceDetector({sha(b"pic"): 1}))
    assert r.exit_code == 0
    row = load_ledger(_paths(tmp_path)[0])[0]
    assert row.face_counts == {} and row.detect_attempts == 1  # no count, one attempt burned


def test_face_fetch_fault_then_success_next_run_counts(tmp_path):
    cfg = make_config(roster=_fam(), selfie_bonus=True, selfie_emoji="selfie", max_attempts=3)
    slack = FakeSlack(now=mkts(2026, 9, 18, 9), bot_user_id=BOT, users=_users("U0A", "U0B"))
    _snipe(slack, mkts(2026, 9, 18, 8), data=b"face2")
    det = FakeFaceDetector({sha(b"face2"): 2})

    slack.faults.fetch_timeout(times=1)
    slack.as_of(mkts(2026, 9, 18, 9))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 9), detector=det)
    led, _ = _paths(tmp_path)
    assert load_ledger(led)[0].face_counts == {}  # first run failed the fetch

    slack.as_of(mkts(2026, 9, 18, 10))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 10), detector=det)
    row = load_ledger(led)[0]
    assert row.face_counts == {"F01": 2}  # the id is retried and counted the next run
