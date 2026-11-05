"""Standalone search must package the shared resource and retirement owners."""
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_packaged_search_runtime_is_current():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, 'scripts/gen_packaged_search_runtime.py', '--check'],
                            cwd=root, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
