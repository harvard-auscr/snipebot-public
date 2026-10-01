"""Privacy and robustness regressions for `snipebot review`: the local gallery and the
review folder.

Each test pins down one confirmed defect. Offline only: Slack is a small hand-written fake, the
detector is a fake with detect_boxes, and every gallery server binds 127.0.0.1 port 0
and is shut down by the fixture.
"""

from __future__ import annotations

import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from snipebot import review

REPO = Path(__file__).resolve().parents[3]
SECRET = "s3cr3tPathToken"
TS = "1700000000.000100"
FILE_ID = "F0IMG0001"
URL = "https://files.example.invalid/F0IMG0001/thumb_1024.jpg"


# --- a gallery harness ------------------------------------------------------------

class _Gallery:
    def __init__(self, server, thread, capsys):
        self.server = server
        self.thread = thread
        self._capsys = capsys
        self._err = []

    def stderr(self) -> str:
        self._err.append(self._capsys.readouterr().err)
        return "".join(self._err)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def raw(self, request: bytes, timeout: float = 5.0) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as s:
            s.sendall(request)
            chunks = []
            while True:
                try:
                    chunk = s.recv(65536)
                except (ConnectionResetError, ConnectionAbortedError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def wait_stderr(self, needle: str, seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if needle in self.stderr():
                return True
            time.sleep(0.05)
        return needle in self.stderr()


@pytest.fixture
def gallery(tmp_path, monkeypatch, capsys):
    """Start review.serve on 127.0.0.1:0 in a thread with a known secret; yields a
    starter taking the fetch function. A traceback the server thread prints is read
    back through capsys."""
    import http.server

    started = {}

    class Recording(http.server.ThreadingHTTPServer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started["server"] = self

    monkeypatch.setattr(http.server, "ThreadingHTTPServer", Recording)
    monkeypatch.setattr(review.secrets, "token_urlsafe", lambda n: SECRET)
    holder = {}

    def start(fetch):
        payload = [{"ts": TS, "when": "Tue Nov 14 22:13", "reasons": ["no_clear_face"],
                    "targets": 1, "clear": 0, "faint": 0, "label": None,
                    "link": "https://example.invalid/archives/C0CHAN/p1700000000000100",
                    "images": [{"id": FILE_ID, "w": 100, "h": 100, "boxes": []}]}]
        t = threading.Thread(
            target=review.serve,
            args=(tmp_path / "review", payload, fetch, {(TS, FILE_ID): URL}, "Photo review"),
            kwargs={"open_browser": False, "port": 0},
            daemon=True,
        )
        t.start()
        deadline = time.monotonic() + 5
        while "server" not in started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "server" in started, "serve() never bound a server"
        holder["g"] = _Gallery(started["server"], t, capsys)
        return holder["g"]

    yield start
    g = holder.get("g")
    if g is not None:
        g.server.shutdown()
        g.thread.join(timeout=5)


# --- P1: parse anomalies carrying user ids reach stderr ---------------------------

def test_P1_scan_lets_parse_anomalies_with_user_ids_reach_stderr(tmp_path):
    """P1: scan used to let parse()'s ParseAnomaly warnings reach Python's default
    warning display, so a text/blocks mention disagreement or a member-posted
    snipe_digest printed user IDs on stderr. The review tool's logs never carry them."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(REPO)!r})
        from snipebot import review

        photo = {{"id": "F0IMG0001", "mimetype": "image/jpeg", "name": "a.jpg",
                  "size": 10, "thumb_1024": "https://files.example.invalid/a.jpg"}}
        messages = [
            {{"ts": "1700000000.000100", "user": "U0SENDER1", "type": "message",
              "text": "<@U0TARGET1> got you", "blocks": [], "files": [photo]}},
            {{"ts": "1700000001.000100", "user": "U0POSTER1", "type": "message",
              "text": "hi", "metadata": {{"event_type": "snipe_digest",
                                          "event_payload": {{}}}}}},
        ]

        class Slack:
            def history(self, channel, oldest, latest):
                return list(messages)

            def fetch_file_bytes(self, url):
                return b"\\xff\\xd8\\xff fake jpeg"

        class Detector:
            def detect_boxes(self, data):
                return [(1, 2, 3, 4, 950)], 100, 100

        cache = {{"version": 1, "posts": {{}}}}
        review.scan(Slack(), "C0CHAN", "1690000000.000000", "1710000000.000000",
                    Detector(), cache, "U0BOT0001")
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          cwd=str(REPO), timeout=60)
    assert proc.returncode == 0, proc.stderr
    for uid in ("U0SENDER1", "U0TARGET1", "U0POSTER1"):
        assert uid not in proc.stderr, f"user id {uid} reached stderr:\n{proc.stderr}"


# --- P3: a cancelled image load prints a traceback ---------------------------------

def test_P3_cancelled_image_request_prints_a_traceback(gallery):
    """P3: a browser that cancels an in-flight img/<ts>/<file_id> request (lazy loading,
    a filter click) used to make the late reply raise ConnectionResetError into
    socketserver.handle_error: a full traceback on the owner's console in normal use."""
    go = threading.Event()

    def slow_fetch(url):
        go.wait(5)
        return b"\xff\xd8\xff" + b"\0" * (4 * 1024 * 1024)

    g = gallery(slow_fetch)
    s = socket.create_connection(("127.0.0.1", g.port), timeout=5)
    s.sendall(f"GET /{SECRET}/img/{TS}/{FILE_ID} HTTP/1.0\r\n\r\n".encode())
    time.sleep(0.3)  # the handler is now inside fetch
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()  # abortive close: the browser cancelled the image
    time.sleep(0.3)
    go.set()
    printed = g.wait_stderr("Traceback", seconds=3)
    assert not printed, f"traceback on stderr:\n{g.stderr()}"
