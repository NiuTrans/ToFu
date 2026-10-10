"""Read-only job inspection uses acknowledged indexes without receipt-history scans."""
from copy import deepcopy
import pytest
from lib.research import workspace
from lib.research.receipt_pages import ReceiptPages
from tests.research.test_research_pending_paging import DIRECTION, initial, save, apply
from tests.research.test_research_pending_lineage import receipt
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def test_indexed_inspection_overlays_tail_without_history_reads_or_writes(storage, monkeypatch):
    supervisor, _ = storage
    saved = save(initial())
    candidate = apply(saved, 'finish-A', [receipt('job-A', 'completed', 'poll', 'finish-A')], supervisor.client)
    # An acknowledged pending snapshot may contain an unpaged current window.
    snapshot = deepcopy(saved)
    snapshot['runs'][0]['tool_receipts'] = candidate['runs'][0]['tool_receipts']
    monkeypatch.setattr(workspace, 'load_workspace', lambda *args, **kwargs: deepcopy(snapshot))
    def forbidden(*args, **kwargs):
        raise AssertionError('inspection must neither scan history nor write records')
    monkeypatch.setattr(ReceiptPages, 'open_receipts', forbidden)
    monkeypatch.setattr(supervisor.client, 'command', forbidden)
    result = workspace.read_run_execution_state(DIRECTION, user_id=1, run_id='run')
    assert result == dict(revision=saved['revision'], run_id='run', count=3, execution_status='completed')


def test_legacy_snapshot_uses_one_acknowledged_snapshot(storage, monkeypatch):
    saved = workspace.save_workspace(DIRECTION, 'en', initial(), expected_revision=0, user_id=1)
    calls = []
    def snapshot(*args, **kwargs):
        calls.append(1)
        return deepcopy(saved)
    monkeypatch.setattr(workspace, 'load_workspace', snapshot)
    assert workspace.read_run_execution_state(DIRECTION, user_id=1, run_id='run')['execution_status'] == 'running'
    assert len(calls) == 1


def test_present_corrupt_checkpoint_never_falls_back(storage, monkeypatch):
    from lib.storage.errors import StorageError
    snapshot = save(initial())
    snapshot['runs'][0]['receipt_progress'] = '0' * 64
    monkeypatch.setattr(workspace, 'load_workspace', lambda *args, **kwargs: deepcopy(snapshot))
    with pytest.raises(StorageError, match='checkpoint is missing or invalid'):
        workspace.read_run_execution_state(DIRECTION, user_id=1, run_id='run')


def test_long_history_inspection_has_bounded_reads(storage, monkeypatch):
    from lib.research.persistence import research_direction_hash
    from lib.research.progress_checkpoint import ProgressCheckpointRepository
    supervisor, _ = storage
    snapshot = save(initial())
    run = snapshot['runs'][0]
    repository = ProgressCheckpointRepository(supervisor.client, user_id=1,
        paper_hash=research_direction_hash(DIRECTION), lang='en', run=run)
    candidate = deepcopy(run)
    rows = []
    for ordinal in range(1024):
        row = receipt('job-B', 'completed', 'poll', f'call-{ordinal}')
        row['source_task_id'] = f'poll-{ordinal}'
        rows.append(row)
    candidate['receipt_history'] = repository.pages.prepare(run['receipt_history'], rows)
    candidate['receipt_progress'] = repository.prepare(run, candidate, snapshot['capability_bindings'])
    # Supply the acknowledged snapshot seam; all checkpoint/page records are real.
    snapshot['runs'][0] = candidate
    monkeypatch.setattr(workspace, 'load_workspace', lambda *args, **kwargs: deepcopy(snapshot))
    query = supervisor.client.query
    queries = []
    def counted(name, args):
        queries.append((name, args.get('namespace')))
        return query(name, args)
    monkeypatch.setattr(supervisor.client, 'query', counted)
    result = workspace.read_run_execution_state(DIRECTION, user_id=1, run_id='run')
    assert result['count'] == 1026 and result['execution_status'] == 'running'
    assert all(namespace != 'research.receipt_pages.v1' for _, namespace in queries)
    assert len(queries) <= 3
