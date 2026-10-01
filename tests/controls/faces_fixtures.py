"""Fixture-path seam for the faces positive controls (50-test-matrix.md §1.3,
CTL-FACES-*).

The slow real-detector test resolves each photo through `photo_path(name)` here,
so a control turns a fixture red by monkeypatching this one function to hand back
a different file. Nothing in this module imports cv2 or reads image bytes; it only
maps a fixture name to its path and records the expected face count.
"""

from __future__ import annotations

from pathlib import Path

PHOTOS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "photos"

# fixture filename -> expected count_faces at the default threshold (10 §8).
EXPECTED_COUNTS: dict[str, int] = {
    "selfie_two_faces.jpg": 2,
    "portrait_one_face.jpg": 1,
    "landscape_no_face.jpg": 0,
}


def photo_path(name: str) -> Path:
    """Path to the named photo fixture. The single seam the CTL-FACES-* controls
    swap so the real detector reads a different file (50 §1.3)."""
    return PHOTOS_DIR / name
