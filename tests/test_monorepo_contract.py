"""Executable specification for first-party monorepo boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_monorepo_contract_guard_passes():
    result = subprocess.run(
        [sys.executable, "scripts/check_monorepo.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tofu-agent, tofu-search, tofu-trading" in result.stdout
