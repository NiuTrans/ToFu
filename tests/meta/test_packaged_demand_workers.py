"""The private standalone worker artifact must derive from one maintained source."""
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_packaged_worker_generation_is_current():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / 'scripts/gen_packaged_demand_workers.py'), '--check'],
        cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
