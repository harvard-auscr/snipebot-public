"""Adversarial spec-conformance probes for tests/fake_slack.py (spec 10-slack-io.md
sections 2, 5, 7). Each test encodes one stated rule and is expected to FAIL against
the current fake, proving a divergence from the specification.

Inputs are built by hand through the constructor and the authoring API; no fixture or
production module beyond the SlackIO surface is relied on.
"""

from __future__ import annotations

from tests.fake_slack import FakeSlack, FakeUser

NOW = "1758210000.000000"


def _basic(**overrides) -> FakeSlack:
    kwargs = {
        "now": NOW,
        "users": {
            "U0BOT": FakeUser(id="U0BOT", is_bot=True),
            "U01AAA": FakeUser(id="U01AAA", display_name="a", real_name="a"),
            "U02AAA": FakeUser(id="U02AAA", display_name="b", real_name="b"),
        },
        "channels": ("C0MAIN01",),
        "bot_member_of": ("C0MAIN01",),
        "horizon_days": 90,
    }
    kwargs.update(overrides)
    return FakeSlack(**kwargs)


_DIGEST_METADATA = {
    "event_type": "snipe_digest",
    "event_payload": {"report": "daily", "period_key": "daily:2026-09-18"},
}


def test_history_reactions_keep_first_added_order():
    """Spec 10 section 2 (What each method does): `history` is "RAW, unchanged. Every
    returned message dict is exactly as Slack sent it, including files, reactions,
    edited, metadata, subtype, thread_ts, bot_id." and `reactions_get` returns the
    "Same shape as a history message".

    Slack orders a message's `reactions` array by the instant each emoji was first
    added, not alphabetically. Here "zebra" is added before "apple", so a raw payload
    must list zebra first. The fake re-sorts reactions by name, so the order it returns
    is not "exactly as Slack sent it".
    """
    slack = _basic()
    parent = slack.post(at="1758210000.000100", user="U01AAA", channel="C0MAIN01", text="x")
    slack.react(at="1758210001.000000", ts=parent, channel="C0MAIN01", user="U01AAA", name="zebra")
    slack.react(at="1758210002.000000", ts=parent, channel="C0MAIN01", user="U02AAA", name="apple")
    slack.as_of("1758210003.000000")

    msgs = slack.history("C0MAIN01", oldest="0.000000")
    names = [r["name"] for r in msgs[0]["reactions"]]
    assert names == ["zebra", "apple"]  # fails: fake returns sorted ["apple", "zebra"]


def test_reloaded_world_preserves_private_channel_and_name():
    """Spec 10 section 7 (World file format): the `channels` object maps each id to
    "{is_member, is_private, name}", and "The event log is the source of truth" for a
    world that "lives in a file" so a reloaded fake answers identically. Section 5:
    "channel_info(c) returns {"id": c, "is_member": c in bot_member_of, "is_private":
    ..., "name": ...}".

    A world describing a private channel named "snipes" is loaded; the reloaded fake's
    channel_info must report is_private=True and name="snipes". The fake discards both
    fields on load and hard-codes is_private=False / name="" in channel_info, so the
    reload does not answer identically and re-serialising corrupts the stored channel.
    """
    seed = FakeSlack(now=NOW, channels=("C0MAIN01",), bot_member_of=("C0MAIN01",))
    world = seed.to_world_dict()
    world["channels"]["C0MAIN01"] = {"is_member": True, "is_private": True, "name": "snipes"}

    reloaded = FakeSlack.from_world_dict(world)
    info = reloaded.channel_info("C0MAIN01")
    assert info["is_private"] is True   # fails: fake returns False
    assert info["name"] == "snipes"     # fails: fake returns ""
