"""Face detector adapter: the one place OpenCV is imported. sync.py never imports cv2;
it receives a FaceDetector by injection exactly as it receives the SlackIO client. The
YuNetDetector's body (the cv2 import, decode and detect logic) follows in a later revision
of this module; this file declares the constructor and raises until it is filled in.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Callable, Mapping
from typing import Protocol


class FaceDetector(Protocol):
    def count_faces(self, image_bytes: bytes) -> int: ...


class LazyDetector:  # satisfies FaceDetector; builds its backing detector on first use
    """Defers building the backing FaceDetector (and, for YuNetDetector, importing cv2)
    until the first count_faces call. A sync run whose rules never put selfie_bonus in
    force never counts a face, so it constructs nothing and OpenCV stays unimported
    (10 §9)."""

    def __init__(self, build: Callable[[], FaceDetector]) -> None:
        self._build = build
        self._detector: FaceDetector | None = None

    def count_faces(self, image_bytes: bytes) -> int:
        if self._detector is None:
            self._detector = self._build()
        return self._detector.count_faces(image_bytes)


class UndecodableImage(Exception):
    """count_faces was given bytes no decoder accepts (corrupt, truncated by the
    fetch_truncate fault, or an unsupported/half-supported format). Raised, not returned,
    so a real face count of 0 (a landscape) is never confused with 'could not read it'.
    The sync faces block (20 §5) tolerates it exactly like a fetch fault: no count for that
    image, run continues, detect_attempts advances if the row ends uncounted."""


_MAX_SIDE = 1024  # detection input is downscaled so the longer side is at most this
_MAX_PIXELS = 50_000_000  # decoded-pixel guard: a header above this raises (10 §9)
_MIN_SIDE = 64  # a detection input side shorter than this is padded up to it (see count_faces)


def _in_memory_cv2_format(image_bytes: bytes) -> bool:
    """True for the formats cv2.imdecode reads straight from memory (JPEG, PNG, WebP,
    TIFF, BMP, GIF). Any other format would make OpenCV spill the buffer to a temp
    file on disk before decoding, so it goes to the in-memory PIL path instead."""
    head = image_bytes[:12]
    return (
        head.startswith(b"\xff\xd8\xff")  # JPEG
        or head.startswith(b"\x89PNG")  # PNG
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")  # WebP
        or head.startswith((b"II*\x00", b"MM\x00*"))  # TIFF
        or head.startswith(b"BM")  # BMP
        or head.startswith((b"GIF87a", b"GIF89a"))  # GIF
    )

# The only PIL formats ever opened: the in-memory cv2 set (header guard) and the
# HEIC/HEIF path (decode fallback, 10 §9). Any other Pillow plugin (EPS spills the
# buffer to a temp file and runs an external interpreter) is refused before it parses.
_GUARD_PIL_FORMATS = ["JPEG", "PNG", "WEBP", "TIFF", "BMP", "GIF", "HEIF"]
_FALLBACK_PIL_FORMATS = ["HEIF"]

_heif_registered = False


def _register_heif_once() -> None:
    """Register pillow-heif's HEIF opener with PIL exactly once (10 §9 decode)."""
    global _heif_registered
    if not _heif_registered:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _heif_registered = True


class YuNetDetector:  # satisfies FaceDetector; imports cv2
    def __init__(self, model_path: str, score_threshold: str) -> None:
        # score_threshold arrives from FacesConfig as the decimal STRING (40 §1.10);
        # this is the single float() applied to configuration (10 §9, LN-NOFLOAT).
        self.model_path = model_path
        self.score_threshold = float(score_threshold)
        import cv2  # noqa: F401  the one place OpenCV is imported (10 §9)

        self._detector = None  # the FaceDetectorYN is built lazily on first count

    def _load(self):
        """Build the cv2.FaceDetectorYN once, from the vendored model bytes."""
        if self._detector is None:
            import cv2

            # CPU backend; input size is set per image before each detect call.
            self._detector = cv2.FaceDetectorYN.create(
                self.model_path, "", (320, 320), self.score_threshold
            )
        return self._detector

    def count_faces(self, image_bytes: bytes) -> int:
        faces, _width, _height = self._sane_faces(image_bytes)
        return 0 if faces is None else len(faces)

    def detect_boxes(self, image_bytes: bytes) -> tuple[list[tuple[int, int, int, int, int]], int, int]:
        """The sane boxes behind count_faces, for the local review tool: ([(x, y, w, h,
        score_milli)], frame_width, frame_height). Coordinates are integer pixels of the
        detection frame (downscaled, maybe padded); score_milli is the score x 1000,
        floored."""
        faces, width, height = self._sane_faces(image_bytes)
        boxes = []
        if faces is not None:
            for f in faces:
                x, y, w, h = (int(round(v)) for v in f[:4])
                # floor, never round: a score just under the threshold must not
                # be promoted to it (0.8996 is 899, not the live 900).
                boxes.append((x, y, w, h, math.floor(f[-1] * 1000)))
        return boxes, width, height

    def _sane_faces(self, image_bytes: bytes):
        """(rows of the detector output that are sane boxes, or None; frame width, height)."""
        import cv2

        detector = self._load()
        image = self._decode(image_bytes)
        height, width = image.shape[:2]
        longest = max(height, width)
        if longest > _MAX_SIDE:  # downscale only; never upscale, aspect preserved
            scale = _MAX_SIDE / longest
            new_w = max(1, round(width * scale))
            new_h = max(1, round(height * scale))
            image = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
            height, width = new_h, new_w
        # A side of 32 px or less leaves FaceDetectorYN's output partly unfilled
        # (phantom score-1.0 boxes of zero size at garbage coordinates); pad such a
        # side with a black border so the detector never runs in that regime.
        pad_h = max(0, _MIN_SIDE - height)
        pad_w = max(0, _MIN_SIDE - width)
        if pad_h or pad_w:
            image = cv2.copyMakeBorder(
                image, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=(0, 0, 0)
            )
            height, width = height + pad_h, width + pad_w
        detector.setInputSize((width, height))
        _retval, faces = detector.detect(image)
        if faces is None:
            return None, width, height
        # Count only sane boxes: finite, positive size, overlapping the image.
        import numpy as np

        boxes = faces[:, :4]
        sane = (
            np.isfinite(boxes).all(axis=1)
            & (boxes[:, 2] > 0)
            & (boxes[:, 3] > 0)
            & (boxes[:, 0] < width)
            & (boxes[:, 1] < height)
            & (boxes[:, 0] + boxes[:, 2] > 0)
            & (boxes[:, 1] + boxes[:, 3] > 0)
        )
        return faces[sane], width, height

    @staticmethod
    def _guard_pixels(image_bytes: bytes) -> None:
        """Raise UndecodableImage when the header's width x height exceeds
        _MAX_PIXELS, read cheaply from a lazy PIL open before any decode so a
        decompression bomb never allocates its array (10 §9). max_image_bytes
        bounds the fetched bytes only; this bounds the decoded pixels. A header
        PIL cannot read is refused: its pixel count is unknown, so it raises
        UndecodableImage rather than reaching a decoder."""
        import io

        from PIL import Image

        _register_heif_once()
        # PIL's own decompression-bomb check would raise on a huge header before
        # this guard could; disable it here (restored in finally) so the header's
        # size is always readable and this fixed cap is the one that decides.
        previous_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None
        try:
            with Image.open(io.BytesIO(image_bytes), formats=_GUARD_PIL_FORMATS) as handle:
                width, height = handle.size
        except Exception as exc:
            # Pillow's PNG plugin parses every ancillary chunk before IDAT and
            # rejects some that libpng only skips (oversized text/ICC chunks, a
            # bad ancillary CRC). IHDR is always the first chunk and is what the
            # decoder sizes by, so a PNG's pixel count is still known from it.
            if (
                image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
                and image_bytes[12:16] == b"IHDR"
                and len(image_bytes) >= 24
            ):
                width, height = struct.unpack(">II", image_bytes[16:24])
            else:
                raise UndecodableImage("image header unreadable; pixel count unknown") from exc
        finally:
            Image.MAX_IMAGE_PIXELS = previous_limit
        if width * height > _MAX_PIXELS:
            raise UndecodableImage(
                f"decoded pixels {width * height} exceed the {_MAX_PIXELS} cap"
            )

    def _decode(self, image_bytes: bytes):
        """Bytes -> a BGR uint8 array; OpenCV for the formats it decodes in memory,
        PIL (pillow-heif, HEIC/HEIF only) for the rest, else raise. Nothing is ever
        written to disk: a format OpenCV would spill to a temp file skips OpenCV.

        EXIF orientation is honoured on both paths (10 §9): cv2.imdecode's default
        applies it, and the HEIC/PIL fallback applies PIL.ImageOps.exif_transpose
        before conversion. A decoded-pixel guard runs first (see _guard_pixels)."""
        import cv2
        import numpy as np

        self._guard_pixels(image_bytes)

        image = None
        if _in_memory_cv2_format(image_bytes):
            buffer = np.frombuffer(image_bytes, dtype=np.uint8)
            try:
                image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
            except cv2.error:  # empty/assertion-failing buffer: a failed decode
                image = None
        if image is not None:
            return image
        try:  # HEIC/HEIF fallback: an iPhone original with no JPEG thumb (10 §9)
            import io

            from PIL import Image, ImageOps

            _register_heif_once()
            with Image.open(io.BytesIO(image_bytes), formats=_FALLBACK_PIL_FORMATS) as handle:
                upright = ImageOps.exif_transpose(handle)  # honour EXIF orientation
                rgb = np.asarray(upright.convert("RGB"))
        except Exception as exc:
            raise UndecodableImage("no decoder read the image bytes") from exc
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


class FakeFaceDetector:  # satisfies FaceDetector; no cv2, no model
    def __init__(self, counts: Mapping[str, int]) -> None:  # sha256 hex -> face count
        self.counts = counts

    def count_faces(self, image_bytes: bytes) -> int:
        h = hashlib.sha256(image_bytes).hexdigest()
        try:
            return self.counts[h]
        except KeyError:
            raise  # unknown bytes: fail loudly
