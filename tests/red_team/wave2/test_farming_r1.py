"""Wave 2 red-team, round 1 (spec conformance): a player farming points or dodging
gates end to end through `snipebot.sync.run_sync` with `FakeSlack` + `FakeFaceDetector`
+ FilesStore.

Every test here asserts a rule the spec states and the current code violates, so each
FAILS on the code under attack. The focus is the L8 audit list (the `--dry-run` surface
that flags "the verdicts most likely to be wrong", spec 40-config-cli.md section 4):
`snipebot/sync.py` builds it with only five categories
(`text_blocks_disagree`, `non_permitted_veto`, `late_tag`, `ambiguous_selfie`, `repost`),
so the anti-abuse categories the spec also requires are silently absent — a player can
farm the exact patterns those flags exist to surface, and no spot-check line is printed.

Helpers mirror tests/_helpers_sync.py + tests/test_sync.py (copied, not imported beyond
the shared builders, per the wave brief).
"""

from __future__ import annotations

from pathlib import Path

from snipebot.faces import FakeFaceDetector
from snipebot.sync import Command, run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import (
    BOT,
    CHANNEL,
    image_file,
    make_config,
    mkts,
    roster_of,
)

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


def _dry_run_audit(slack, config, tmp_path, *, now, detector=None) -> str:
    """Run the real pipeline under --dry-run (backfill) and return the audit stderr."""
    led, st = _paths(tmp_path)
    run_sync(
        slack, config, detector=detector or FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now),
        command=Command.BACKFILL, dry_run=True,
    )


def test_file_sig_repost_not_audit_flagged(tmp_path, capsys):
    """00-data.md section 2 (File signature): "A likely repost is flagged in the L8
    audit (not auto-rejected) when two rows **from the same sender** share any
    `file_sig`." 40-config-cli.md section 4 lists "likely reposts (shared `file_sig`
    from one sender)" among the L8 categories, distinct from the hash `repost` gate.

    A player re-uploads the same photo with a one-pixel change: the bytes differ (so the
    hash `REPOST` gate does NOT fire and it counts again) but name/size/dimensions are
    identical, so the `file_sig` heuristic matches. The spec requires this be flagged for
    a manual spot check. sync.py never computes same-sender `file_sig` collisions, so the
    second post is flagged nowhere: farming the repost flag is invisible.
    """
    # U0A and U0B are in different sibling groups -> non-sib, so no faces are fetched and
    # no rendition hash is taken; this isolates the file_sig heuristic from the hash gate.
    roster = roster_of({"U0A": "red", "U0B": "blue"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    # Same name ("snap.png"), same size (4 bytes), same 100x100 dims -> identical file_sig;
    # different bytes -> different rendition, so no hash REPOST. 20 min apart -> both count.
    slack.post(at=mkts(2026, 9, 18, 10, 0), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"AAAA")])
    ts2 = slack.post(at=mkts(2026, 9, 18, 10, 20), user="U0A", channel=CHANNEL,
                     text="<@U0B>", files=[image_file("F02", b"BBBB")])

    _dry_run_audit(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    err = capsys.readouterr().err
    # The second post shares a file_sig with the first from the same sender and must be
    # flagged as a likely repost in the L8 audit. It is not.
    assert f"ts={ts2}" in err


def test_first_seen_already_edited_not_audit_flagged(tmp_path, capsys):
    """00-data.md section 2 (`first_sight_edited`): audit flag "first seen already edited"
    (L8) — "a good snipe whose caption was later fixed must not be voided". 40-config-cli.md
    section 4: the L8 audit lists "rows first seen already edited (tag assumed present at
    posting)".

    A message first observed already edited is TAKEN as tagged-at-posting (plan section 1),
    which is exactly the backdating loophole the flag exists to spot-check. sync.py stores
    `first_sight_edited` on the row but never adds it to the audit list, so a message that
    was edited before the bot ever saw it is accepted with no spot-check line.
    """
    roster = roster_of({"U0A": "red", "U0B": "blue"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    ts = slack.post(at=mkts(2026, 9, 18, 10, 0), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"snap")])
    # Edit the message BEFORE the bot's first sync -> first observation carries "edited"
    # so parse sets first_sight_edited = True; the tag is still present at first sight.
    slack.edit(at=mkts(2026, 9, 18, 10, 5), ts=ts, channel=CHANNEL, user="U0A",
               text="<@U0B> fixed caption")
    slack.as_of(mkts(2026, 9, 18, 12))

    _dry_run_audit(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    err = capsys.readouterr().err
    assert f"ts={ts}" in err


def test_off_roster_sender_not_audit_flagged(tmp_path, capsys):
    """40-config-cli.md section 4: the L8 audit flags "off-roster senders and off-roster
    targets". These are the rows the roster gate silently drops (spec 20 section 4 /
    plan section 6 "Roster gate, both sides"); surfacing them is how an operator notices a
    real player who was never added to `players`.

    A message from a sender who is not on the roster is gated SENDER_OFF_ROSTER and
    NOT_COUNTED, but sync.py's audit dict has no off-roster category, so the drop is
    invisible in the audit list.
    """
    # U0OFF is not in the roster; U0B is.
    roster = roster_of({"U0B": "blue"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0OFF", "U0B"))
    ts = slack.post(at=mkts(2026, 9, 18, 10, 0), user="U0OFF", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"snap")])

    _dry_run_audit(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    err = capsys.readouterr().err
    assert f"ts={ts}" in err


def test_rejected_near_cooldown_not_audit_flagged(tmp_path, capsys):
    """40-config-cli.md section 4: the L8 audit flags rows "rejected with under 60 s of
    cooldown to spare" — the near-misses an operator most wants to eyeball, because a
    player who re-snipes 14:30 after a counted snipe is farming the cooldown edge.

    A second A->B snipe 14 min 30 s after the first (default 15 min cooldown, 30 s of
    window left) is COOLDOWN-rejected, but sync.py's audit dict never flags near-cooldown
    rejections, so this near-miss is printed nowhere.
    """
    roster = roster_of({"U0A": "red", "U0B": "blue"})
    cfg = make_config(roster=roster)  # default cooldown 15 min
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    slack.post(at=mkts(2026, 9, 18, 10, 0, 0), user="U0A", channel=CHANNEL,
               text="<@U0B>", files=[image_file("F01", b"snap1")])
    # 14 min 30 s later -> 30 s short of the 15 min cooldown: rejected with < 60 s to spare.
    ts2 = slack.post(at=mkts(2026, 9, 18, 10, 14, 30), user="U0A", channel=CHANNEL,
                     text="<@U0B>", files=[image_file("F02", b"snap2")])

    _dry_run_audit(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    err = capsys.readouterr().err
    assert f"ts={ts2}" in err
