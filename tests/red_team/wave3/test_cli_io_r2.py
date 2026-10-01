"""Wave-3 red-team round 2 — HOSTILE INPUT / DISPATCH conformance for snipebot/cli.py.

Breaker tests: each asserts a behaviour the spec REQUIRES and is expected to FAIL on the
current code (a passing test is not a finding). Slack never touches the network: the
Slack-touching command is driven through an injected FakeSlack, and the production doctor
dispatch is exercised with `snipebot.slack_io.make_client` monkeypatched to a stub so the
"should have built a client from SLACK_BOT_TOKEN" path never reaches a socket.

Spec anchors:
  * 40-config-cli.md §4.2 (`roster`, `doctor`), §5 / §5.2 (doctor Slack block), §6.1
    (SLACK_BOT_TOKEN required for the doctor Slack block).
"""

from __future__ import annotations

import calendar
import json
from pathlib import Path

import yaml

from snipebot import cli
from snipebot.cli import main
from snipebot.faces import FakeFaceDetector
from snipebot.slack_io import AuthIdentity

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import image_file
from snipebot.ts import US_PER_SECOND

CHANNEL = "C0MAIN01"
BOT = "U0BOT"
ADMIN = "U0ADMIN"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0, micro=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.{micro:06d}"


NOW_TS = _ts(2026, 9, 18, 12)
NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 10)


def _config_dict():
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
            "multi_tag": "per_target", "max_targets_per_message": None,
            "edit_grace_minutes": 10, "max_snipes_per_target_per_day": None,
            "allow_self": False, "allow_bots": False, "count_thread_replies": False,
            "count_image_links": False, "allow_video": False, "selfie_bonus": False,
        },
        "players": {"count_intra_group": True,
                    "groups": {"fam": ["U0AAAA1", "U0BBBB1"]}, "extras": []},
        "consent": {
            "veto": {"emoji": "no_entry_sign", "by": ["admins"]},
            "optout_messages": [], "opted_out": [],
        },
        "admins": [ADMIN],
        "feedback": {
            "reactions": {
                "counted": "white_check_mark", "cooldown": "hourglass_flowing_sand",
                "untagged": None, "not_counted": "x", "selfie": None,
            },
            "review": {"min_targets": 5, "emoji": "question"},
        },
        "reports": [],
    }


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config_dict(), sort_keys=False), encoding="utf-8")
    return path


def _data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d


def _base(cfg: Path, data: Path):
    return ["--config", str(cfg), "--data-dir", str(data)]


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


# --------------------------------------------------------------------------- #
# Finding: `doctor` (online) never builds a Slack client from SLACK_BOT_TOKEN.
# `_cmd_doctor` forwards the injection-only `slack_factory` (None in production)
# straight to `doctor.run`, which then reports DOC-AUTH FAIL "no Slack client
# available" and exits 10 for EVERY production `snipebot doctor` run — even with a
# valid bot token and a healthy workspace. The Slack block of §5.2 is dead in prod.
# --------------------------------------------------------------------------- #

class _HealthyBotStub:
    """A minimal SlackIO a fixed `make_client` would return for a healthy bot token.
    No network: in the current (buggy) code it is never constructed at all."""

    def auth_identity(self) -> AuthIdentity:
        return AuthIdentity(user_id=BOT, bot_id="B0BOT", team_id="T1",
                            url="https://x.slack.com/")

    def auth_scopes(self):
        return frozenset()

    def __getattr__(self, name):
        def _method(*a, **k):  # pragma: no cover - not reached in the buggy path
            raise AssertionError(f"unexpected network call: {name}")
        return _method


def test_doctor_online_builds_client_from_bot_token(tmp_path, monkeypatch):
    """40-config-cli.md §6.1: `SLACK_BOT_TOKEN` is required "for any command that
    touches Slack (... `doctor` Slack block)" and §5: the against-Slack block "needs
    `SLACK_BOT_TOKEN`". So a production `snipebot doctor` (no `--offline`) with the
    token set must BUILD a real client (via `slack_io.make_client`) and run the Slack
    checks — not report DOC-AUTH "no Slack client available" and exit 10."""
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-valid-bot-token")

    calls: list[str] = []

    def fake_make_client(token, **kwargs):
        calls.append(token)
        return _HealthyBotStub()

    # A correct dispatch reads SLACK_BOT_TOKEN and builds the client through this seam
    # (exactly as every other Slack-touching command does via cli._get_slack).
    monkeypatch.setattr("snipebot.slack_io.make_client", fake_make_client)

    # Production dispatch: no slack_factory injected.
    main(["doctor", "--json", *_base(cfg, data)])

    assert calls == ["xoxb-valid-bot-token"], (
        "online `doctor` must build a Slack client from SLACK_BOT_TOKEN, but "
        "_cmd_doctor forwards the (None) injection factory and never calls make_client, "
        "so the entire §5.2 Slack block is dead in production and DOC-AUTH FAILs with "
        "'no Slack client available' (exit 10)"
    )


# --------------------------------------------------------------------------- #
# Finding: `roster` never refreshes the `users.json` cache. §4.2 states roster
# "Writes nothing (refreshes the `users.json` cache locally)"; the cache it is meant
# to refresh is exactly what `report`/`export` read for id->name resolution
# (cli._users_cache). The current handler prints and returns, writing no users.json.
# --------------------------------------------------------------------------- #

def test_roster_refreshes_users_cache(tmp_path, monkeypatch):
    """40-config-cli.md §4.2 (`roster`): "Writes nothing (refreshes the `users.json`
    cache locally)." After a successful `roster` the local `users.json` cache must be
    refreshed with the fetched members so later `report`/`export` resolve names; the
    handler currently writes no `users.json` at all."""
    monkeypatch.setattr(cli, "_now_us", lambda: NOW_US)
    cfg = _write_config(tmp_path)
    data = _data_dir(tmp_path)
    cache_path = data / "users.json"
    assert not cache_path.exists()  # sanity: nothing there yet

    rc = main(["roster", *_base(cfg, data)], slack_factory=lambda: _world())
    assert rc == 0

    assert cache_path.exists(), (
        "roster must refresh the users.json cache locally (§4.2), but the command "
        "wrote no users.json, so report/export name resolution never sees these members"
    )
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "U0AAAA1" in cache
