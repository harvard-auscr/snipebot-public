"""Test-only mirror of `slack-app-manifest.yaml`'s bot scopes (40-config-cli.md
section 7.3), used to build a "fully-granted" fake token for `test_doctor.py`'s
DOC-SCOPES PASS scenario.

Kept as data independent of `snipebot.doctor`'s own manifest-reading path
(`doctor._read_manifest_scopes`, which reads the real committed file) so
`CTL-DOCTOR-SCOPE` (50-test-matrix.md section 2.7; `tests/controls/registry.py`)
can drop a scope from what a test token was *granted* without touching what
`doctor` itself considers *required* -- the mismatch is exactly what the control
exercises.
"""

from __future__ import annotations

FULL_BOT_SCOPES: tuple[str, ...] = (
    "channels:history",
    "channels:read",
    "users:read",
    "reactions:write",
    "reactions:read",
    "chat:write",
    "files:read",
)


def full_granted_scopes() -> tuple[str, ...]:
    """The scopes a correctly-installed bot token carries: every scope the
    manifest declares. `CTL-DOCTOR-SCOPE` monkeypatches this function."""
    return FULL_BOT_SCOPES
