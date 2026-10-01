"""Wave 4, round 1 breaker tests for snipebot/slack_io.py (the real transport).

Offline only: Web API calls go through a stub client or a real slack_sdk WebClient whose
lowest HTTP hook is monkeypatched; fetch_file_bytes runs the real urllib opener against a
monkeypatched http.client connection class, so no socket is ever opened.
"""

from __future__ import annotations

import email.message
import http.client
import io
import json
import urllib.request

import pytest

import snipebot.slack_io as slack_io
from snipebot.slack_io import AuthError, SlackAPIError, SlackError, SlackWebClient


# --------------------------------------------------------------------------- helpers


class _Resp:
    def __init__(self, data, *, status_code: int = 200, headers=None) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}


class _ApiError(Exception):
    """Same shape as slack_sdk.errors.SlackApiError: carries `.response`."""

    def __init__(self, response: _Resp) -> None:
        super().__init__("api error")
        self.response = response


class _Stub:
    def __init__(self, **queues) -> None:
        self._queues = {k: list(v) for k, v in queues.items()}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**kwargs):
            item = self._queues[name].pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item.data, dict) and not item.data.get("ok", False):
                raise _ApiError(item)
            return item

        return call


def _sleeps():
    calls: list[float] = []
    return calls.append, calls


class _Sock:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def makefile(self, *_a, **_k):
        return io.BytesIO(self._raw)


class _FakeHTTPSConnection:
    """Stands in for http.client.HTTPSConnection under urllib's real handlers
    (redirect handling included); answers from a per-host table."""

    routes: dict[str, bytes] = {}
    log: list[tuple[str, str, dict]] = []

    def __init__(self, host, timeout=None, **_kw) -> None:
        self.host = host
        self.sock = None

    def set_debuglevel(self, _level) -> None:
        pass

    def set_tunnel(self, *_a, **_k) -> None:
        pass

    def request(self, method, url, body=None, headers=None, **_kw) -> None:
        type(self).log.append((self.host, url, dict(headers or {})))

    def getresponse(self):
        raw = type(self).routes[self.host]
        resp = http.client.HTTPResponse(_Sock(raw), method="GET")
        resp.begin()
        return resp

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- findings


def test_fetch_redirect_to_foreign_host_does_not_forward_bot_token(monkeypatch):
    """Claim: fetch_file_bytes sends `Authorization: Bearer <bot token>` on the
    original request and urllib's default redirect handler copies that header onto
    the redirected request, so a 302 from files.slack.com to a non-Slack host hands
    the bot token to that host. Violates the deploy requirement that the token is
    never sent off Slack hosts (10 section 2: the bearer header authenticates the
    file's private Slack rendition URL, a `files.slack.com` resource)."""
    _FakeHTTPSConnection.log = []
    _FakeHTTPSConnection.routes = {
        "files.slack.com": (
            b"HTTP/1.1 302 Found\r\n"
            b"Location: https://elsewhere.fixture.invalid/photo-1.jpg\r\n"
            b"Content-Length: 0\r\n\r\n"
        ),
        "elsewhere.fixture.invalid": (
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc"
        ),
    }
    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeHTTPSConnection)
    monkeypatch.setattr(
        urllib.request, "_opener",
        urllib.request.build_opener(urllib.request.ProxyHandler({})),
    )
    client = SlackWebClient(_Stub(), bot_token="xoxb-fixture", sleep=lambda s: None)
    try:
        client.fetch_file_bytes("https://files.slack.com/files-tmb/T0TEAM-F0FILE001/photo-1.jpg")
    except SlackError:
        pass  # refusing the redirect is an acceptable fix

    first = _FakeHTTPSConnection.log[0]
    assert first[0] == "files.slack.com"
    assert first[2].get("Authorization") == "Bearer xoxb-fixture"  # non-vacuous
    leaked = [
        host for host, _url, headers in _FakeHTTPSConnection.log
        if host != "files.slack.com"
        and any(k.lower() == "authorization" for k in headers)
    ]
    assert leaked == [], f"bot token sent to non-Slack host(s): {leaked}"


def test_users_list_ok_true_without_members_raises_malformed_response():
    """Claim: `_paged` reads `data.get("members") or []`, so an ok:true users.list
    body missing its documented `members` field returns [] instead of raising.
    sync then rewrites the users.json cache from an empty list. Violates 10 section 2
    DECISION: an ok:true response missing a documented result field (`members`
    named explicitly) raises SlackAPIError("malformed_response"). The same path
    serves conversations_members."""
    client = SlackWebClient(
        _Stub(users_list=[_Resp({"ok": True, "response_metadata": {"next_cursor": ""}})]),
        bot_token="xoxb-fixture", sleep=lambda s: None,
    )
    with pytest.raises(SlackAPIError) as info:
        client.users_list()
    assert info.value.error == "malformed_response"


def test_ok_false_ratelimited_honours_lowercase_retry_after_from_real_sdk(monkeypatch):
    """Claim: for an ok:false `ratelimited` body on HTTP 200, slack_sdk hands the
    response headers over as a plain dict with the server's header-name casing
    (base_client: `headers=dict(response["headers"])`; it only adds the dual
    Retry-After/retry-after keys on the 429 path). `_parse_retry_after` looks up
    the exact key "Retry-After", so a `retry-after: 30` header is ignored and the
    request is retried after 1 s. Violates 10 section 3 (E9 row + rate-limit
    table): the ok:false rate limit takes the same path as 429 and honours
    Retry-After; header names are case-insensitive (RFC 9110 section 5.1)."""
    from slack_sdk import WebClient

    bodies = [
        json.dumps({"ok": False, "error": "ratelimited"}),
        json.dumps({"ok": True, "channel": {"id": "C0MAIN01"}}),
    ]

    def fake_internal(self, url, req):
        msg = email.message.Message()
        msg["content-type"] = "application/json; charset=utf-8"
        msg["retry-after"] = "30"
        return {"status": 200, "headers": msg, "body": bodies.pop(0)}

    monkeypatch.setattr(WebClient, "_perform_urllib_http_request_internal", fake_internal)
    sleep, calls = _sleeps()
    client = SlackWebClient(WebClient(token="xoxb-fixture"), bot_token="xoxb-fixture", sleep=sleep)
    assert client.channel_info("C0MAIN01") == {"id": "C0MAIN01"}
    assert calls == [30]


def test_auth_scopes_ok_false_invalid_auth_raises_auth_error():
    """Claim: auth_scopes never inspects the body, so an auth.test that answers
    ok:false `invalid_auth` (HTTP 200, raised by slack_sdk as SlackApiError with a
    response) returns an empty scope set and doctor reports every manifest scope as
    missing instead of the real cause. Violates 10 section 3: `invalid_auth` maps
    to AuthError, the fatal classes being distinct "so doctor can print a precise
    cause" (40 section 5.2 DOC-SCOPES reads the same auth.test call)."""
    client = SlackWebClient(
        _Stub(auth_test=[_Resp({"ok": False, "error": "invalid_auth"})]),
        bot_token="xoxb-fixture", sleep=lambda s: None,
    )
    with pytest.raises(AuthError):
        client.auth_scopes()
