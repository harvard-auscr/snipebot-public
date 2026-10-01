"""The live L6 rig entry (50-test-matrix.md section 7, rows ``L6-RIG-*``).

Opt-in: every test here is marked ``rig`` and depends on the session fixture, which
skips with a clear reason unless the five rig env vars are set (README). So in the
normal suite -- no tokens -- this module collects and skips cleanly, touching no
network. When the vars ARE set (a throwaway workspace, gate G1), the fixture runs
the ordered scenario ONCE (``rig_scenario.run``) and each test asserts one facet of
the read-back through ``assertions``: the ledger rows, the reactions actually on the
messages, the posted digests and their metadata, the ``post_to`` second channel, the
selfie round trip, and the API-vs-phone candidate parity.

The offline halves of the rig (``plan``, ``render_config``, the assertion helpers)
are covered by ``test_rig_offline.py``; nothing here runs without a workspace.
"""

from __future__ import annotations

import os

import pytest

from tests.rig import assertions as A
from tests.rig import rig_scenario as R

pytestmark = pytest.mark.rig


@pytest.fixture(scope="session")
def rig_results():
    """Run the whole scenario once per session, or skip when unconfigured."""
    env = dict(os.environ)
    absent = R.missing_env(env)
    if absent:
        pytest.skip(
            "rig needs a real workspace: set " + ", ".join(R.REQUIRED_ENV)
            + " (missing: " + ", ".join(absent) + ")"
        )
    import tempfile

    with tempfile.TemporaryDirectory(prefix="snipebot-rig-") as workdir:
        yield R.run(env, workdir=workdir)


def _react(results, step_id):
    ts = results.ts_by_step[step_id]
    return results.reactions[ts], ts


# --- L6-RIG-ledger: the ordered ledger rows and their verdicts ---------------

def test_rig_ledger_rows(rig_results):
    rows = rig_results.ledger_rows
    ids = rig_results.ts_by_step
    A.assert_status(rows, ids["R1"], "COUNTED")          # R1
    A.assert_status(rows, ids["R2"], "COOLDOWN")         # R2
    A.assert_blocked_by(rows, ids["R2"], ids["R1"])      # R2 blocked by R1
    A.assert_status(rows, ids["R3"], "COUNTED")          # R3 (rig allow_bots)


# --- L6-RIG-reactions: R1/R5 reactions actually on the message ---------------
# (paired positive control: CTL-RIG-NOREACT)

def test_rig_reactions(rig_results):
    counted = rig_results.emojis["counted"]
    veto = rig_results.emojis["veto"]

    # Right after R1's own sync the bot's counted emoji is on the message.
    A.assert_reaction_present(rig_results.step_reactions["R1"],
                              rig_results.bot_user_id, counted)
    r1_msg, _ = _react(rig_results, "R1")
    # After R1 the counted emoji is on the message; after the R5 veto it is removed
    # and the human veto remains -- the final read-back reflects the converged state.
    A.assert_reaction_absent(r1_msg, rig_results.bot_user_id, counted)   # R5 removed it
    assert rig_results.ids["U_HUMAN"] in A.reactors(r1_msg, veto)         # human veto present


# --- L6-RIG-selfie: R12/R13 the 🤳 round trip and the group points -----------

def test_rig_selfie(rig_results):
    selfie = rig_results.emojis["selfie"]
    counted = rig_results.emojis["counted"]
    # Right after R12's own sync the bot's selfie emoji is on it, with counted.
    r12_then = rig_results.step_reactions["R12"]
    A.assert_reaction_present(r12_then, rig_results.bot_user_id, selfie)
    A.assert_reaction_present(r12_then, rig_results.bot_user_id, counted)
    r12_msg, _ = _react(rig_results, "R12")
    # After the R13 `selfie --no` override the bot's 🤳 is gone; the counted stays.
    A.assert_reaction_absent(r12_msg, rig_results.bot_user_id, selfie)
    A.assert_reaction_present(r12_msg, rig_results.bot_user_id, counted)
    # Points: 2 after R12, dropping to 1 after the R13 override.
    if "R12" in rig_results.group_points:
        A.assert_group_points(rig_results.group_points["R12"], 2)
    if "R13" in rig_results.group_points:
        A.assert_group_points(rig_results.group_points["R13"], 1)


# --- L6-RIG-digest: the posted digest and its metadata key -------------------

def test_rig_digest_posted_in_channel(rig_results):
    # At least one snipe_digest is in the watched channel with a well-formed key.
    digests = [
        m for m in rig_results.main_messages
        if isinstance(m.get("metadata"), dict)
        and m["metadata"].get("event_type") == A.DIGEST_EVENT_TYPE
    ]
    assert digests, "no digest posted in the watched channel"
    for digest in digests:
        payload = digest["metadata"]["event_payload"]
        assert payload.get("period_key")
        assert payload.get("numbers_hash")
        assert payload.get("revision") is not None

    # R7: exactly one digest for the day period, revision 0. R9: that same message
    # chat.updated in place -- revision 1, a new numbers_hash, the _revised_ marker.
    snap = rig_results.digest_snapshots
    keys = {
        m["metadata"]["event_payload"].get("period_key") for m in snap["R7"]
        if isinstance(m.get("metadata"), dict)
        and m["metadata"].get("event_type") == A.DIGEST_EVENT_TYPE
    }
    assert len(keys) == 1, f"expected one day period digested at R7, got {len(keys)}"
    key = keys.pop()
    d7 = A.assert_one_digest(snap["R7"], key)
    d9 = A.assert_one_digest(snap["R9"], key)
    p7 = d7["metadata"]["event_payload"]
    p9 = d9["metadata"]["event_payload"]
    assert p7.get("revision") == 0, f"R7 digest revision {p7.get('revision')!r}"
    assert d9.get("ts") == d7.get("ts"), "R9 digest is not the R7 message updated in place"
    assert p9.get("revision") == 1, f"R9 digest revision {p9.get('revision')!r}"
    assert p9.get("numbers_hash") != p7.get("numbers_hash"), "R9 numbers_hash unchanged"
    assert "_revised_" in repr(d9.get("blocks", [])) + str(d9.get("text", "")), \
        "R9 digest lacks the _revised_ marker"


# --- L6-RIG-postto: R8 the post_to second channel ----------------------------

def test_rig_post_to_second_channel(rig_results):
    off_digests = [
        m for m in rig_results.off_messages
        if isinstance(m.get("metadata"), dict)
        and m["metadata"].get("event_type") == A.DIGEST_EVENT_TYPE
    ]
    assert off_digests, "no digest posted to the post_to (C_OFF) channel"
    off_channel = rig_results.ids["C_OFF"]
    for digest in off_digests:
        period = digest["metadata"]["event_payload"]["period_key"]
        A.assert_post_to(rig_results.main_messages, rig_results.off_messages, period)
        assert digest["metadata"]["event_payload"]["channel"] == off_channel


# --- L6-RIG-parity: R11 API-vs-phone candidate parity (the L6->G2 bridge) -----

def test_rig_api_phone_parity(rig_results):
    api = rig_results.parity.get("api")
    phone = rig_results.parity.get("phone")
    if api is None or phone is None:
        pytest.skip("no phone-shape G2 fixture supplied for the parity check")
    A.assert_parity(api, phone)
