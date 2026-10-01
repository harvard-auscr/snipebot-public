"""Wave 2, Round 2 (hostile input / failure injection) — adversarial attack on
`snipebot/sync.py`: merge fact-replacement vs. window-bounded consent re-observation,
and the exit-code contract under injected Slack faults.

Every test in this file FAILS on the current code (a passing probe is not a finding).
Inputs are built with the FakeSlack authoring API and tests/_helpers_sync.py.
"""

from __future__ import annotations

from pathlib import Path

from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_ledger
from snipebot.slack_io import AuthError, SlackAPIError
from snipebot.sync import run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import BOT, CHANNEL, image_file, make_config, mkts, roster_of

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


def _fam():
    return roster_of({"U0A": "fam", "U0B": "fam", ADMIN: None})


def _snipe(slack, at, *, fid="F01", data=b"pic"):
    return slack.post(at=at, user="U0A", channel=CHANNEL, text="<@U0B>",
                      files=[image_file(fid, data)])


def _run(slack, config, tmp_path, *, now, no_react=True, **kw):
    led, st = _paths(tmp_path)
    return run_sync(
        slack, config, detector=FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now),
        no_post=True, no_react=no_react, **kw,
    )


def test_untolerated_reaction_error_returns_exit_1(tmp_path, capsys):
    """E-W4-21 (supersedes the 20 §9.1 "untolerated Slack error in step 7 -> 1" row): a
    `reactions_add` SlackAPIError (`internal_error`) never escapes `run_sync` and never fails
    the run; it is logged `WARN reaction_failed`, persist still runs, and the exit code is 0.
    """
    cfg = make_config(roster=_fam())
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B"))
    _snipe(slack, mkts(2026, 9, 18, 10))             # a counted snipe -> a desired reaction
    slack.faults.reaction_error(method="reactions_add", error="internal_error", times=9)

    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), no_react=False)
    assert r.exit_code == 0 and r.ledger_written
    assert "reaction_failed" in capsys.readouterr().err


def test_auth_identity_fault_returns_nonzero(tmp_path, monkeypatch):
    """20 §2 step 10: "return `SyncResult`; any raised failure has already set a nonzero code";
    §2 pins that "a failure in steps 0-6 therefore has zero side effects". The step-2
    `slack.auth_identity()` call sits OUTSIDE the fetch try/except, so a Slack fault there
    (a revoked/invalid token — `AuthError`) escapes `run_sync` uncaught rather than returning a
    nonzero SyncResult with nothing written.
    """
    cfg = make_config(roster=_fam())
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT,
                      users=_users("U0A", "U0B"))
    _snipe(slack, mkts(2026, 9, 18, 10))

    def boom():
        raise AuthError("token_revoked")

    monkeypatch.setattr(slack, "auth_identity", boom)
    r = _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    led, st = _paths(tmp_path)
    assert r.exit_code != 0
    assert not led.exists() and not st.exists()
