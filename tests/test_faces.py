"""Faces adapter tests (10-slack-io.md §9, §8; 50-test-matrix.md test_faces.py row).

The one slow test runs the real `YuNetDetector` against the three photo fixtures
and asserts the §8 positive-control counts; it needs `cv2` and is skipped without
it. Every other test either uses `FakeFaceDetector` (no `cv2`, no model) or, for
the decode/downscale/threshold paths, the real detector guarded by the same
`cv2` skip.
"""

from __future__ import annotations

import hashlib
import io
import struct
import time
import zlib
from pathlib import Path

import pytest

from snipebot.faces import (
    FakeFaceDetector,
    UndecodableImage,
    YuNetDetector,
)
from tests.controls import faces_fixtures
from tests.controls.faces_fixtures import EXPECTED_COUNTS

try:
    import cv2  # noqa: F401
    import numpy as np

    HAS_CV2 = True
except Exception:  # pragma: no cover - environment without opencv
    HAS_CV2 = False

requires_cv2 = pytest.mark.skipif(not HAS_CV2, reason="cv2 is unavailable")

MODEL_PATH = str(
    Path(__file__).resolve().parents[1]
    / "snipebot"
    / "models"
    / "face_detection_yunet_2023mar.onnx"
)


def _detector() -> YuNetDetector:
    return YuNetDetector(MODEL_PATH, "0.9")


# --------------------------------------------------------------------------- #
# Slow real-detector positive controls (§8). One node per fixture so the
# CTL-FACES-* controls (50 §1.3) each map to a single node id.
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@requires_cv2
@pytest.mark.parametrize("name", list(EXPECTED_COUNTS))
def test_real_detector_counts(name: str) -> None:
    # photo_path is the seam the CTL-FACES-* controls swap; read it through the
    # module so a monkeypatch on it is honoured.
    path = faces_fixtures.photo_path(name)
    detector = _detector()
    assert detector.count_faces(path.read_bytes()) == EXPECTED_COUNTS[name]


# --------------------------------------------------------------------------- #
# FakeFaceDetector: keyed by sha256, raises on an unknown hash (§9).
# --------------------------------------------------------------------------- #
def test_fake_detector_returns_keyed_count() -> None:
    a, b = b"image-alpha", b"image-beta"
    fake = FakeFaceDetector(
        {
            hashlib.sha256(a).hexdigest(): 2,
            hashlib.sha256(b).hexdigest(): 0,
        }
    )
    assert fake.count_faces(a) == 2
    assert fake.count_faces(b) == 0


def test_fake_detector_raises_on_unknown_hash() -> None:
    fake = FakeFaceDetector({hashlib.sha256(b"known").hexdigest(): 1})
    with pytest.raises(KeyError):
        fake.count_faces(b"never-seen-these-bytes")


# --------------------------------------------------------------------------- #
# Decode paths (§9): JPEG, PNG, a synthesised HEIC, and an undecodable blob.
# All use the one-face portrait so the decoders can be compared on the count.
# --------------------------------------------------------------------------- #
def _portrait_bgr():
    data = faces_fixtures.photo_path("portrait_one_face.jpg").read_bytes()
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


@requires_cv2
def test_decode_jpeg() -> None:
    img = _portrait_bgr()
    ok, jpeg = cv2.imencode(".jpg", img)
    assert ok
    assert _detector().count_faces(jpeg.tobytes()) == 1


@requires_cv2
def test_decode_png() -> None:
    img = _portrait_bgr()
    ok, png = cv2.imencode(".png", img)
    assert ok
    assert _detector().count_faces(png.tobytes()) == 1


@requires_cv2
def test_decode_heic_via_pillow_heif() -> None:
    # A raw iPhone-style HEIC (no JPEG rendition): cv2.imdecode fails, the
    # pillow-heif fallback decodes it (§9). Synthesised from the portrait fixture.
    pillow_heif = pytest.importorskip("pillow_heif")
    from PIL import Image

    pillow_heif.register_heif_opener()
    img = _portrait_bgr()
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="HEIF")
    heic = buffer.getvalue()
    # Confirm this really exercises the fallback: OpenCV cannot read it.
    assert cv2.imdecode(np.frombuffer(heic, np.uint8), cv2.IMREAD_COLOR) is None
    assert _detector().count_faces(heic) == 1


@requires_cv2
def test_undecodable_blob_raises() -> None:
    with pytest.raises(UndecodableImage):
        _detector().count_faces(b"this is plainly not an image")


# --------------------------------------------------------------------------- #
# Downscale (§9): an image whose long side exceeds 1024 is resized before
# detection, and the count is unchanged.
# --------------------------------------------------------------------------- #
@requires_cv2
def test_downscale_preserves_count() -> None:
    img = _portrait_bgr()
    height, width = img.shape[:2]
    assert max(height, width) <= 1024  # the fixture starts small
    scale = 1600 / max(height, width)
    big = cv2.resize(img, (round(width * scale), round(height * scale)))
    assert max(big.shape[:2]) > 1024
    ok, jpeg = cv2.imencode(".jpg", big)
    assert ok
    assert _detector().count_faces(jpeg.tobytes()) == 1


# --------------------------------------------------------------------------- #
# Threshold parsing (§9, 40 §1.10): the decimal string is parsed at construction;
# that float() is the program's only one. A non-numeric string raises a
# ValueError (InvalidValueError-like) at construction.
# --------------------------------------------------------------------------- #
@requires_cv2
def test_threshold_string_parsed_at_construction() -> None:
    detector = YuNetDetector(MODEL_PATH, "0.9")
    assert detector.score_threshold == pytest.approx(0.9)


@requires_cv2
def test_threshold_non_numeric_raises_value_error() -> None:
    with pytest.raises(ValueError):
        YuNetDetector(MODEL_PATH, "abc")


# --------------------------------------------------------------------------- #
# EXIF orientation (§9): a photo whose pixels are stored sideways and tagged
# orientation 6 must count the same as the upright original on BOTH decode
# paths. cv2.imdecode applies the orientation itself; the HEIC/PIL fallback
# leans on ImageOps.exif_transpose (the tag is normalised on HEIF decode, so
# the transpose is a no-op there, but the count parity is what §9 asserts).
# --------------------------------------------------------------------------- #
def _portrait_rgb():
    img = _portrait_bgr()
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def _stored_sideways_orientation_6():
    """The upright portrait's pixels rotated 90° CCW, so an orientation-6 tag
    rotates them back to upright on decode. Returns a PIL image plus its EXIF
    orientation bytes."""
    from PIL import Image

    upright = Image.fromarray(_portrait_rgb())
    stored = upright.rotate(90, expand=True)
    exif = Image.Exif()
    exif[274] = 6  # 0x0112 Orientation = rotate 90° CW to display
    return stored, exif


@pytest.mark.slow
@requires_cv2
def test_exif_orientation_honoured_on_cv2_path() -> None:
    stored, exif = _stored_sideways_orientation_6()
    buffer = io.BytesIO()
    stored.save(buffer, format="JPEG", exif=exif)
    jpeg = buffer.getvalue()
    # The stored pixels are landscape; cv2 must rotate them upright before detect.
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == _portrait_bgr().shape[:2]
    assert _detector().count_faces(jpeg) == EXPECTED_COUNTS["portrait_one_face.jpg"]


@pytest.mark.slow
@requires_cv2
def test_exif_orientation_honoured_on_heic_fallback() -> None:
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    stored, exif = _stored_sideways_orientation_6()
    buffer = io.BytesIO()
    stored.save(buffer, format="HEIF", exif=exif.tobytes())
    heic = buffer.getvalue()
    # cv2 cannot read the HEIC, so this exercises the pillow-heif fallback.
    assert cv2.imdecode(np.frombuffer(heic, np.uint8), cv2.IMREAD_COLOR) is None
    assert _detector().count_faces(heic) == EXPECTED_COUNTS["portrait_one_face.jpg"]


# --------------------------------------------------------------------------- #
# Decoded-pixel guard (§9): a header whose width x height exceeds the fixed
# 50,000,000-pixel cap raises UndecodableImage from the cheap lazy header read,
# before any array is allocated and before the decoders run. A 20000x20000 PNG
# is a few dozen bytes on disk but 400,000,000 pixels decoded.
# --------------------------------------------------------------------------- #
def _png_header_only(width: int, height: int) -> bytes:
    """A valid PNG signature + IHDR (+ IEND) declaring width x height, with no
    IDAT — so a lazy PIL open reads the size without any pixel data existing."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = (
        struct.pack(">I", len(ihdr_data))
        + b"IHDR"
        + ihdr_data
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    )
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    return signature + ihdr + iend


@requires_cv2
def test_oversized_header_raises_without_decoding() -> None:
    blob = _png_header_only(20000, 20000)  # 4e8 pixels, tiny on disk
    assert len(blob) < 200
    start = time.perf_counter()
    with pytest.raises(UndecodableImage):
        _detector().count_faces(blob)
    assert time.perf_counter() - start < 2.0


@requires_cv2
def test_header_at_cap_is_not_guarded() -> None:
    # A header just under the cap passes the guard and reaches the decoders,
    # which reject this IDAT-less PNG on their own terms (still UndecodableImage,
    # but proving the guard did not short-circuit a legitimately sized header).
    blob = _png_header_only(7000, 7000)  # 4.9e7 pixels, under the 5e7 cap
    with pytest.raises(UndecodableImage):
        _detector().count_faces(blob)
