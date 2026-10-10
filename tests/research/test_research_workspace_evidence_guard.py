"""Application saves retain completed scientific observations across revisions."""
from copy import deepcopy

import pytest

from lib.research.workspace import empty_workspace, load_workspace, save_workspace
from lib.research.program import dependency_digests
from lib.storage import StorageSupervisor
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit
DIRECTION = 'Replication of compression results'


@pytest.fixture(params=['sqlite', 'tofudb'])
def storage(tmp_path, monkeypatch, request):
    import lib.research.workspace as workspace
    supervisor = StorageSupervisor(project_root=tmp_path / 'storage', backend=request.param, startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(workspace, '_storage', lambda **_: supervisor.client)
    try:
        yield supervisor, request.param
    finally:
        supervisor.stop()


def saved_observation():
    draft = empty_workspace(DIRECTION, 'en')
    draft['runs'] = [{'id': 'counter-result', 'status': 'passed',
                      'execution_status': 'completed', 'scientific_outcome': 'refuted',
                      'task_id': 'experiment-1', 'artifact_ref': 'metrics.json',
                      'tool_receipts': [{'tool': 'experiment.result', 'status': 'done',
                                         'result_digest': 'counter-digest'}]}]
    draft['runs'][0].update(dependency_digests(draft))
    return save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)


@pytest.mark.parametrize('change', ['delete', 'rename', 'outcome', 'receipt', 'metric', 'pending'])
def test_completed_observation_cannot_be_erased_or_rewritten(storage, change):
    original = saved_observation()
    draft = deepcopy(original)
    row = draft['runs'][0]
    if change == 'delete':
        draft['runs'] = []
    elif change == 'rename':
        row['id'] = 'renamed-counter'
    elif change == 'outcome':
        row['scientific_outcome'] = 'supported'
    elif change == 'receipt':
        row['tool_receipts'] = []
    elif change == 'metric':
        row['metric'] = 'invented replacement'
    else:
        row['execution_status'] = 'running'
    with pytest.raises(ValueError, match='Completed research observations'):
        save_workspace(DIRECTION, 'en', draft, expected_revision=1, user_id=1)
    assert load_workspace(DIRECTION, 'en', user_id=1) == original


def test_scoped_protocol_edits_and_appended_results_preserve_observation(storage):
    original = saved_observation()
    draft = deepcopy(original)
    draft['hypothesis'] = 'A narrower tested scope'
    draft['protocol']['dataset'] = 'Independent held-out data'
    draft['runs'].append({'id': 'replication', 'status': 'running'})
    saved = save_workspace(DIRECTION, 'en', draft, expected_revision=1, user_id=1)
    assert saved['revision'] == 2
    assert saved['runs'][0]['scientific_outcome'] == 'refuted'
    assert saved['runs'][0]['validity'] == 'stale'
    assert saved['runs'][1]['id'] == 'replication'


def test_pending_observation_can_settle(storage):
    draft = empty_workspace(DIRECTION, 'en')
    draft['runs'] = [{'id': 'pending', 'execution_status': 'running'}]
    saved = save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    saved['runs'][0].update(execution_status='completed', scientific_outcome='refuted')
    settled = save_workspace(DIRECTION, 'en', saved, expected_revision=1, user_id=1)
    assert settled['runs'][0]['scientific_outcome'] == 'refuted'


def test_legacy_completed_rows_are_also_retained(storage):
    draft = empty_workspace(DIRECTION, 'en')
    draft['runs'] = [{'id': 'legacy', 'status': 'inconclusive'}]
    saved = save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    saved['runs'] = []
    with pytest.raises(ValueError, match='Completed research observations'):
        save_workspace(DIRECTION, 'en', saved, expected_revision=1, user_id=1)


def test_stale_revision_precedes_evidence_validation(storage):
    saved = saved_observation()
    saved['runs'] = []
    with pytest.raises(StorageError) as caught:
        save_workspace(DIRECTION, 'en', saved, expected_revision=0, user_id=1)
    assert caught.value.code == 'database_conflict'


def test_observations_remain_owner_scoped(storage):
    saved_observation()
    other = empty_workspace(DIRECTION, 'en')
    if storage[1] == 'tofudb':
        # This native authority is opened for owner 1; crossing the authority
        # scope must refuse the operation rather than expose another owner.
        with pytest.raises(StorageError) as caught:
            save_workspace(DIRECTION, 'en', other, expected_revision=0, user_id=2)
        assert caught.value.message == 'authority_scope_mismatch'
    else:
        saved = save_workspace(DIRECTION, 'en', other, expected_revision=0, user_id=2)
        assert saved['runs'] == []
    assert len(load_workspace(DIRECTION, 'en', user_id=1)['runs']) == 1


def test_workspace_http_put_returns_error_and_keeps_counterevidence(storage):
    import server
    from tests.research.test_research_workspace import _run, _get_json

    saved = saved_observation()
    saved['runs'] = []
    server.app.config['TESTING'] = True
    response = _run(server.app.test_client().put('/api/v4/research/workspace', json={
        'direction': DIRECTION, 'lang': 'en', 'workspace': saved, 'expected_revision': 1,
    }))
    assert response.status_code == 400
    assert 'Completed research observations' in str(_get_json(response))
    restored = load_workspace(DIRECTION, 'en', user_id=1)
    assert restored['revision'] == 1
    assert restored['runs'][0]['scientific_outcome'] == 'refuted'
