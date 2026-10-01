"""LN-NOFLOAT: `float(` is never applied to a ts across snipebot/ and
tests/oracle/. The single permitted call site is faces.score_threshold in
snipebot/faces.py.

A ts is integer microseconds everywhere (snipebot.ts); a float() call in a
scanned module can only mean a ts was mangled through binary floating point,
which silently loses precision on a 17-digit value. The scan uses the Python
tokenizer, so `float(` mentioned in a docstring or comment does not count -- only
an actual call.
"""

from __future__ import annotations

import token as _token
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_FACES = (ROOT / "snipebot" / "faces.py").resolve()


def _scanned_files() -> list[Path]:
    roots = [ROOT / "snipebot", ROOT / "tests" / "oracle"]
    files: list[Path] = []
    for r in roots:
        if r.is_dir():
            files.extend(sorted(r.rglob("*.py")))
    return files


def _float_call_lines(path: Path) -> list[int]:
    """Line numbers of actual `float(` calls (NAME 'float' followed by '('),
    ignoring strings and comments."""
    with path.open("rb") as fh:
        toks = list(tokenize.tokenize(fh.readline))
    lines: list[int] = []
    for i, tok in enumerate(toks[:-1]):
        if tok.type == _token.NAME and tok.string == "float":
            nxt = toks[i + 1]
            if nxt.type == _token.OP and nxt.string == "(":
                lines.append(tok.start[0])
    return lines


def test_no_float_applied_to_ts() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        if path.resolve() == _FACES:
            continue  # the one documented exception (faces.score_threshold)
        for lineno in _float_call_lines(path):
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
    assert offenders == [], (
        "float( applied outside the permitted faces.score_threshold site: "
        + ", ".join(offenders)
    )
