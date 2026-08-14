"""Application adapter contract for durable orchestration Sidecar storage."""

from __future__ import annotations

import pytest

from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
from lib.storage import StorageSupervisor


pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=20)
    supervisor.start()
    try:
        yield SidecarOrchestrationRunStore(
            client=lambda **_kwargs: supervisor.client)
    finally:
        supervisor.stop()


def test_full_lifecycle_uses_semantic_operations(store):
    assert store.create_run(
        'run-adapter', definition={'nodes': []}, input_text='go',
        orch_id='flow', name='Adapter', created_by='test')
    assert store.get_run('run-adapter')['input'] == 'go'
    assert store.project_event(
        'run-adapter', 0, {'type': 'start'}, 'running')
    assert store.append_event('run-adapter', 2, {'type': 'progress'})
    page = store.get_event_page('run-adapter', 0)
    assert [event['seq'] for event in page.events] == [0, 2]
    assert page.next_cursor == 3
    assert store.update_status('run-adapter', 'done', final='complete')
    assert store.get_run('run-adapter')['terminal'] is True
    assert store.project_event(
        'run-adapter', 3, {'type': 'late'}, 'running') is False
    assert [item['id'] for item in store.list_runs(status='done')] == [
        'run-adapter']
    assert store.delete_run('run-adapter')
    assert store.get_run('run-adapter') is None
