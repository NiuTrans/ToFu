"""Developer gates must not traverse Tofu-owned mutable runtime state."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from lib.runtime_layout import INSTALL_STATE


pytestmark = pytest.mark.unit
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_ruff_excludes_only_the_authoritative_intree_undo_store():
    configuration = tomllib.loads(
        (_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    excluded = configuration["tool"]["ruff"]["extend-exclude"]
    runtime_state_paths = {entry.prefix.rstrip("/") for entry in INSTALL_STATE}

    assert "lib/.project_sessions" in excluded
    assert "lib/.project_sessions" in runtime_state_paths
    assert "**/.project_sessions" not in excluded
    assert "**/.project_sessions/**" not in excluded
