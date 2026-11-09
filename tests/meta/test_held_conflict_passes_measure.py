"""The held-conflict replay keeps every receipt and reports each pass's work."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_replay_rechecks_once_after_a_restart_then_takes_no_lease():
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'benchmarks' / 'held_conflict_passes.py'),
         '--repository', str(ROOT), '--tasks', '1', '--files', '12', '--dirs', '3',
         '--root-depth', '5', '--passes', '3'],
        capture_output=True, text=True, timeout=600, check=True)
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result['lib'] == str(ROOT / 'lib' / '__init__.py')
    assert result['held'] == [['blocked', 'conflict_inputs_changed']]
    assert [entry['claims_leases'] for entry in result['passes']] == [1, 0, 0]
    assert all(entry['cpu_s'] >= 0 and entry['wall_s'] >= 0 for entry in result['passes'])
