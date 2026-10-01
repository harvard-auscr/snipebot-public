"""Red-team wave 2 / round 1 — SPEC CONFORMANCE for snipebot/sync.py steps 5-7
(consent observation, face detection, reaction convergence).

Each test targets one break in the shipped code and FAILS on it. Inputs are built
with the FakeSlack authoring API (tests/fake_slack.py) and the shared sync helpers
(tests/_helpers_sync.py). No production or test file other than this one is modified.
"""

from __future__ import annotations

from snipebot.config import VetoActor
from snipebot.faces import FakeFaceDetector
from snipebot.slack_io import MessageNotFound, MissingScope
from snipebot.sync import run_sync
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
)

ADMIN = "U0ADMIN"


def _users(*ids: str) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in ids:
        out[uid] = FakeUser(id=uid)
    return out


def _paths(tmp_path):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack, config, tmp_path, *, now, detector=None, **kw):
    led, st = _paths(tmp_path)
    return run_sync(
        slack, config, detector=detector or FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now), no_post=True, **kw,
    )


def test_optout_reactions_get_message_not_found_tolerated(tmp_path):
    """10 §3 (Slack error table, MessageNotFound / opt-out read): "reactions_get on the
    opt-out message(s) raising MessageNotFound (aged past the 90-day horizon, or deleted)
    is not an error and changes nothing." 20 §5.2 step 2 makes that reactions_get call when
    the opt-out reactions are truncated. `_observe_optouts` calls slack.reactions_get with no
    try/except, so a message_not_found (a delete racing the settle call) propagates out of
    run_sync and crashes the sync instead of being tolerated.
    """
    roster = roster_of({"U0A": "fam", "U0B": "fam", "U0X": "fam"})
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B", "U0X", ADMIN))
    optout_ts = slack.post(at=mkts(2026, 9, 18, 9), user="U0A", channel=CHANNEL,
                           text="react to leave")
    # Two reactors so a limit-1 truncation makes count != len(users) and forces reactions_get.
    slack.react(at=mkts(2026, 9, 18, 10), ts=optout_ts, channel=CHANNEL, user="U0X", name="wave")
    slack.react(at=mkts(2026, 9, 18, 10, 1), ts=optout_ts, channel=CHANNEL, user="U0B", name="wave")
    cfg = make_config(roster=roster, optout_message_ts=(optout_ts,))
    slack.faults.truncate_reaction_users(limit=1)

    orig = slack.reactions_get

    def rg(channel, ts):
        if ts == optout_ts:            # the message vanished between history and the settle call
            raise MessageNotFound("message_not_found")
        return orig(channel, ts)

    slack.reactions_get = rg
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 0            # spec: "not an error and changes nothing"


def test_veto_reactions_get_message_not_found_tolerated(tmp_path):
    """10 §3 (Slack error table): "message_not_found | MessageNotFound | (a) opt-out/veto read
    of the opt-out message(s) ... | no [not fatal]". 20 §5.1 step 1 makes a single
    reactions_get for the veto emoji when its payload user list is truncated. `_reaction_full_users`
    calls slack.reactions_get with no try/except, so a veto read racing a delete
    (message_not_found) propagates and crashes the sync instead of being tolerated.
    """
    roster = roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})
    cfg = make_config(roster=roster, veto_by=(VetoActor.ADMINS,), admins=(ADMIN,))
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B", ADMIN))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"snap")])
    # Two veto reactors so limit-1 truncation forces the reactions_get settle call.
    slack.react(at=mkts(2026, 9, 18, 11), ts=ts, channel=CHANNEL, user=ADMIN, name="no_entry_sign")
    slack.react(at=mkts(2026, 9, 18, 11, 1), ts=ts, channel=CHANNEL, user="U0B", name="no_entry_sign")
    slack.faults.truncate_reaction_users(limit=1)

    orig = slack.reactions_get

    def rg(channel, tsx):
        if tsx == ts:
            raise MessageNotFound("message_not_found")
        return orig(channel, tsx)

    slack.reactions_get = rg
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert r.exit_code == 0


def test_convergence_reactions_get_message_not_found_tolerated(tmp_path):
    """10 §3 (Slack error table): "message_not_found | MessageNotFound | ... (b) reaction
    convergence on a row that vanished mid-run | no [not fatal]". 20 §5.3 settles a truncated
    `observed` set with one reactions_get before add/remove. `_observed_reactions` calls
    slack.reactions_get with no try/except, so if the row vanishes between the history fetch and
    that settle call (message_not_found) the whole run crashes instead of skipping the row.
    """
    roster = roster_of({"U0A": "fam", "U0B": "fam", "U0C": "fam"})
    cfg = make_config(roster=roster, selfie_emoji=None)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B", "U0C"))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"pic")])
    # A non-veto/non-selfie reaction by two users: truncation forces the convergence settle
    # reactions_get, but veto/opt-out/admin-selfie observation never call it here.
    slack.react(at=mkts(2026, 9, 18, 10, 5), ts=ts, channel=CHANNEL, user="U0B", name="tada")
    slack.react(at=mkts(2026, 9, 18, 10, 6), ts=ts, channel=CHANNEL, user="U0C", name="tada")
    slack.faults.truncate_reaction_users(limit=1)

    orig = slack.reactions_get

    def rg(channel, tsx):
        if tsx == ts:
            raise MessageNotFound("message_not_found")
        return orig(channel, tsx)

    slack.reactions_get = rg
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12),
             detector=FakeFaceDetector({sha(b"pic"): 1}))
    assert r.exit_code == 0


def test_faces_after_boundary_not_fired_on_missing_scope(tmp_path, monkeypatch):
    """20 §2.2 (boundary table): "faces:after | after each such fetch + count returns (fact
    written or fault tolerated) | message ts". A MissingScope is neither a written fact nor a
    per-image tolerated fault (it aborts detection for the whole run, 20 §5.2.2), and the
    §5.2.2 pseudocode `break`s out of the loop BEFORE `_boundary("faces:after")` on
    (MissingScope, AuthError). The shipped `_detect_faces` fires `faces:after` on that path
    anyway, emitting an unpaired/extra boundary the crash matrix does not expect.
    """
    from snipebot import sync as _sync

    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster, selfie_bonus=True, selfie_emoji="selfie", max_attempts=3)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"pic")])

    def boom(url):
        raise MissingScope("missing_scope")

    slack.fetch_file_bytes = boom
    recorded: list[str] = []
    monkeypatch.setattr(_sync, "_boundary", lambda name, key=None: recorded.append(name))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    assert "faces:before" in recorded          # the failing fetch was attempted
    assert "faces:after" not in recorded       # spec: break precedes faces:after on MissingScope
