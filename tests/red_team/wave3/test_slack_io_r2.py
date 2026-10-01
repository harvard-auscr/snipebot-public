"""Wave 3 red-team, round 2 (HOSTILE INPUT / FAILURE INJECTION) against
snipebot/slack_io.py.

Target: the real transport (`SlackWebClient` / `make_client`), stubbed at the
`slack_sdk.WebClient` method surface or (for `fetch_file_bytes`) at
`urllib.request.urlopen`. Never the network, never tests.fake_slack. Each test
asserts a rule stated in spec/10-slack-io.md and FAILS on the current code,
proving a break.
"""

from __future__ import annotations

import http.client

import pytest

import snipebot.slack_io as slack_io
from snipebot.slack_io import (
    MAX_RATE_LIMIT_RETRIES,
    RateLimited,
    SlackError,
    SlackHTTPError,
    SlackTransportError,
    SlackWebClient,
)


# --------------------------------------------------------------------------- #
# Minimal stubs (this file never imports tests.fake_slack).
# --------------------------------------------------------------------------- #

class _Resp:
    """A slack_sdk-shaped response: `.data`, `.status_code`, `.headers`."""

    def __init__(self, data, *, status_code: int = 200, headers=None) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}


class _FixedClient:
    """Every Web API method name returns the same canned response object."""

    def __init__(self, resp) -> None:
        self._resp = resp

    def __getattr__(self, name):
        def method(**kwargs):
            return self._resp

        return method


def _noop_sleep(_seconds: float) -> None:
    return None


class _BodyRaisingResponse:
    """A urlopen() result: serves one chunk, then raises on the next read()."""

    def __init__(self, first_chunk: bytes, exc: Exception) -> None:
        self._first = first_chunk
        self._exc = exc
        self._served = False

    def read(self, _size):
        if not self._served:
            self._served = True
            return self._first
        raise self._exc

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


# --------------------------------------------------------------------------- #
# Finding 1 — a truncated body (IncompleteRead) escapes the error taxonomy.
# --------------------------------------------------------------------------- #

def test_body_stream_incompleteread_escapes_taxonomy(monkeypatch):
    """spec/10-slack-io.md section 3: "Every failure is one of these classes;
    nothing else escapes a `SlackIO` method." `SlackTransportError` is the class
    for a "response the client never received" / "transport failure / dropped
    response". A server that closes the connection mid-body makes
    `response.read()` raise `http.client.IncompleteRead` (an HTTPException, not an
    OSError/URLError), which the body-streaming `except` clause does not catch, so
    it escapes `fetch_file_bytes` raw instead of as a SlackError.
    """
    def fake_urlopen(request, timeout=None):
        return _BodyRaisingResponse(
            b"partial", http.client.IncompleteRead(b"partial", 999)
        )

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = SlackWebClient(_FixedClient(_Resp({})), bot_token="xoxb-test")

    with pytest.raises(SlackError):
        client.fetch_file_bytes("https://files.slack.com/x")


# --------------------------------------------------------------------------- #
# Finding 2 — a non-200 with a non-JSON body is misclassified as a transport
# error instead of SlackHTTPError (the Mapping guard runs before the status).
# --------------------------------------------------------------------------- #

def test_non200_nonjson_body_becomes_transport_error():
    """spec/10-slack-io.md section 3 taxonomy table: "non-200/429 HTTP ->
    `SlackHTTPError`". A 503 whose body is a proxy HTML error page (not a JSON
    object) is still a received non-200 response and must map to `SlackHTTPError`
    (with `.status`), yet `_call` checks `isinstance(data, Mapping)` before it
    ever looks at the status, so a non-Mapping body is forced into
    `SlackTransportError` regardless of the 503 status.
    """
    resp = _Resp("<html>503 Service Unavailable</html>", status_code=503)
    client = SlackWebClient(_FixedClient(resp), bot_token="xoxb-test")

    with pytest.raises(SlackHTTPError) as excinfo:
        client.auth_identity()
    assert excinfo.value.status == 503


# --------------------------------------------------------------------------- #
# Finding 3 — a 429 with a non-JSON body is misclassified as a transport error
# and never honoured as a rate limit.
# --------------------------------------------------------------------------- #

def test_rate_limit_with_nonjson_body_becomes_transport_error():
    """spec/10-slack-io.md section 3 taxonomy table: "HTTP 429 (after retries) ->
    `RateLimited`", and `SlackTransportError` is defined as "No usable HTTP
    response". A 429 whose body is empty/non-JSON (a CDN or proxy 429) is a usable
    HTTP response carrying a real status and a `Retry-After`; it must go down the
    429 path (honour + eventually `RateLimited`), but `_call`'s
    `isinstance(data, Mapping)` guard runs before the `status == 429` check, so a
    429 with a `None`/non-Mapping body raises `SlackTransportError` on the first
    hit and the rate limit is never honoured.
    """
    resp = _Resp(None, status_code=429, headers={"Retry-After": "1"})
    client = SlackWebClient(
        _FixedClient(resp), bot_token="xoxb-test", sleep=_noop_sleep
    )

    with pytest.raises(RateLimited):
        client.auth_identity()


# --------------------------------------------------------------------------- #
# Finding 4 — RateLimited.method carries the raw slack_sdk method name, not the
# SlackIO method name the spec example prescribes.
# --------------------------------------------------------------------------- #

def test_ratelimited_method_is_slackio_name_not_api_name():
    """spec/10-slack-io.md section 3: `class RateLimited` documents
    `method: str  # "history" | "reactions_add" | ... , for the log`. For a
    rate-limited `history()` call the method label must therefore be the SlackIO
    method name "history", but `_call` is invoked with the raw Web API name
    "conversations_history" and stores that on the exception, so
    `RateLimited.method` is "conversations_history".
    """
    resp = _Resp(
        {"ok": False, "error": "ratelimited"},
        status_code=429,
        headers={"Retry-After": "1"},
    )
    client = SlackWebClient(
        _FixedClient(resp), bot_token="xoxb-test", sleep=_noop_sleep
    )

    with pytest.raises(RateLimited) as excinfo:
        client.history("C123", "0")
    # sanity: the retry budget was honoured before raising
    assert excinfo.value.retry_after_seconds == 1
    assert excinfo.value.method == "history"
