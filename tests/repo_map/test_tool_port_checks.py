"""Offline repository qualification checks follow real tool outcome semantics."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('script', [
    'check_tool_port.py', 'check_public_tool_port.py', 'check_publication_port.py',
    'check_task_indexes.py', 'check_bootstrap_epoch_task.py', 'check_loaded_epoch_task.py',
])
def test_repository_port_qualification(script, tmp_path):
    """Exercise success, foreign-root refusal, settlement and owned teardown."""
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join(filter(None, (
        str(ROOT), environment.get('PYTHONPATH'),
    )))
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'benchmarks/repository_index' / script)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
