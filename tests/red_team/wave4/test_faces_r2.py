"""Wave 4, round 2 -- breaks against the faces surface: snipebot/faces.py after the
round-1 repairs (the in-memory decoder allow-list, the pixel guard, the size padding).

Each test proves one defect and FAILS on the current code. Everything is offline: no
external program is ever run (the one decoder that would launch one is replaced by a
spy), image bytes are built in memory, and any file the code under test writes lands
under tmp_path.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")

from snipebot.faces import UndecodableImage, YuNetDetector  # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
_MODEL = str(_ROOT / "snipebot" / "models" / "face_detection_yunet_2023mar.onnx")


def _detector() -> YuNetDetector:
    return YuNetDetector(_MODEL, "0.9")


# --------------------------------------------------------------------------- #
# 1. The PIL fallback hands PostScript to an external renderer via a temp file
# --------------------------------------------------------------------------- #
_EPS = (
    b"%!PS-Adobe-3.0 EPSF-3.0\n"
    b"%%BoundingBox: 0 0 64 64\n"
    b"%%EndComments\n"
    b"newpath 0 0 moveto 64 64 lineto stroke\n"
    b"showpage\n"
    b"%%EOF\n"
)


def test_count_faces_never_writes_eps_bytes_to_disk(tmp_path, monkeypatch):
    """PLAN.md s1a (sibfam selfie bonus): the bot fetches each live image 'into memory
    (never to disk'; 40 s7.3: 'sibfam-tagged photos are face-counted in memory, never
    stored'; 10 s9 Decode: OpenCV first, then 'the HEIC/HEIF path via pillow-heif',
    else UndecodableImage.

    The round-1 repair sends every format outside the cv2 allow-list to the PIL
    fallback, assuming PIL decodes in memory. PIL's EPS plugin does not: on load it
    copies the whole input buffer into a tempfile.mkstemp() file and runs the
    Ghostscript binary on it (PIL.EpsImagePlugin.Ghostscript). The bytes come from
    url_private_download whenever an image/* upload has no thumbnails (10 s2 last
    resort), so a member who uploads PostScript under an image name gets the raw bytes
    written to the runner's disk and interpreted by an external program. Only JPEG and
    HEIC are named decode paths (10 s9); PostScript must be refused before any decoder.

    The Ghostscript subprocess call is replaced by a spy that records the file it was
    given; no external program runs. tempfile.tempdir points at tmp_path so the spill
    stays inside the test directory.
    """
    from PIL import EpsImagePlugin

    spilled: list[bytes] = []

    def spy_check_call(command, *args, **kwargs):
        infile = command[command.index("-f") + 1]
        spilled.append(Path(infile).read_bytes())
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(EpsImagePlugin, "gs_binary", "gs")
    monkeypatch.setattr(EpsImagePlugin, "has_ghostscript", lambda: True)
    monkeypatch.setattr(EpsImagePlugin.subprocess, "check_call", spy_check_call)

    with pytest.raises(UndecodableImage):
        _detector().count_faces(_EPS)
    assert spilled == [], "image bytes were written to a temp file for an external decoder"
