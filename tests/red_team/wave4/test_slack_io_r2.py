"""Wave 4, round 2 breaker tests for snipebot/slack_io.py (the real transport).

Offline only: Web API calls go through a real slack_sdk WebClient whose lowest HTTP
hook (`_perform_urllib_http_request_internal`) is monkeypatched, or a stub client;
fetch_file_bytes runs with every http.client connect() replaced by a refusal, so no
socket is ever opened.
"""

from __future__ import annotations

import email.message
import http.client
import json

import pytest

import snipebot.slack_io as slack_io
from snipebot.slack_io import SlackAPIError, SlackError, SlackTransportError, SlackWebClient


# --------------------------------------------------------------------------- helpers


def _headers(**extra: str) -> email.message.Message:
    msg = email.message.Message()
    msg["content-type"] = "application/json; charset=utf-8"
    for name, value in extra.items():
        msg[name] = value
    return msg


def _refuse_connect(self, *_a, **_k):
    raise ConnectionRefusedError("offline test: connect() is disabled")


class _Resp:
    def __init__(self, data, *, status_code: int = 200, headers=None) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}


class _Stub:
    def __init__(self, **queues) -> None:
        self._queues = {k: list(v) for k, v in queues.items()}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**kwargs):
            return self._queues[name].pop(0)

        return call


# --------------------------------------------------------------------------- findings


def test_post_message_is_not_resent_after_a_dropped_response(monkeypatch):
    """Claim: make_client builds `slack_sdk.WebClient(token=token)` with slack_sdk's
    default retry handlers, which include ConnectionErrorRetryHandler (retries once on
    RemoteDisconnected / ConnectionResetError / URLError). When chat.postMessage reaches
    Slack but the connection drops before the response arrives, slack_sdk silently sends
    the same post again inside the same run, so the digest is posted twice. Violates
    10 section 3 "Idempotent re-post": a post_message that fails with
    SlackTransportError may or may not have posted, and the run does NOT retry the post
    inside the same run (dedup is left to the next run's metadata check)."""
    from slack_sdk import WebClient
    import slack_sdk.http_retry.handler as retry_handler

    sent: list[str] = []

    def fake_internal(self, url, req):
        sent.append(url)
        if len(sent) == 1:
            # Slack accepted the post; the connection dropped before the reply.
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return {
            "status": 200,
            "headers": _headers(),
            "body": json.dumps({"ok": True, "channel": "C0MAIN01", "ts": "1790000001.000100"}),
        }

    monkeypatch.setattr(WebClient, "_perform_urllib_http_request_internal", fake_internal)
    monkeypatch.setattr(retry_handler.time, "sleep", lambda _s: None)

    client = slack_io.make_client("xoxb-fixture")
    try:
        client.post_message(
            "C0MAIN01", text="digest", blocks=[],
            metadata={"event_type": "snipe_digest", "event_payload": {"period_key": "d"}},
        )
    except SlackTransportError:
        pass
    assert len(sent) == 1, f"chat.postMessage was sent {len(sent)} times in one run"


def test_fetch_file_bytes_non_ascii_url_raises_a_slack_error(monkeypatch):
    """Claim: fetch_file_bytes hands the rendition URL to urllib unquoted. A URL whose
    path carries a non-ASCII character (a file named with an accent, as a file-name
    derived thumb or download path can be) makes http.client raise UnicodeEncodeError
    while encoding the request line. That is a ValueError, not one of the caught
    (URLError, TimeoutError, OSError, HTTPException), so it escapes as a non-SlackError.
    The sync faces block tolerates only RateLimited/FileTooLarge/SlackHTTPError/
    SlackTransportError, so the whole run aborts, and every later run re-fetches the
    same image and aborts again. Violates 10 section 2 (fetch_file_bytes: no usable
    response -> SlackTransportError; DECISION: nothing but a SlackError escapes a SlackIO
    method) and 20 section 5 (a per-image fetch fault gives no count, run continues)."""
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _refuse_connect)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", _refuse_connect)
    client = SlackWebClient(_Stub(), bot_token="xoxb-fixture", sleep=lambda s: None)
    try:
        client.fetch_file_bytes(
            "https://files.slack.com/files-tmb/T0TEAM-F0FILE001-ab/café_1024.jpg"
        )
    except SlackError:
        pass  # a SlackError of any class (or a fetched body) is acceptable
    # Any non-SlackError propagates out of the call above and fails the test.


def test_history_ok_true_without_messages_raises_malformed_response():
    """Claim: history pages conversations.history through `_paged` with required=False,
    so an ok:true body with no documented `messages` field returns [] instead of
    raising. sync then sees every in-window row as absent from history and marks up to
    sync.max_deletes_per_run (default 5) counted rows DELETED under the breaker, changing
    the standings. users_list/conversations_members were made strict last round; the
    most important read was left lenient. Violates 10 section 2 DECISION: an ok:true
    response missing a documented result field raises SlackAPIError('malformed_response')
    ('any failed page aborts the run with nothing written', 10 section 2 Partial
    failure)."""
    client = SlackWebClient(
        _Stub(conversations_history=[
            _Resp({"ok": True, "has_more": False, "response_metadata": {"next_cursor": ""}}),
        ]),
        bot_token="xoxb-fixture", sleep=lambda s: None,
    )
    with pytest.raises(SlackAPIError) as info:
        client.history("C0MAIN01", "1790000000.000000")
    assert info.value.error == "malformed_response"


def test_auth_scopes_reads_x_oauth_scopes_case_insensitively(monkeypatch):
    """Claim: slack_sdk builds SlackResponse.headers as `dict(response["headers"])`, a
    plain dict that keeps the server's header-name casing. auth_scopes looks up the
    exact key 'x-oauth-scopes', so a server that sends 'X-OAuth-Scopes' (the casing
    Slack's own docs print) yields an empty scope set and DOC-SCOPES reports every
    manifest scope missing. The same casing defect was fixed for Retry-After last round
    but not here. Violates 40 section 5.2 DOC-SCOPES (granted scopes read from the
    auth.test x-oauth-scopes header) and RFC 9110 section 5.1 (header names are
    case-insensitive)."""
    from slack_sdk import WebClient

    def fake_internal(self, url, req):
        return {
            "status": 200,
            "headers": _headers(**{"X-OAuth-Scopes": "channels:history,chat:write"}),
            "body": json.dumps({"ok": True, "user_id": "U0BOT01", "bot_id": "B0BOT",
                                "team_id": "T0TEAM", "url": "https://fixture.invalid/"}),
        }

    monkeypatch.setattr(WebClient, "_perform_urllib_http_request_internal", fake_internal)
    client = SlackWebClient(WebClient(token="xoxb-fixture"), bot_token="xoxb-fixture",
                            sleep=lambda s: None)
    assert client.auth_scopes() == frozenset({"channels:history", "chat:write"})
