"""Round-1 cross-module spec-conformance red team against the new modules
snipebot/slack_io.py, snipebot/doctor.py and snipebot/cli.py + snipebot/faces.py
(10-slack-io.md §3, §9; 40-config-cli.md §5.2, §6.1).

Each test is a BREAK: it FAILS on the current code and would pass once the code
conforms to the cited spec sentence. Self-contained; no network, no real Slack,
no other red-team module imported.
"""

from __future__ import annotations

import io
import sys
import urllib.error
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- #
# Finding 1 — slack_io: fetch_file_bytes ignores 429 reactive honouring.
# --------------------------------------------------------------------------- #

def test_fetch_file_bytes_honours_retry_after_before_raising(monkeypatch):
    """10-slack-io.md §3 (Rate-limit handling), row `fetch_file_bytes`:
    "only **reactive** `429 + Retry-After` honouring applies (same injected
    sleep, up to `MAX_RATE_LIMIT_RETRIES`, then `RateLimited`)."

    The real client must sleep the injected `Retry-After` and retry the same
    request up to MAX_RATE_LIMIT_RETRIES consecutive 429s before raising. The
    current code raises `RateLimited` on the first 429 with no sleep and no
    retry, so a single transient 429 aborts a rendition fetch the spec says
    should be honoured.
    """
    from snipebot import slack_io

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            "https://files.slack.com/x", 429, "Too Many Requests",
            {"Retry-After": "2"}, None,
        )

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)

    slept: list[float] = []
    client = slack_io.SlackWebClient(
        object(), sleep=lambda s: slept.append(s), bot_token="xoxb-test",
    )

    with pytest.raises(slack_io.RateLimited):
        client.fetch_file_bytes("https://files.slack.com/x")

    # Reactive honouring: the injected sleep is called once per honoured 429,
    # and the request is retried, before the terminal RateLimited.
    assert slept == [2] * slack_io.MAX_RATE_LIMIT_RETRIES
    assert calls["n"] == slack_io.MAX_RATE_LIMIT_RETRIES + 1


# --------------------------------------------------------------------------- #
# Finding 2 — doctor: DOC-AUTH never verifies the token is a bot token.
# --------------------------------------------------------------------------- #

def test_doc_auth_rejects_non_bot_token():
    """40-config-cli.md §5.2, DOC-AUTH: "`auth.test` succeeds **and** the token
    is a **bot** token"; §6.1: `SLACK_BOT_TOKEN` is "validated by `DOC-AUTH` to
    be a bot (`xoxb-`) token."

    A user (`xoxp-`) token authenticates successfully but its `auth.test`
    carries no `bot_id` (`AuthIdentity.bot_id` is the "B… id that appears on bot
    messages"). DOC-AUTH must FAIL such a token. The current `_check_auth`
    returns PASS whenever `auth_identity()` does not raise, so a non-bot token
    is accepted.
    """
    from snipebot import doctor
    from snipebot.slack_io import AuthIdentity

    class _UserTokenSlack:
        def auth_identity(self) -> AuthIdentity:
            return AuthIdentity(
                user_id="U123", bot_id="", team_id="T1", url="https://x.slack.com",
            )

    _identity, result = doctor._check_auth(_UserTokenSlack())
    assert result.id == "DOC-AUTH"
    assert result.ok is False
