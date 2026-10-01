"""Wave-3 round-3 cross-module breaks against the new modules (slack_io, doctor,
faces, cli). Round 3 hunts the invariants: parity between the fake and the real
transport, honest error taxonomy, no silent degradation. Every test in this file is
written to FAIL on the current code and PASS only once the break is fixed; no test
touches the network (every client here is a hand-rolled in-process stub).
"""

from __future__ import annotations

import pytest

from snipebot.slack_io import SlackError, SlackWebClient


class _Resp:
    """The minimal shape SlackWebClient._call / auth_scopes read off a response:
    a status_code, a parsed `data` body, and response `headers`."""

    def __init__(self, status, data, headers=None):
        self.status_code = status
        self.data = data
        self.headers = headers or {}


def test_auth_scopes_swallows_non200_status_into_empty_scopeset():
    """10-slack-io.md §3 error taxonomy: "non-200/429 HTTP | SlackHTTPError" and
    "Every failure is one of these classes; nothing else escapes a SlackIO method."
    `auth_scopes` is the seam DOC-SCOPES reads the `x-oauth-scopes` header from
    (§2 / 40 §5.2), and it issues a real `auth.test` call — but it never inspects
    the HTTP status the way `_call` does. A 503 whose body is a proxy/CDN error page
    is therefore swallowed into an *empty* frozenset() instead of raising
    SlackHTTPError, so DOC-SCOPES then reports a spurious "missing scopes: <all>"
    FAIL (exit 10) on what is really a transient transport fault.

    Contrast: `auth_identity`, which DOES go through `_call`, raises SlackHTTPError
    for the identical 503 response.
    """

    class _Auth503:
        def auth_test(self, **kwargs):
            return _Resp(503, "<html>502 Bad Gateway</html>", {})

    client = SlackWebClient(_Auth503())

    # auth_identity classifies the 503 correctly (control: proves the seam SEES 503).
    with pytest.raises(SlackError):
        client.auth_identity()

    # auth_scopes must not silently swallow a non-200 into an empty scope set.
    with pytest.raises(SlackError):
        client.auth_scopes()
