"""Wave 4, round 1 -- breaks against the faces surface: snipebot/faces.py and the
rendition fetch path (slack_io.SlackWebClient.fetch_file_bytes).

Each test proves one defect and FAILS on the current code. Everything is offline:
the HTTP transport is a fake handler inside urllib's own opener chain, image bytes
are built in memory, and the one subprocess test only decodes a few hundred bytes.
"""

from __future__ import annotations

import email.message
import io
import os
import subprocess
import sys
import urllib.request
import urllib.response
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

import snipebot.slack_io as slack_io  # noqa: E402
from snipebot.faces import UndecodableImage, YuNetDetector  # noqa: E402
from snipebot.slack_io import SlackHTTPError, SlackWebClient  # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
_MODEL = str(_ROOT / "snipebot" / "models" / "face_detection_yunet_2023mar.onnx")


def _detector() -> YuNetDetector:
    return YuNetDetector(_MODEL, "0.9")


def _uniform_jpeg(width: int, height: int) -> bytes:
    image = np.full((height, width, 3), (90, 100, 120), np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def _hdr_header(width: int, height: int) -> bytes:
    """A Radiance RGBE header declaring width x height (the pixel data is absent)."""
    return (
        b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n"
        + f"-Y {height} +X {width}\n".encode("ascii")
    )


# --------------------------------------------------------------------------- #
# 1. Featureless thin/small inputs yield phantom detections
# --------------------------------------------------------------------------- #
def test_featureless_short_image_counts_no_face():
    """10 s9 YuNetDetector (Determinism; count_faces 'returns the number of faces';
    UndecodableImage: 'a real face count of 0 (a landscape)'); 10 s8 photo positive
    controls (a no-face image counts 0).

    A plain single-colour image cannot contain a face, yet when its (downscaled)
    height is 32 px or less cv2.FaceDetectorYN reports a detection with score 1.0,
    zero width/height and coordinates like -8.7e27 or +/-inf (read from an unfilled
    output buffer), and faces.py counts it: count_faces returns faces.shape[0] with no
    sanity check on the boxes and no minimum input size. A wide panorama (for example
    4000 x 120, downscaled to 1024 x 31) is the real-world trigger: the phantom adds a
    face, so a one-face photo can read as a two-face sibling selfie (wrong score). The
    phantom also comes and goes between processes for the same bytes, breaking the s9
    'identical bytes always yield the identical count' guarantee that lets
    rendition_hash double as a repost key.
    """
    detector = _detector()
    counts = {
        (w, h): detector.count_faces(_uniform_jpeg(w, h))
        for w in (256, 1024)
        for h in (8, 16, 24, 32)
    }
    assert counts == {key: 0 for key in counts}, counts


# --------------------------------------------------------------------------- #
# 2. Bearer token forwarded across a redirect to another host
# --------------------------------------------------------------------------- #
class _RedirectingHTTPS(urllib.request.HTTPSHandler):
    """A fake HTTPS transport inside urllib's real opener chain: the first host
    answers 302 to a different host, which answers 200. Records every request's
    Authorization header per host. No socket is ever opened."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, str | None]] = []

    def https_open(self, req):
        host = req.host
        self.seen.append((host, req.get_header("Authorization")))
        headers = email.message.Message()
        if host == "fixture.invalid":
            headers["Location"] = "https://elsewhere.fixture.invalid/photo-1.jpg"
            resp = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
            resp.msg = "Found"
            return resp
        resp = urllib.response.addinfourl(io.BytesIO(b"\xff\xd8bytes"), headers, req.full_url, 200)
        resp.msg = "OK"
        return resp


class _NoPlainHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):  # never touch a socket, even on a downgrade redirect
        raise AssertionError("plain-http request attempted")


def test_fetch_does_not_forward_bot_token_to_redirect_host(monkeypatch):
    """40 s6.1 env table: SLACK_BOT_TOKEN is 'the only production secret'; 40 s4 (make_client):
    'the token flows only through this call'; 20 s2 table: fetch_file_bytes is
    'GET <url> with Authorization: Bearer <bot token>' -- to that url, not to wherever
    it redirects.

    fetch_file_bytes builds urllib.request.Request(url, headers={"Authorization": ...}).
    Headers passed that way are copied onto every redirected request by urllib's
    HTTPRedirectHandler, whatever the new host (and even on an https -> http
    downgrade). So a 302 from the file host hands the xoxb bot token to a foreign
    host. The header must be added with add_unredirected_header (or redirects refused /
    restricted to the original host).
    """
    transport = _RedirectingHTTPS()
    opener = urllib.request.build_opener(transport, _NoPlainHTTP())
    monkeypatch.setattr(urllib.request, "_opener", opener)
    client = SlackWebClient(object(), bot_token="xoxb-fixture-token")
    try:
        client.fetch_file_bytes("https://fixture.invalid/files/F0FILE001/thumb_1024.jpg")
    except SlackHTTPError:
        pass  # refusing the redirect is an acceptable fix
    assert transport.seen[0] == ("fixture.invalid", "Bearer xoxb-fixture-token")
    leaked = [(h, a) for h, a in transport.seen if h != "fixture.invalid" and a]
    assert leaked == [], leaked


# --------------------------------------------------------------------------- #
# 3. Pixel guard is blind to a format PIL cannot parse but OpenCV decodes
# --------------------------------------------------------------------------- #
def test_pixel_guard_covers_radiance_hdr_header(monkeypatch):
    """10 s9 YuNetDetector (Pixel guard): 'Above 50,000,000 decoded pixels -- a fixed
    constant, never configurable -- count_faces raises UndecodableImage'; the guard
    runs 'before decoding'. 50 test matrix L3-FA-fetch-oversize mutant: 'decode the
    oversize bytes anyway -> unbounded memory'.

    _guard_pixels reads the header with PIL and silently returns when PIL cannot parse
    it. OpenCV decodes Radiance .hdr (RGBE, run-length compressed) but PIL cannot open
    it, so an 8000 x 8000 (64 M pixel) .hdr header sails past the guard straight into
    cv2.imdecode, which would allocate ~770 MB of float32 before converting; a few MB
    of RLE data under faces.max_image_bytes buys ~1 G pixels. The spy stands in for
    the decoder: it must never be reached for a header over the cap.
    """
    decoded: list[int] = []

    def spy_imdecode(buf, flags):
        decoded.append(len(buf))
        raise AssertionError("decoder reached for a header above the 50M-pixel cap")

    monkeypatch.setattr(cv2, "imdecode", spy_imdecode)
    with pytest.raises(UndecodableImage):
        _detector().count_faces(_hdr_header(8000, 8000))
    assert decoded == []


# --------------------------------------------------------------------------- #
# 4. Image bytes spilled to a temp file on disk by OpenCV
# --------------------------------------------------------------------------- #
_CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from snipebot.faces import UndecodableImage, YuNetDetector
data = open(sys.argv[2], "rb").read()
try:
    print("COUNT", YuNetDetector(sys.argv[3], "0.9").count_faces(data))
except UndecodableImage:
    print("UNDECODABLE")
"""


def test_count_faces_never_spills_image_bytes_to_disk(tmp_path):
    """PLAN.md s1a (sibfam selfie bonus): 'the bot fetches each live image into memory (never to disk,
    ...'; 40 s7.3: 'sibfam-tagged photos are face-counted in memory, never
    stored'.

    count_faces hands every byte string to cv2.imdecode. For decoders without
    in-memory support (Radiance .hdr among them) OpenCV writes the whole buffer to a
    temp file (OPENCV_TEMP_PATH, else the system temp dir), decodes it from disk and
    deletes it. Proof without watching the file system: run the same count twice in a
    fresh interpreter, once with a writable OPENCV_TEMP_PATH and once with one that
    does not exist. An in-memory decoder gives the same outcome both times; the
    current code counts the image in the first run and reports it undecodable in the
    second, because the disk write failed.
    """
    image = np.zeros((40, 48, 3), np.float32)
    image[:, :, 1] = 0.8
    ok, encoded = cv2.imencode(".hdr", image)
    assert ok
    photo = tmp_path / "photo-1.hdr"  # test input written by the test itself
    photo.write_bytes(encoded.tobytes())

    env = {k: v for k, v in os.environ.items() if not k.startswith("SLACK_")}
    writable = tmp_path / "ocv-temp"
    writable.mkdir()
    outcomes = {}
    for label, temp_dir in (("writable", writable), ("missing", tmp_path / "no" / "such")):
        run_env = dict(env, OPENCV_TEMP_PATH=str(temp_dir))
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, str(_ROOT), str(photo), _MODEL],
            env=run_env, capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr[-500:]
        outcomes[label] = proc.stdout.strip().splitlines()[-1]
    assert outcomes["writable"] == outcomes["missing"], outcomes
