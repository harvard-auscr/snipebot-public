"""Wave-3 red-team round 1 — SPEC CONFORMANCE for snipebot/cli.py end to end.

Breaker tests: each asserts the behaviour the spec REQUIRES and is expected to FAIL on
the current code (a passing test is not a finding). Slack never touches the network:
Slack-touching commands are driven either through an injected FakeSlack or through the
REAL transport class `SlackWebClient` over a canned in-memory WebClient stub.

Spec anchors:
  * 40-config-cli.md §4.4 exit-code table + §6.1 (SLACK_BOT_TOKEN)
  * 20-sync-ledger.md §9.1 exit-code table ("nothing" written column)
  * 40-config-cli.md §5.2 DOC-AUTH
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import State, load_ledger, load_state, save_state
from snipebot.parse import VetoSource
from snipebot.slack_io import SlackWebClient
from snipebot.ts import US_PER_SECOND

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file

CHANNEL = "C0MAIN01"
BOT = "U0BOT"
ADMIN = "U0ADMIN"

import calendar


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 10)


def _config_dict(*, persistence="files", selfie_bonus=False):
    return {
        "enabled": True,
        "persistence": persistence,
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
            "allow_bots": False,
            "count_thread_replies": False,
            "count_image_links": False,
            "allow_video": False,
            "selfie_bonus": selfie_bonus,
        },
        "players": {"count_intra_group": True,
                    "groups": {"fam": ["U0AAAA1", "U0BBBB1"]}, "extras": []},
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


def _write_config(tmp_path: Path, **kw) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config_dict(**kw), sort_keys=False), encoding="utf-8")
    return path


def _data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d


def _world() -> FakeSlack:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        "U0AAAA1": FakeUser(id="U0AAAA1", display_name="Alex"),
        "U0BBBB1": FakeUser(id="U0BBBB1", display_name="Bailey"),
        ADMIN: FakeUser(id=ADMIN, display_name="Admin"),
    }
    slack = FakeSlack(now=NOW_TS, bot_user_id=BOT, users=users)
    slack.post(at=MSG_TS, user="U0AAAA1", channel=CHANNEL, text="<@U0BBBB1>",
               files=[image_file("F01", b"snap")])
    return slack


def _base(cfg: Path, data: Path):
    return ["--config", str(cfg), "--data-dir", str(data)]


def _sync_build(cfg, data, slack):
    return main(["sync", "--no-react", "--no-post", *_base(cfg, data)],
                slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({}))


# --------------------------------------------------------------------------- #
# Finding: a Slack-touching writing command mutates durable data on disk BEFORE
# the Slack client is even constructed, so when Slack is unreachable it exits 5
# (SLACK_ERROR) yet has already written the change — violating the §9.1 exit-code
# table, whose exit-5 row writes "nothing".
# --------------------------------------------------------------------------- #

def test_veto_writes_nothing_when_slack_unreachable(tmp_path, monkeypatch):
    """40-config-cli.md §4.4 / 20-sync-ledger.md §9.1 exit-code table, exit 5 row:
    "Slack fetch/API failure ... | nothing" written. A `veto` that cannot reach Slack
    (no SLACK_BOT_TOKEN) exits 5 (40 §6.1: "Absent -> ... exit 5") and must therefore
    leave the ledger unchanged; the CLI writes the veto before building the client."""
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    assert _sync_build(cfg, data, _world()) == 0
    before = load_ledger(data / "ledger.jsonl")
    assert [r.ts for r in before] == [MSG_TS]
    assert before[0].vetoes == ()  # sanity: no veto yet

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    rc = main(["veto", "--ts", MSG_TS, "--by", ADMIN, *_base(cfg, data)])
    assert rc == int(Exit.SLACK_ERROR)

    after = load_ledger(data / "ledger.jsonl")
    row = next(r for r in after if r.ts == MSG_TS)
    assert not any(v.source == VetoSource.CLI for v in row.vetoes), (
        "exit-5 run must write nothing, but the CLI persisted the veto before "
        "the Slack client was built"
    )


def test_rejoin_writes_nothing_when_slack_unreachable(tmp_path, monkeypatch):
    """40-config-cli.md §4.4 / 20-sync-ledger.md §9.1, exit 5 row writes "nothing".
    `rejoin` is a Slack-touching large movement (40 §4.2); when Slack is unreachable it
    exits 5, so it must not have already removed the user from `state.json`. The CLI
    deletes and saves the opted-out entry before constructing the client."""
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    save_state(data / "state.json", State(opted_out={"U0BBBB1": NOW_US}))

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    rc = main(["rejoin", "U0BBBB1", *_base(cfg, data)])
    assert rc == int(Exit.SLACK_ERROR)

    state = load_state(data / "state.json")
    assert "U0BBBB1" in state.opted_out, (
        "exit-5 run must write nothing, but the CLI removed the opted-out entry "
        "before the Slack client was built"
    )


# --------------------------------------------------------------------------- #
# Finding: DOC-AUTH accepts a NON-bot (user) token. Spec 40 §5.2 requires
# DOC-AUTH to FAIL unless "auth.test succeeds AND the token is a bot token".
# The real transport is exercised over a canned WebClient whose auth.test
# succeeds but returns no bot_id (the shape a user/xoxp token produces).
# --------------------------------------------------------------------------- #

class _CannedResp:
    def __init__(self, data, *, status_code=200, headers=None):
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}


class _CannedWebClient:
    """A stubbed slack_sdk.WebClient: each API method name returns a fixed canned
    response dict. No network. auth.test succeeds but carries NO bot_id."""

    def __init__(self, responses):
        self._responses = responses

    def __getattr__(self, name):
        responses = object.__getattribute__(self, "_responses")
        if name not in responses:
            raise AssertionError(f"unexpected slack_sdk method: {name!r}")

        def method(**kwargs):
            return responses[name]

        return method


def _user_token_web_client() -> _CannedWebClient:
    scopes = ("channels:read,groups:read,chat:write,reactions:read,"
              "reactions:write,users:read,channels:history,groups:history")
    return _CannedWebClient({
        # auth.test SUCCEEDS but has no bot_id -> this is a user token, not a bot token.
        "auth_test": _CannedResp(
            {"ok": True, "user_id": BOT, "team_id": "T1", "url": "https://x.slack.com/"},
            headers={"x-oauth-scopes": scopes},
        ),
        "conversations_info": _CannedResp(
            {"ok": True, "channel": {"id": CHANNEL, "is_member": True,
                                     "is_private": False, "name": "snipes"}}),
        "users_list": _CannedResp(
            {"ok": True, "response_metadata": {}, "members": [
                {"id": "U0AAAA1", "deleted": False, "is_bot": False,
                 "profile": {"display_name": "Alex"}},
                {"id": "U0BBBB1", "deleted": False, "is_bot": False,
                 "profile": {"display_name": "Bailey"}},
                {"id": ADMIN, "deleted": False, "is_bot": False,
                 "profile": {"display_name": "Admin"}},
                {"id": BOT, "deleted": False, "is_bot": True, "profile": {}},
            ]}),
        "conversations_members": _CannedResp(
            {"ok": True, "response_metadata": {},
             "members": ["U0AAAA1", "U0BBBB1", ADMIN]}),
    })


def test_doctor_auth_fails_on_non_bot_token(tmp_path, capsys):
    """40-config-cli.md §5.2 DOC-AUTH: FAIL unless "`auth.test` succeeds **and** the
    token is a **bot** token". A user token whose auth.test succeeds but returns no
    bot_id must make DOC-AUTH FAIL (ok == False); the check only tests that auth.test
    did not raise."""
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)

    def factory():
        return SlackWebClient(_user_token_web_client(), bot_token="xoxb-canned")

    main(["doctor", "--json", *_base(cfg, data)], slack_factory=factory)
    out = capsys.readouterr().out
    records = [json.loads(ln) for ln in out.splitlines() if ln.strip().startswith("{")]
    auth = next(r for r in records if r["id"] == "DOC-AUTH")
    assert auth["ok"] is False, (
        "DOC-AUTH must FAIL for a non-bot (user) token, but the check passed on an "
        "auth.test that merely did not raise"
    )
