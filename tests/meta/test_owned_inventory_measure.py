"""Inventory measurement refuses incomparable evidence and retains raw clocks."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def run_measure(*extra):
    return subprocess.run([sys.executable, str(ROOT / 'benchmarks/owned_inventory_reads.py'),
                           '--repository', str(ROOT), '--repeats', '2', *extra],
                          capture_output=True, text=True)


def test_inventory_measure_has_equal_work_oracles_and_refuses_changed_receipts(tmp_path):
    measured = run_measure()
    assert measured.returncode == 0, measured.stderr
    result = json.loads(measured.stdout)
    assert all(len(source_hash) == 64 for source_hash in result['sources'].values())
    for row in result['tasks']:
        assert row['total'] == row['rows'] * 4 // 5
        assert row['returned'] == min(50, row['total'])
        assert row['calls'] == {'snapshots': 1, 'workUnitProjections': row['returned']}
    for row in result['conversations']:
        assert row['calls']['catalogPages'] == 1
        assert row['historyPageRows'] > 0
    for row in result['tasks'] + result['conversations']:
        assert len(row['cpuMs']) == len(row['wallMs']) == 2
        assert len(row['sha256']) == len(row['fixtureSha256']) == 64
    result['tasks'][0]['sha256'] = 'lost-page-evidence'
    baseline = tmp_path / 'incomparable.json'
    baseline.write_text(json.dumps(result))
    refused = run_measure('--baseline', str(baseline))
    assert refused.returncode != 0
    assert 'complete response changed' in refused.stderr
    assert refused.stdout == ''
