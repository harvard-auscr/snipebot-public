"""Wave 4, round 3 -- breaks against the faces surface: snipebot/faces.py.

Everything is offline and in memory: image bytes are built from the committed photo
fixtures, nothing is fetched and nothing is written to disk.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from snipebot.faces import UndecodableImage, YuNetDetector  # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
_MODEL = str(_ROOT / "snipebot" / "models" / "face_detection_yunet_2023mar.onnx")
_PORTRAIT = _ROOT / "tests" / "fixtures" / "photos" / "portrait_one_face.jpg"


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _with_chunk_after_ihdr(png: bytes, chunk: bytes) -> bytes:
    # 8-byte signature + the 25-byte IHDR chunk, then the extra ancillary chunk.
    return png[:33] + chunk + png[33:]


def test_pixel_guard_refuses_decodable_png_with_metadata_pillow_rejects():
    """10 s9 YuNetDetector: the pixel guard only reads the header's width x height
    and raises above 50,000,000 pixels; 'OpenCV imdecode on image_bytes first ...
    bytes that still will not decode raise UndecodableImage'.

    The round-1 repair made _guard_pixels refuse ANY header Pillow cannot parse.
    Pillow's PNG plugin parses every ancillary chunk before IDAT and rejects
    perfectly decodable files: a compressed text chunk (zTXt, or an iTXt XMP packet)
    or an iCCP profile that inflates past PngImagePlugin.MAX_TEXT_CHUNK (1 MB), or an
    ancillary chunk with a bad CRC (libpng only warns and skips it). OpenCV decodes
    all three in memory and the photo has one face, yet count_faces raises
    UndecodableImage for a 602 x 768 image far under the pixel cap. In sync the
    image never gets a count, detect_attempts burns to max_attempts and the row ends
    AMBIGUOUS, so a sibling selfie loses its bonus although it decodes.
    """
    image = cv2.imread(str(_PORTRAIT))
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    png = encoded.tobytes()
    detector = YuNetDetector(_MODEL, "0.9")
    assert detector.count_faces(png) == 1  # control: the plain PNG counts one face

    inflates_past_1mb = zlib.compress(b"x" * 2_000_000, 9)
    bad_crc = _chunk(b"tEXt", b"Comment\x00photo-1")[:-4] + b"\x00\x00\x00\x00"
    variants = {
        "zTXt": _chunk(b"zTXt", b"Comment\x00\x00" + inflates_past_1mb),
        "iCCP": _chunk(b"iCCP", b"icc\x00\x00" + inflates_past_1mb),
        "tEXt-bad-crc": bad_crc,
    }
    outcomes = {}
    for label, extra in variants.items():
        blob = _with_chunk_after_ihdr(png, extra)
        decoded = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None and decoded.shape[:2] == (768, 602), label
        try:
            outcomes[label] = detector.count_faces(blob)
        except UndecodableImage:
            outcomes[label] = "undecodable"
    assert outcomes == {label: 1 for label in variants}, outcomes
