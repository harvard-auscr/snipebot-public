"""Wave 3, round 3 -- INVARIANTS breaks against snipebot/faces.py.

Round 3 theme: idempotence, determinism, fake/real parity, no-secrets, no-network.

RESULT: no surviving finding. Every invariant probed against the *real* YuNet model
(snipebot/models/face_detection_yunet_2023mar.onnx) and the three positive-control
fixtures held; each candidate red test PASSED on the current code and was therefore
deleted (a passing test is not a finding). This file is intentionally empty of tests;
the negative result and two spec-wording issues are reported in the structured output.

What was exercised empirically (all confirmed correct, so not asserted here):
- Positive controls at the default threshold "0.9": selfie=2, portrait=1, landscape=0
  (spec/10-slack-io.md s8 table, lines 922-926).
- Determinism / purity of image_bytes (spec/10-slack-io.md s9 "Determinism",
  lines 1008-1011): identical bytes -> identical count across two YuNetDetector
  instances, under idempotent re-calls, and under interleaving with other-sized images
  on a shared lazily-built detector.
- Decode taxonomy (spec/10-slack-io.md s9 "Decode", lines 998-1002 + the UndecodableImage
  docstring): zero-byte, single-byte, magic-only, and truncated JPEG bytes (any cut, even
  1 byte) all raise the typed UndecodableImage; a crafted PNG bomb (30000x30000 header,
  truncated IDAT) raises UndecodableImage; realistic formats (grayscale JPEG, CMYK JPEG,
  RGBA PNG, 16-bit PNG, WebP, first-frame GIF, pillow-heif HEIC) all decode to the correct
  count of 1 on the portrait subject.
- No-secrets (rules: logs/stored data hold user IDs only): the sole exception text is the
  constant "no decoder read the image bytes"; no path, filename, byte content or token.
- No-network: faces.py imports only hashlib, cv2, numpy, PIL, pillow-heif; the model is a
  local vendored ONNX file, CPU backend; nothing dials out.
- Model sidecar integrity: snipebot/models/face_detection_yunet_2023mar.onnx.sha256 equals
  the SHA-256 of the .onnx file.
"""

from __future__ import annotations
