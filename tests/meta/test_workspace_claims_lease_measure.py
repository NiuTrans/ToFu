"""The claims-lease measurement admits the complete claim set and leaves nothing held."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_measurement_reports_held_keys_and_releases_everything():
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'benchmarks' / 'workspace_claims_lease.py'),
         '--repository', str(ROOT), '--files', '12', '--dirs', '3', '--dir-depth', '2',
         '--root-depth', '5', '--samples', '2'],
        capture_output=True, text=True, timeout=300, check=True)
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result['lib'] == str(ROOT / 'lib' / '__init__.py')
    assert result['files'] == 12
    # Every file claim adds its own target plus shared ancestor barriers.
    keys, = result['registry_keys_while_held']
    assert keys > 12
    assert len(result['cpu_seconds']) == len(result['wall_seconds']) == 2
