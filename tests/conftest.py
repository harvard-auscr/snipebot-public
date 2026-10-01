"""Shared fixtures. Kept deliberately minimal: other workstreams rely on these
two paths and must not need to edit this file."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def project_root() -> Path:
    """The repository root (the directory holding pyproject.toml)."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def fixtures_dir(project_root: Path) -> Path:
    """The test fixtures directory, tests/fixtures/."""
    return project_root / "tests" / "fixtures"
