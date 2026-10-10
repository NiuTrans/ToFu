"""Admin restart progress is durable, bounded and cannot grant approval."""
import json
from pathlib import Path
import pytest
from jsonschema import validate
from lib import lifecycle_approval as approvals

pytestmark = pytest.mark.unit


def test_preparation_projection_preserves_approval_and_uses_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, '_APPROVALS_FILE', str(tmp_path / 'approvals.json'))
    record = approvals.create_request('restart')
    approvals.record_restart_preparation(record['id'], {'phase': 'compiling', 'elapsedSeconds': 21,
                                                       'remainingSeconds': 500})
    observed = approvals.get(record['id'])
    assert observed['status'] == 'pending'
    assert approvals.validate(record['id'], 'restart')[0] is False
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/restart_preparation_v1.json').read_text())
    validate(observed['preparation'], schema)
    assert observed['preparation']['phase'] == 'compiling'
    approvals.record_restart_preparation(record['id'], {'phase': 'frontend', 'elapsedSeconds': 30})
    observed = approvals.get(record['id'])
    assert 'remainingSeconds' not in observed['preparation']
    assert observed['status'] == 'pending'


def test_unknown_progress_and_shutdown_records_cannot_create_restart_authority(tmp_path, monkeypatch):
    path = tmp_path / 'approvals.json'
    monkeypatch.setattr(approvals, '_APPROVALS_FILE', str(path))
    shutdown = approvals.create_request('shutdown')
    before = path.read_bytes()
    approvals.record_restart_preparation('missing', {'phase': 'compiling', 'elapsedSeconds': 1})
    approvals.record_restart_preparation(shutdown['id'], {'phase': 'compiling', 'elapsedSeconds': 1})
    assert path.read_bytes() == before
