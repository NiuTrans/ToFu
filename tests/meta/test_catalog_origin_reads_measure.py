"""Catalog provenance measurement holds its oracle and reads each admission once."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_catalog_origin_measure_reads_only_new_admissions_beside_a_writer():
    measured = subprocess.run([sys.executable, str(ROOT / 'benchmarks/catalog_origin_reads.py'),
                               '--repository', str(ROOT), '--admissions', '3', '--reads', '2',
                               '--writers', '1'], capture_output=True, text=True)
    assert measured.returncode == 0, measured.stderr
    result = json.loads(measured.stdout)
    assert result['composition'] == ['recipient_origins']
    assert all(len(source_hash) == 64 for source_hash in result['sources'].values())
    assert [read['rpcs'] for read in result['warm']] == [1]
    assert result['afterOneAdmission']['rpcs'] == 3
    assert result['retainedBytes'] > 0
    contention = result['contention']
    assert contention['failures'] == {}
    assert len(contention['succeededMs']) == 2
    assert contention['writerCommits'] >= 2
