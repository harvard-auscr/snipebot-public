"""Wave 3, round 1 -- SPEC CONFORMANCE breaks against snipebot/faces.py.

Each test proves one violation of the face-detector adapter contract and FAILS on
the current code. Tests here need cv2; they are skipped when it is unavailable
(the same import guard the spec attaches to the real-detector path, 10 s9 / 40 s5).
"""

from __future__ import annotations

import pytest

cv2 = pytest.importorskip("cv2")

from snipebot.faces import UndecodableImage, YuNetDetector

_MODEL = "snipebot/models/face_detection_yunet_2023mar.onnx"


def _detector() -> YuNetDetector:
    return YuNetDetector(_MODEL, "0.9")


def test_empty_bytes_raise_undecodable_not_cv2_error():
    """spec/10-slack-io.md s9 (Decode) -- 'bytes that still will not decode raise
    UndecodableImage'; the UndecodableImage docstring: 'count_faces was given bytes
    no decoder accepts (corrupt, truncated by the fetch_truncate fault, ...). Raised,
    not returned'. An empty rendition is undecodable, so count_faces must raise the
    typed UndecodableImage that the sync faces block tolerates (20 s5). On current
    code cv2.imdecode asserts !buf.empty() and a raw cv2.error escapes _decode
    (faces.py:85 is not wrapped), which sync's except-list (sync.py:488-490) does not
    catch -- crashing the run instead of leaving the row uncounted.
    """
    with pytest.raises(UndecodableImage):
        _detector().count_faces(b"")
