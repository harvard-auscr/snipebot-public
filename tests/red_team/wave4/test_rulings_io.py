"""Wave 4 rulings on the Slack transport and doctor: regression tests for
E-W4-6 (fetch_file_bytes keeps the bot token on the requested https host) and
E-W4-7 (the emoji-existence seam is gone). Offline: urllib's opener is replaced
by one whose handlers never open a socket."""

from __future__ import annotations

import email.message
import io
import urllib.request
import urllib.response

import pytest

from snipebot.slack_io import SlackTransportError, SlackWebClient


class _RecordingHTTPS(urllib.request.HTTPSHandler):
    """Answers every https request 200 with a few bytes and records it."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, str | None]] = []

    def https_open(self, req):
        self.seen.append((req.host, req.get_header("Authorization")))
        resp = urllib.response.addinfourl(
            io.BytesIO(b"\xff\xd8bytes"), email.message.Message(), req.full_url, 200,
        )
        resp.msg = "OK"
        return resp


class _RecordingHTTP(urllib.request.HTTPHandler):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def http_open(self, req):
        self.seen.append(req.host)
        raise AssertionError("plain-http request attempted")


@pytest.fixture
def transports(monkeypatch):
    https, http = _RecordingHTTPS(), _RecordingHTTP()
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(https, http))
    return https, http


@pytest.mark.parametrize("url", [
    "http://fixture.invalid/files/F0FILE001/thumb_1024.jpg",
    "HTTP://fixture.invalid/files/F0FILE001/thumb_1024.jpg",
    "ftp://fixture.invalid/files/F0FILE001/thumb_1024.jpg",
    "file:///fixture.invalid/F0FILE001.jpg",
    "//fixture.invalid/files/F0FILE001/thumb_1024.jpg",
    "fixture.invalid/files/F0FILE001/thumb_1024.jpg",
])
def test_fetch_refuses_a_non_https_url_before_any_request(transports, url):
    """E-W4-6: a non-https rendition URL is refused with SlackTransportError and no
    request is made, so the bearer token never travels in clear."""
    https, http = transports
    client = SlackWebClient(object(), bot_token="xoxb-fixture-token")
    with pytest.raises(SlackTransportError):
        client.fetch_file_bytes(url)
    assert https.seen == [] and http.seen == []


def test_fetch_sends_the_token_to_the_requested_https_host(transports):
    """E-W4-6 positive control: an https URL is fetched with the bearer token on
    exactly the host it names."""
    https, _ = transports
    client = SlackWebClient(object(), bot_token="xoxb-fixture-token")
    body = client.fetch_file_bytes("HTTPS://fixture.invalid/files/F0FILE002/thumb_1024.jpg")
    assert body == b"\xff\xd8bytes"
    assert https.seen == [("fixture.invalid", "Bearer xoxb-fixture-token")]


def test_fetch_token_header_is_unredirected(transports, monkeypatch):
    """E-W4-6: the Authorization header is attached with add_unredirected_header,
    never as a regular header urllib would copy onto a redirected request."""
    added: list[str] = []
    real_add_header = urllib.request.Request.add_header

    def spy_add_header(self, key, val):
        added.append(key.lower())
        return real_add_header(self, key, val)

    monkeypatch.setattr(urllib.request.Request, "add_header", spy_add_header)
    client = SlackWebClient(object(), bot_token="xoxb-fixture-token")
    client.fetch_file_bytes("https://fixture.invalid/files/F0FILE003/thumb_1024.jpg")
    assert "authorization" not in added


def test_real_transport_has_no_emoji_list():
    """E-W4-7: DOC-EMOJI-EXISTS and the `emoji_list` transport method are removed
    (emoji.list lists custom emoji only and needs a scope the manifest never grants)."""
    import snipebot.doctor as doctor
    import snipebot.slack_io as slack_io

    assert not hasattr(SlackWebClient, "emoji_list")
    assert not hasattr(slack_io, "_EmojiListMixin")
    assert not hasattr(doctor, "_check_emoji_exists")
