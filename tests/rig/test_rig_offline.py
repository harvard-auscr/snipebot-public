"""Offline tests for the L6 rig (50-test-matrix.md section 7).

These run in the normal suite -- no workspace, no tokens, no network. They pin the
parts of the rig that are pure or fake-backed:

* ``plan()`` lists every 7.2 step in order with the right actor;
* the ``assertions`` helpers judge canned read-back dicts correctly (a missing 🤳,
  a wrong digest hash and a stray bot reaction each fail; the good shapes pass);
* the config template renders with placeholder IDs and loads through
  ``snipebot.config.load_config``;
* ``run(env)`` with the tokens absent refuses cleanly -- ``RigNotConfigured`` before
  any Slack client is constructed (a ``slack_sdk.WebClient`` built here fails loudly);
* the ``CTL-RIG-NOREACT`` positive control: with reaction convergence in force the
  counted emoji lands on a counted snipe against ``FakeSlack``; the registry break
  (``sync`` skips convergence) turns this red, driven offline by the controls gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.rig import assertions as A
from tests.rig import rig_scenario as R


# --------------------------------------------------------------------------- #
# plan(): every 7.2 step in order with the right actor.
# --------------------------------------------------------------------------- #

def test_plan_is_the_ordered_r0_through_r13():
    ids = [step.id for step in R.plan()]
    assert ids == [f"R{i}" for i in range(14)]


def test_plan_step_ids_are_unique():
    ids = [step.id for step in R.plan()]
    assert len(ids) == len(set(ids))


def test_plan_actors_match_the_script():
    actor = {step.id: step.actor for step in R.plan()}
    # SYSTEM = a snipebot invocation (R0 doctor preflight, R13 CLI selfie override).
    # Every other step's primary action is a user-token human action.
    assert actor["R0"] is R.Actor.SYSTEM
    assert actor["R13"] is R.Actor.SYSTEM
    assert actor["R8"] is R.Actor.SYSTEM          # a pure scheduler advance, no human post
    human = {sid for sid, a in actor.items() if a is R.Actor.HUMAN}
    assert human == {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R9", "R10", "R11", "R12"}


def test_plan_r0_runs_doctor_everything_else_runs_sync():
    by_id = {step.id: step for step in R.plan()}
    assert by_id["R0"].then_run == "doctor"
    assert all(step.then_run == "sync" for step in R.plan() if step.id != "R0")


def test_plan_r10_runs_two_sync_passes():
    by_id = {step.id: step for step in R.plan()}
    assert by_id["R10"].passes == 2
    assert all(step.passes == 1 for step in R.plan() if step.id != "R10")


# --------------------------------------------------------------------------- #
# assertions helpers over canned read-back dicts.
# --------------------------------------------------------------------------- #

BOT = "U0BOT000"


def _msg(*reactions):
    return {"reactions": [dict(r) for r in reactions]}


def test_bot_reactions_reads_only_the_bots_own():
    msg = _msg(
        {"name": "white_check_mark", "users": [BOT], "count": 1},
        {"name": "no_entry_sign", "users": ["U0HUMAN0"], "count": 1},  # a human veto
    )
    assert A.bot_reactions(msg, BOT) == {"white_check_mark"}
    assert A.reactors(msg, "no_entry_sign") == {"U0HUMAN0"}


def test_counted_reaction_present_passes_and_missing_fails():
    counted = _msg({"name": "white_check_mark", "users": [BOT], "count": 1})
    A.assert_reaction_present(counted, BOT, "white_check_mark")  # R1: no raise
    bare = _msg()
    with pytest.raises(A.RigAssertionError):
        A.assert_reaction_present(bare, BOT, "white_check_mark")


def test_veto_removes_counted_emoji_read_back():
    # R5: after the veto the bot's counted emoji is gone; a human veto remains.
    vetoed = _msg({"name": "no_entry_sign", "users": ["U0HUMAN0"], "count": 1})
    A.assert_reaction_absent(vetoed, BOT, "white_check_mark")
    still_counted = _msg({"name": "white_check_mark", "users": [BOT], "count": 1})
    with pytest.raises(A.RigAssertionError):
        A.assert_reaction_absent(still_counted, BOT, "white_check_mark")


def test_selfie_reaction_present_after_r12_and_missing_fails():
    # R12: the bot carries exactly the counted + selfie emoji.
    good = _msg(
        {"name": "white_check_mark", "users": [BOT], "count": 1},
        {"name": "selfie", "users": [BOT], "count": 1},
    )
    A.assert_reaction_present(good, BOT, "selfie")
    A.assert_bot_reactions_exact(good, BOT, {"white_check_mark", "selfie"})

    # A missing 🤳 must fail (the selfie bonus did not land).
    missing_selfie = _msg({"name": "white_check_mark", "users": [BOT], "count": 1})
    with pytest.raises(A.RigAssertionError):
        A.assert_reaction_present(missing_selfie, BOT, "selfie")
    with pytest.raises(A.RigAssertionError):
        A.assert_bot_reactions_exact(missing_selfie, BOT, {"white_check_mark", "selfie"})


def test_exact_bot_reactions_rejects_a_stray_extra():
    # An extra bot reaction the convergence should have removed must fail.
    extra = _msg(
        {"name": "white_check_mark", "users": [BOT], "count": 1},
        {"name": "selfie", "users": [BOT], "count": 1},
        {"name": "question", "users": [BOT], "count": 1},   # stray
    )
    with pytest.raises(A.RigAssertionError):
        A.assert_bot_reactions_exact(extra, BOT, {"white_check_mark", "selfie"})


def test_ledger_row_assertions():
    rows = [
        {"ts": "100.000001", "status": "COUNTED", "blocked_by": None, "selfie": "NOT_APPLICABLE"},
        {"ts": "130.000001", "status": "COOLDOWN", "blocked_by": "100.000001",
         "selfie": "NOT_APPLICABLE"},
    ]
    A.assert_status(rows, "100.000001", "COUNTED")
    A.assert_status(rows, "130.000001", "COOLDOWN")
    A.assert_blocked_by(rows, "130.000001", "100.000001")   # R2 blocked by R1
    with pytest.raises(A.RigAssertionError):
        A.assert_status(rows, "130.000001", "COUNTED")
    with pytest.raises(A.RigAssertionError):
        A.assert_blocked_by(rows, "130.000001", None)
    with pytest.raises(A.RigAssertionError):
        A.find_row(rows, "999.000000")                       # no such row


def _digest(period_key, *, report="daily", channel="C0MAIN00", semester="rig-semester",
            numbers_hash="abc123", revision=0):
    return {
        "ts": "200.000001",
        "metadata": {
            "event_type": "snipe_digest",
            "event_payload": {
                "report": report,
                "period_key": period_key,
                "channel": channel,
                "semester": semester,
                "numbers_hash": numbers_hash,
                "revision": revision,
            },
        },
    }


def test_digest_in_channel_and_metadata_key():
    period = "daily:2026-09-18"
    messages = [{"ts": "1.0", "text": "chatter"}, _digest(period)]
    digest = A.assert_one_digest(messages, period)
    A.assert_digest_metadata(
        digest, report="daily", period_key=period, channel="C0MAIN00",
        semester="rig-semester", numbers_hash="abc123", revision=0,
    )
    # A wrong numbers_hash (a digest not re-rendered for a number change) fails.
    with pytest.raises(A.RigAssertionError):
        A.assert_digest_metadata(
            digest, report="daily", period_key=period, channel="C0MAIN00",
            semester="rig-semester", numbers_hash="WRONGHASH", revision=0,
        )
    # A stale revision fails too (R9 expects revision:1).
    with pytest.raises(A.RigAssertionError):
        A.assert_digest_metadata(
            digest, report="daily", period_key=period, channel="C0MAIN00",
            semester="rig-semester", numbers_hash="abc123", revision=1,
        )


def test_digest_dedup_and_missing_cases():
    period = "daily:2026-09-18"
    with pytest.raises(A.RigAssertionError):
        A.assert_one_digest([_digest(period), _digest(period)], period)   # two: not one
    with pytest.raises(A.RigAssertionError):
        A.assert_one_digest([{"ts": "1.0"}], period)                      # none
    A.assert_no_digest([{"ts": "1.0"}], period)                           # genuinely absent


def test_post_to_lands_in_off_channel_only():
    # R8: the post_to report's digest is in C_OFF, none duplicated in C_MAIN.
    period = "daily:2026-09-19"
    off = [_digest(period, report="officers", channel="C0OFF000")]
    main: list[dict] = [{"ts": "1.0", "text": "no digest here"}]
    digest = A.assert_post_to(main, off, period)
    assert digest["metadata"]["event_payload"]["channel"] == "C0OFF000"
    # If it also appeared in C_MAIN, the dedup guarantee is broken -> fail.
    with pytest.raises(A.RigAssertionError):
        A.assert_post_to([_digest(period)], off, period)


class _Cand:
    def __init__(self, targets, live_images, file_sigs):
        self.targets = targets
        self.live_images = live_images
        self.file_sigs = file_sigs


def test_parity_same_candidate_passes_diverging_fails():
    api = _Cand(("U0TARGET",), 1, ("sig-api",))
    phone = _Cand(("U0TARGET",), 1, ("sig-phone",))   # differs only by file identity
    A.assert_parity(api, phone)                        # modulo file identity: ok
    diverged = _Cand(("U0TARGET", "U0OTHER0"), 1, ("sig-phone",))
    with pytest.raises(A.RigAssertionError):
        A.assert_parity(api, diverged)                 # targets diverge -> rig invalid
    fewer_images = _Cand(("U0TARGET",), 0, ())
    with pytest.raises(A.RigAssertionError):
        A.assert_parity(api, fewer_images)


def test_group_points_two_then_one():
    A.assert_group_points(2, 2)   # R12
    A.assert_group_points(1, 1)   # R13
    with pytest.raises(A.RigAssertionError):
        A.assert_group_points(1, 2)


# --------------------------------------------------------------------------- #
# The config template renders with placeholder IDs and loads.
# --------------------------------------------------------------------------- #

_PLACEHOLDER_IDS = {
    "C_MAIN": "C0MAIN00",
    "C_OFF": "C0OFF000",
    "U_HUMAN": "U0HUMAN0",
    "U_TARGET": "U0TARGET",
    "U_BOT": "U0BOT01",
}


def test_template_renders_and_loads(tmp_path: Path):
    from snipebot.config import load_config

    text = R.render_config(_PLACEHOLDER_IDS)
    for placeholder in R.PLACEHOLDERS:
        assert "{{" + placeholder + "}}" not in text     # every placeholder filled
    dest = tmp_path / "config.yaml"
    dest.write_text(text, encoding="utf-8")
    config = load_config(dest)

    # The rig-specific knobs survived into the resolved config.
    assert config.channel == "C0MAIN00"
    assert config.admins == ("U0HUMAN0",)                 # sole admin
    rule = config.rules.entries[0]
    assert rule.allow_bots is True
    assert rule.selfie_bonus is True
    assert rule.cooldown.microseconds == 60_000_000       # 60 s
    # U_HUMAN and U_TARGET are one sibling group; U_BOT is rostered ungrouped.
    groups = {e.group for e in config.roster.entries.values()}
    assert groups == {"sibs", None}
    # Exactly one report carries a post_to second channel (C_OFF).
    post_tos = [r.post_to for r in config.reports if r.post_to is not None]
    assert post_tos == ["C0OFF000"]


def test_render_config_rejects_a_missing_placeholder():
    with pytest.raises(KeyError):
        R.render_config({"C_MAIN": "C0MAIN00"})           # C_OFF/U_HUMAN/U_TARGET/U_BOT absent


def test_rendered_roster_carries_the_bot_ungrouped(tmp_path: Path):
    """E-W4-20c: U_BOT is rostered with no group so the R3 bot target counts.

    Spec 50 sections 7.1/7.2: R3 tags U_BOT and expects COUNTED under the rig's
    ``allow_bots: true``; the rules gate roster membership before allow_bots, so the
    bot must be on the roster (ungrouped) or R3 scores TARGET_OFF_ROSTER.
    """
    from snipebot.config import load_config

    assert "U_BOT" in R.PLACEHOLDERS
    config = load_config(R.write_config(_PLACEHOLDER_IDS, tmp_path / "c.yaml"))
    entry = config.roster.entries["U0BOT01"]
    assert entry.group is None
    assert config.roster.entries["U0HUMAN0"].group == "sibs"
    assert config.roster.entries["U0TARGET"].group == "sibs"
    missing = {k: v for k, v in _PLACEHOLDER_IDS.items() if k != "U_BOT"}
    with pytest.raises(KeyError):
        R.render_config(missing)                          # U_BOT has no fallback


def test_write_config_matches_render(tmp_path: Path):
    dest = R.write_config(_PLACEHOLDER_IDS, tmp_path / "c.yaml")
    assert dest.read_text(encoding="utf-8") == R.render_config(_PLACEHOLDER_IDS)


# --------------------------------------------------------------------------- #
# missing_env / run() refuse cleanly with no tokens and touch no network.
# --------------------------------------------------------------------------- #

def test_missing_env_lists_all_five_when_empty():
    assert R.missing_env({}) == list(R.REQUIRED_ENV)
    assert not R.is_configured({})


def test_missing_env_reports_only_the_absent_ones():
    env = {
        R.ENV_BOT_TOKEN: "xoxb-...",
        R.ENV_USER_TOKEN: "xoxp-...",
        R.ENV_CHANNEL: "snipes",
        # officers + target absent
    }
    assert R.missing_env(env) == [R.ENV_OFFICERS, R.ENV_TARGET]


def test_run_without_tokens_refuses_before_any_client(monkeypatch):
    # Any attempt to construct a Slack client would be a network-capable object; make
    # it explode so a regression that builds one before the env gate is caught here.
    import slack_sdk

    def _boom(*a, **k):
        raise AssertionError("WebClient must not be constructed when unconfigured")

    monkeypatch.setattr(slack_sdk, "WebClient", _boom)

    with pytest.raises(R.RigNotConfigured) as exc:
        R.run({})
    assert "rig not configured" in str(exc.value)


def test_run_with_partial_env_still_refuses(monkeypatch):
    import slack_sdk

    monkeypatch.setattr(
        slack_sdk, "WebClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not construct")),
    )
    with pytest.raises(R.RigNotConfigured):
        R.run({R.ENV_BOT_TOKEN: "xoxb-only"})


# --------------------------------------------------------------------------- #
# CTL-RIG-NOREACT positive control, driven offline against FakeSlack.
#
# With reaction convergence in force, `sync` places the counted emoji on a plainly
# counted snipe. The registry break (`sync._converge_reactions` stubbed to a no-op)
# is exactly "step 7 skipped": the emoji never lands, and this assertion turns red.
# The controls-registry gate drives this node id with and without the break.
# --------------------------------------------------------------------------- #

def test_ctl_rig_noreact_positive_control(tmp_path: Path):
    from snipebot.faces import FakeFaceDetector
    from snipebot.sync import run_sync
    from snipebot.ts import parse_ts

    from tests.fake_slack import FakeSlack, FakeUser
    from tests._helpers_sync import (
        BOT as SBOT,
        CHANNEL,
        image_file,
        make_config,
        mkts,
        roster_of,
    )

    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)   # counted = white_check_mark; selfie bonus off
    users = {SBOT: FakeUser(id=SBOT, is_bot=True),
             "U0A": FakeUser(id="U0A"), "U0B": FakeUser(id="U0B")}
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=SBOT, users=users)
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL,
                    text="<@U0B>", files=[image_file("F01", b"snap")])

    data = tmp_path / "data"
    data.mkdir()
    run_sync(
        slack, cfg, detector=FakeFaceDetector({}),
        ledger_path=data / "ledger.jsonl", state_path=data / "state.json",
        now_us=parse_ts(mkts(2026, 9, 18, 12)), no_post=True,
    )

    final = slack.reactions_get(CHANNEL, ts)
    # The read-back reaction assertion the rig uses, against the real convergence:
    # with the break applied this is empty and the assertion turns red.
    A.assert_reaction_present(final, SBOT, cfg.feedback.counted)
