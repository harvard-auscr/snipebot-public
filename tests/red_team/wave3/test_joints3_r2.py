"""Wave 3 red-team, round 2 — HOSTILE INPUT / FAILURE INJECTION across the new
io/adapter/CLI modules (snipebot/slack_io.py, doctor.py, faces.py, cli.py).

Each test asserts a rule the spec REQUIRES and is written to FAIL on the current
code, proving a cross-module break under malformed responses, odd HTTP behaviour,
or a missing runtime dependency. No network is ever touched: HTTP is stubbed at
`urllib.request.urlopen`, the Web API at a canned client, and the real transport
is exercised in-process. A passing test would not be a finding.

Spec anchors:
  * spec/10-slack-io.md section 3  (error taxonomy: "nothing else escapes")
  * spec/40-config-cli.md section 5 (doctor: offline + Slack blocks)
  * spec/40-config-cli.md section 6.1/6.2 (SLACK_BOT_TOKEN; runtime deps)
"""

from __future__ import annotations

import http.client
import json
import sys
import types
from pathlib import Path

import pytest
import yaml

import snipebot.doctor as doctor
import snipebot.slack_io as slack_io
from snipebot.cli import main
from snipebot.slack_io import SlackError, SlackWebClient


# --------------------------------------------------------------------------- #
# Minimal stubs (this file never imports the network).
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, data, *, status_code: int = 200, headers=None) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}


class _StubClient:
    """A slack_sdk.WebClient stand-in: each method returns its next queued item."""

    def __init__(self, **queues) -> None:
        self._queues = {name: list(q) for name, q in queues.items()}

    def __getattr__(self, name):
        def method(**kwargs):
            queue = self._queues.get(name)
            if not queue:
                raise AssertionError(f"no stubbed response for {name!r}")
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return method


class _IncompleteReadResponse:
    """A urlopen() result whose body read() raises http.client.IncompleteRead
    on the second chunk -- a truncated/interrupted body, which is NOT an OSError."""

    def __init__(self) -> None:
        self._n = 0

    def read(self, _size):
        self._n += 1
        if self._n == 1:
            return b"partial-bytes"
        raise http.client.IncompleteRead(b"partial-bytes", 4096)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


# --------------------------------------------------------------------------- #
# Finding 1 (cli): production `snipebot doctor` (online) never builds a Slack
# client from SLACK_BOT_TOKEN, so the whole section-5.2 Slack block is skipped.
# --------------------------------------------------------------------------- #

_DOCTOR_CFG = {
    "slack": {"channel": "C0MAINAA"},
    "timezone": "America/New_York",
    "semesters": [{"name": "fall", "start": "2020-01-01", "end": "2035-12-20"}],
    "rules": {"selfie_bonus": False},
    "players": {"extras": ["U0AAA001", "U0AAA002"]},
    "consent": {"veto": {"emoji": "x"}},
    "admins": ["U0ADMIN1"],
    "feedback": {"reactions": {}},
}


def test_doctor_online_never_builds_client_from_token(tmp_path: Path, monkeypatch, capsys):
    """spec/40-config-cli.md section 5.2: doctor's "against Slack" block "needs
    `SLACK_BOT_TOKEN`" and runs DOC-AUTH/DOC-SCOPES/DOC-CHANNEL-MEMBER, and
    section 6.1: "the CLI reads `SLACK_BOT_TOKEN` from the environment ... and
    passes it in" to `make_client`. In production `snipebot doctor` runs with
    `slack_factory=None`; `_cmd_doctor` forwards that None straight to
    `doctor.run`, which then sets `slack = None` and can never build a client
    from the token, so the entire Slack block degenerates to a single DOC-AUTH
    FAIL and `make_client` is never called even with a valid token present."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(_DOCTOR_CFG, sort_keys=False), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-present-and-valid")

    called: dict[str, int] = {"n": 0}

    def fake_make_client(token, **kwargs):
        called["n"] += 1
        # A client the Slack block could use; on current code it is never reached.
        return _StubClient(auth_test=[_Resp({"ok": True, "bot_id": "B1", "user_id": "U0BOT"})])

    monkeypatch.setattr(slack_io, "make_client", fake_make_client)

    main(["doctor", "--json", "--config", str(cfg), "--data-dir", str(data)])

    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip().startswith("{")
    ]
    auth = next(r for r in records if r["id"] == "DOC-AUTH")
    assert called["n"] >= 1, (
        "doctor online must build a Slack client from SLACK_BOT_TOKEN, but the CLI "
        "forwarded slack_factory=None and make_client was never called"
    )
    assert auth["detail"] != "no Slack client available"


# --------------------------------------------------------------------------- #
# Finding 2 (slack_io): a truncated response body (http.client.IncompleteRead)
# while streaming in fetch_file_bytes escapes the SlackError taxonomy.
# --------------------------------------------------------------------------- #

def test_fetch_file_bytes_incomplete_read_is_slack_error(monkeypatch):
    """spec/10-slack-io.md section 3: "Every failure is one of these classes;
    nothing else escapes a `SlackIO` method", and `SlackTransportError` covers a
    "connection reset ... or a response the client never received". A body cut
    short raises `http.client.IncompleteRead`, which is an `HTTPException`, NOT an
    `OSError`/`URLError`/`TimeoutError`, so the streaming loop's
    `except (URLError, TimeoutError, OSError)` never catches it and it escapes
    fetch_file_bytes raw, outside the taxonomy."""
    def fake_urlopen(request, timeout=None):
        return _IncompleteReadResponse()

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = SlackWebClient(_StubClient(), bot_token="xoxb-test")

    with pytest.raises(SlackError):
        client.fetch_file_bytes("https://files.slack.com/x")


# --------------------------------------------------------------------------- #
# Finding 4 (slack_io): an ok:true Web API response missing its documented result
# field makes the SlackIO method raise a bare KeyError, outside the taxonomy.
# --------------------------------------------------------------------------- #

def test_reactions_get_missing_message_is_slack_error():
    """spec/10-slack-io.md section 3: "Every failure is one of these classes;
    nothing else escapes a `SlackIO` method." `reactions_get` "Return[s] one
    message" via `data["message"]`; a malformed but ok:true response with no
    "message" key (a hostile/odd API payload) makes it raise a raw `KeyError`
    instead of a `SlackError`, so a non-taxonomy exception escapes the method."""
    client = _StubClient(reactions_get=[_Resp({"ok": True})])  # ok, but no "message"
    web = SlackWebClient(client)
    with pytest.raises(SlackError):
        web.reactions_get("C0MAINAA", "1758210000.000199")


# guard against an unused-import lint (only used by sibling suites)
_ = (types, sys)
