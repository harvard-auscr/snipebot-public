"""Red-team wave 2, round 1 (SPEC CONFORMANCE) against snipebot/sync.py steps 8-10,
`post_digests`, and how sync drives persistence.py / ledger.py.

Each test reproduces one spec violation and FAILS on the current code. Inputs are built
with the FakeSlack authoring API and tests/_helpers_sync.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from snipebot import sync
from snipebot.aggregate import eligible_snipes
from snipebot.config import Cadence, ReportSpec, Section
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger
from snipebot.persistence import CommitResult
from snipebot.report import NameResolver, render_digest
from snipebot.sync import run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import (
    BOT,
    CHANNEL,
    data_paths,
    image_file,
    make_config,
    mkts,
    roster_of,
)

ADMIN = "U0ADMIN"


def _users(*ids: str) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in ids:
        out[uid] = FakeUser(id=uid, display_name=uid)
    return out


def _digest_posts(slack) -> list[dict]:
    return [
        e for e in slack._events
        if e["kind"] == "post"
        and (e["data"].get("metadata") or {}).get("event_type") == "snipe_digest"
    ]


# --------------------------------------------------------------------------- Finding A


def test_commit_message_omits_by_reason_deltas(tmp_path, monkeypatch):
    """20 §8.4 (commit message format): the body is "verdict deltas by `Status`/`Reason`
    and the row add/drop counts, relative to the cumulative baseline", laid out as

        rows +<added> -<dropped>
        counted +<Δ>
        cooldown +<Δ>
        ...

    A run that turns one message into a COUNTED snipe moves `counted` by +1, so §8.4
    requires a `counted +1` body line in the commit message `sync` hands to
    `store.commit_and_push`. The shipped `sync._commit_message` emits only the header and
    the `rows +A -B` line, so no per-reason delta ever reaches git (and, downstream,
    `_moved_pairs_from_message` reads 0 for every real sync commit).
    """
    captured: dict[str, str] = {}

    class _SpyStore:
        def refresh(self) -> None:
            pass

        def baseline_verdicts(self, local_day: str):
            return None

        def commit_and_push(self, *, local_day, large_movement, message, boundary):
            captured["message"] = message
            return CommitResult(sha="s", sealed_sha=None, amended=False, pushed=True)

        def history(self):
            return []

        def restore(self, commit):
            pass

    monkeypatch.setattr(sync, "store_for", lambda config, data_dir: _SpyStore())

    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    now = mkts(2026, 9, 18, 21, 30)
    slack = FakeSlack(now=now, bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 12), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"snapA")])

    led, st = data_paths(tmp_path)
    result = run_sync(
        slack, cfg, detector=FakeFaceDetector({}), ledger_path=led, state_path=st,
        now_us=parse_ts(now), no_react=True, no_post=True,
    )
    assert result.exit_code == 0
    message = captured["message"]

    # Exactly one message became a COUNTED snipe this run (summary logs counted=1).
    body_lines = message.splitlines()[1:]           # drop the "<command> <day>" header
    reason_lines = [
        ln for ln in body_lines
        if ln.strip() and ln.split(None, 1)[0] != "rows"
    ]
    # §8.4 mandates a per-reason delta line (here `counted +1`); the current message has none.
    assert any(re.match(r"^counted \+1$", ln) for ln in reason_lines), (
        f"commit message omits the §8.4 `counted +1` delta line; body was {body_lines!r}"
    )


# --------------------------------------------------------------------------- Finding B


def test_digest_opted_out_uses_reconstruction_not_state(tmp_path):
    """20 §6.2 (digest posting) mandates the durable opt-out set for both eligibility and
    render: "elig = eligible_snipes(ledger, config.rules, config.roster, state.opted_out,
    ...)" and "render_digest(report, ..., state.opted_out, names, ...)".

    `post_digests` instead reconstructs the set from this run's verdicts
    (`_opted_out_from_verdicts`). A user who opted out (durable `state.opted_out`) but has
    no COUNTED/COOLDOWN pair this window surfaces in no verdict, so the reconstruction drops
    them. That user is a rostered group member, and `build_groups_table` counts members as
    `u not in opted_out`, so dropping them inflates the `groups` member count and changes the
    `numbers_hash` (00-data §9) posted to the channel. The digest sync posts therefore does
    not match the spec-mandated render off `state.opted_out`.
    """
    report = ReportSpec(
        name="daily", cadence=Cadence.DAILY, at_hour=21, at_minute=0, weekday=None,
        post_to=None, sections=(Section.DAY, Section.GROUPS), top_n=5,
    )
    roster = roster_of({"U0A": "fam", "U0B": "fam", "U0C": "fam", ADMIN: None})
    # U0C is durably opted out but sends/receives no snipe this window.
    cfg = make_config(roster=roster, reports=(report,), seed_opted_out=("U0C",))
    now = mkts(2026, 9, 18, 21, 30)
    slack = FakeSlack(now=now, bot_user_id=BOT,
                      users=_users("U0A", "U0B", "U0C", ADMIN),
                      channels=(CHANNEL,), bot_member_of=(CHANNEL,))
    slack.post(at=mkts(2026, 9, 18, 12), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"snapA")])

    led, st = data_paths(tmp_path)
    result = run_sync(
        slack, cfg, detector=FakeFaceDetector({}), ledger_path=led, state_path=st,
        now_us=parse_ts(now), no_react=True,
    )
    assert result.exit_code == 0 and result.digests_posted == 1
    posted_hash = _digest_posts(slack)[0]["data"]["metadata"]["event_payload"]["numbers_hash"]

    # The spec-mandated render: the durable opt-out set {U0C}.
    rows = load_ledger(led)
    sem = cfg.semesters[0]
    names = NameResolver({u: u for u in ("U0A", "U0B", "U0C")}, cfg.roster)
    elig = eligible_snipes(rows, cfg.rules, cfg.roster, {"U0C"},
                           cfg.semesters, cfg.tz, sem)
    correct = render_digest(
        report, "daily:2026-09-18", sem, elig, cfg.roster, {"U0C"}, names, cfg.tz,
        revision=0, selfie_emoji=cfg.feedback.selfie,
    )
    assert posted_hash == correct.metadata.numbers_hash, (
        "posted digest was rendered with a reconstructed opt-out set, not state.opted_out"
    )
