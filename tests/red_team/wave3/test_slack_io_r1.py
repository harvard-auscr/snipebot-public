"""Wave 3 red-team, round 1 (SPEC CONFORMANCE) against snipebot/slack_io.py.

Target: the real transport (`SlackWebClient` / `make_client`), stubbed at the
`slack_sdk.WebClient` method surface or (for `fetch_file_bytes`) at
`urllib.request.urlopen`. Never the network. Each test asserts a rule stated in
spec/10-slack-io.md and FAILS on the current code, proving a break.
"""

from __future__ import annotations

import urllib.error

import pytest

import snipebot.slack_io as slack_io
from snipebot.slack_io import (
    MAX_RATE_LIMIT_RETRIES,
    RateLimited,
    SlackError,
    SlackTransportError,
    SlackWebClient,
)


# --------------------------------------------------------------------------- #
# Minimal stubs (this file never imports tests.fake_slack).
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, data, *, status_code: int = 200, headers=None) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}


class _StubClient:
    def __init__(self, **queues) -> None:
        self._queues = {name: list(q) for name, q in queues.items()}

    def __getattr__(self, name):
        def method(**kwargs):
            queue = self._queues.get(name)
            if not queue:
                raise AssertionError(f"no more stubbed responses for {name!r}")
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return method


def _sleep_recorder():
    calls: list[float] = []
    return (lambda s: calls.append(s)), calls


class _FakeHTTPResponse:
    """A urlopen() result: read() serves chunks, then optionally raises."""

    def __init__(self, chunks, *, raise_after=None) -> None:
        self._chunks = list(chunks)
        self._raise_after = raise_after
        self._served = 0

    def read(self, _size):
        if self._raise_after is not None and self._served >= self._raise_after:
            raise self._raise_after_exc
        if not self._chunks:
            return b""
        self._served += 1
        return self._chunks.pop(0)

    _raise_after_exc: Exception = ConnectionResetError("peer reset the connection mid-body")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


# --------------------------------------------------------------------------- #
# Finding 1 — fetch_file_bytes does not honour 429 reactively.
# --------------------------------------------------------------------------- #

def test_fetch_file_bytes_429_retries_with_injected_sleep(monkeypatch):
    """spec/10-slack-io.md section 3 rate-limit table, `fetch_file_bytes` row:
    "only reactive `429 + Retry-After` honouring applies (same injected sleep,
    up to MAX_RATE_LIMIT_RETRIES, then RateLimited)." A single 429 followed by a
    good response must be honoured (sleep Retry-After, retry the same GET) and
    return the bytes -- not abort on the first 429.
    """
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                "https://files.slack.com/x", 429, "Too Many Requests",
                {"Retry-After": "2"}, None,
            )
        return _FakeHTTPResponse([b"real-bytes"])

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    sleep, sleeps = _sleep_recorder()
    client = SlackWebClient(_StubClient(), bot_token="xoxb-test", sleep=sleep)

    data = client.fetch_file_bytes("https://files.slack.com/x")

    assert data == b"real-bytes"       # the retried GET succeeded
    assert sleeps == [2]               # slept the Retry-After via the injected sleep


def test_fetch_file_bytes_429_raises_only_after_max_retries(monkeypatch):
    """spec/10-slack-io.md section 3 rate-limit table, `fetch_file_bytes` row:
    "up to MAX_RATE_LIMIT_RETRIES, then RateLimited". A wall of 429s must be
    honoured MAX_RATE_LIMIT_RETRIES times (that many injected sleeps) before
    RateLimited is raised -- the current code raises on the first 429 with no
    sleep at all.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://files.slack.com/x", 429, "Too Many Requests",
            {"Retry-After": "1"}, None,
        )

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    sleep, sleeps = _sleep_recorder()
    client = SlackWebClient(_StubClient(), bot_token="xoxb-test", sleep=sleep)

    with pytest.raises(RateLimited):
        client.fetch_file_bytes("https://files.slack.com/x")
    assert len(sleeps) == MAX_RATE_LIMIT_RETRIES


# --------------------------------------------------------------------------- #
# Finding 2 — a connection reset while streaming the body escapes the taxonomy.
# --------------------------------------------------------------------------- #

def test_fetch_file_bytes_connection_reset_midbody_is_transport_error(monkeypatch):
    """spec/10-slack-io.md section 3: "Every failure is one of these classes;
    nothing else escapes a `SlackIO` method", and `SlackTransportError` covers
    "connection reset ... or a response the client never received". A reset
    while streaming the body must surface as SlackTransportError, not a raw
    ConnectionResetError.
    """
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse([b"partial"], raise_after=1)

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = SlackWebClient(_StubClient(), bot_token="xoxb-test")

    with pytest.raises(SlackTransportError):
        client.fetch_file_bytes("https://files.slack.com/x")
