"""Wave 3 red-team, round 3 (INVARIANTS) against snipebot/slack_io.py.

Target: the real transport (`SlackWebClient` / `make_client`), stubbed at the
`slack_sdk.WebClient` method surface or (for `fetch_file_bytes`) at
`urllib.request.urlopen`. Never the network, never `tests.fake_slack`.

Invariant under test (spec/10-slack-io.md section 3): "Every failure is one of
these classes; nothing else escapes a `SlackIO` method", and `SlackTransportError`
is the class for a "connection reset, timeout, or a response the client never
received". `fetch_file_bytes` drives the HTTP transport by hand (urllib), so it
must map *every* transport-layer failure into that taxonomy.

Each test asserts that rule and FAILS on the current code, proving a break.
"""

from __future__ import annotations

import http.client

import pytest

import snipebot.slack_io as slack_io
from snipebot.slack_io import SlackError, SlackWebClient


class _BodyThenRaise:
    """A urlopen() result: serves one chunk, then raises `exc` on the next read().

    Mirrors what `http.client.HTTPResponse.read()` does on a live but malformed
    (e.g. badly re-chunked) body: it hands back the bytes it already has and then
    raises while decoding the next chunk.
    """

    def __init__(self, first_chunk: bytes, exc: BaseException) -> None:
        self._first = first_chunk
        self._exc = exc
        self._served = False

    def read(self, _size: int) -> bytes:
        if not self._served:
            self._served = True
            return self._first
        raise self._exc

    def __enter__(self) -> "_BodyThenRaise":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


# --------------------------------------------------------------------------- #
# Finding 1 — a connect / status-line HTTPException escapes the taxonomy.
#
# The connect-phase handler in fetch_file_bytes catches only
#   `except (urllib.error.URLError, TimeoutError, OSError)`.
# But `urllib.request.urlopen` reads the response status line via
# `http.client.HTTPResponse.getresponse()`, which is called OUTSIDE urllib's
# own OSError->URLError wrapping. A proxy/CDN that returns a malformed status
# line makes it raise `http.client.BadStatusLine`, whose MRO is
# BadStatusLine -> HTTPException -> Exception (it is NOT an OSError/URLError/
# TimeoutError; only `RemoteDisconnected` among the http.client errors is an
# OSError). So it escapes fetch_file_bytes raw, not as a SlackError.
# --------------------------------------------------------------------------- #

def test_fetch_connect_badstatusline_escapes_taxonomy(monkeypatch):
    """spec/10-slack-io.md section 3: "Every failure is one of these classes;
    nothing else escapes a `SlackIO` method." A malformed HTTP status line from a
    proxy/CDN (`http.client.BadStatusLine`, an HTTPException that is not an
    OSError) raised while opening the rendition connection must surface as a
    `SlackError` (`SlackTransportError` -- "a response the client never
    received"), yet the connect-phase `except` clause omits `HTTPException`, so
    it escapes `fetch_file_bytes` raw.
    """
    def fake_urlopen(request, timeout=None):
        raise http.client.BadStatusLine("HTTP/1.0 <garbage-from-proxy>")

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = SlackWebClient(object(), bot_token="xoxb-test")

    with pytest.raises(SlackError):
        client.fetch_file_bytes("https://files.slack.com/x")


# --------------------------------------------------------------------------- #
# Finding 2 — a body-phase HTTPException other than IncompleteRead escapes.
#
# The body-streaming handler was widened to
#   `except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead)`
# but that catches ONE HTTPException subtype, not the class. Reading a chunked
# body, `http.client.HTTPResponse.read()` can raise `http.client.LineTooLong`
# ("chunk size") -- an HTTPException that is NOT IncompleteRead and NOT an
# OSError -- when a chunk-size line is malformed/over-long. It therefore escapes
# `fetch_file_bytes` raw, so the earlier IncompleteRead fix is subtype-specific,
# not taxonomy-complete.
# --------------------------------------------------------------------------- #

def test_fetch_body_linetoolong_escapes_taxonomy(monkeypatch):
    """spec/10-slack-io.md section 3: "Every failure is one of these classes;
    nothing else escapes a `SlackIO` method", and `SlackTransportError` covers "a
    response the client never received". A chunked-body decode failure
    (`http.client.LineTooLong`, an HTTPException that is neither `IncompleteRead`
    nor an OSError) raised mid-stream must surface as a `SlackError`, yet the
    body-streaming `except` clause enumerates only `IncompleteRead`, so any other
    `HTTPException` escapes `fetch_file_bytes` raw.
    """
    def fake_urlopen(request, timeout=None):
        return _BodyThenRaise(b"partial", http.client.LineTooLong("chunk size"))

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = SlackWebClient(object(), bot_token="xoxb-test", max_image_bytes=10**9)

    with pytest.raises(SlackError):
        client.fetch_file_bytes("https://files.slack.com/x")
