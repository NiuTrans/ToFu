"""Executable contract for the Research Foundry production workspace."""

from __future__ import annotations

import asyncio

import pytest

from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit
DIRECTION = 'Mechanistic KV cache compression'


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    import lib.research.workspace as workspace
    from lib.storage import StorageSupervisor

    supervisor = StorageSupervisor(
        project_root=tmp_path / 'sidecar', backend='sqlite', startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(workspace, '_storage', lambda **_kwargs: supervisor.client)
    try:
        yield supervisor
    finally:
        supervisor.stop()


def test_new_direction_returns_canonical_revision_zero(storage):
    from lib.research.workspace import load_workspace
    got = load_workspace(DIRECTION, 'en', user_id=1)
    assert got['revision'] == 0
    assert got['direction'] == DIRECTION
    assert got['runs'] == [] and got['claims'] == []
    assert got['compilation']['status'] == 'not_run'


def test_workspace_survives_round_trip_and_normalizes_records(storage):
    from lib.research.workspace import load_workspace, save_workspace
    draft = load_workspace(DIRECTION, 'en', user_id=1)
    draft.update({
        'stage': 'experiment',
        'hypothesis': 'Layer entropy predicts a safe compression rate.',
        'protocol': {
            'primary_metric': 'Needle recall', 'baseline': 'Full KV',
            'dataset': 'RULER', 'falsifier': 'Recall drops by >2%',
            'resources': '1x A100 / 6h',
        },
        'runs': [{'id': 'r1', 'label': '4x compression', 'status': 'passed',
                  'metric': '97.8', 'artifact_ref': 'runs/r1/metrics.json'}],
        'claims': [{'id': 'c1', 'text': 'Recall is retained',
                    'status': 'supported', 'evidence_refs': ['run:r1']}],
    })
    saved = save_workspace(
        DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    assert saved['revision'] == 1
    restored = load_workspace(DIRECTION, 'en', user_id=1)
    assert restored['hypothesis'].startswith('Layer entropy')
    assert restored['runs'][0]['status'] == 'passed'
    assert restored['claims'][0]['evidence_refs'] == ['run:r1']


def test_stale_revision_cannot_overwrite_newer_evidence(storage):
    from lib.research.workspace import empty_workspace, save_workspace
    first = empty_workspace(DIRECTION)
    save_workspace(DIRECTION, 'en', first, expected_revision=0, user_id=1)
    with pytest.raises(StorageError) as caught:
        save_workspace(DIRECTION, 'en', first, expected_revision=0, user_id=1)
    assert caught.value.code == 'database_conflict'


def test_owner_and_language_are_isolated(storage):
    from lib.research.workspace import empty_workspace, load_workspace, save_workspace
    draft = empty_workspace(DIRECTION)
    draft['hypothesis'] = 'private owner one'
    save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    assert load_workspace(DIRECTION, 'en', user_id=2)['revision'] == 0
    assert load_workspace(DIRECTION, 'zh', user_id=1)['revision'] == 0


def test_http_contract_is_mounted_and_rejects_stale_revision(storage):
    import lib.research.workspace as workspace
    import server

    server.app.config['TESTING'] = True
    client = server.app.test_client()
    response = _run(client.get(
        '/api/v1/research/workspace', query_string={
            'direction': DIRECTION, 'lang': 'en'}))
    assert response.status_code == 200
    body = _run(response.get_json())
    assert body['workspace']['revision'] == 0

    payload = {
        'direction': DIRECTION, 'lang': 'en', 'expected_revision': 0,
        'workspace': body['workspace'],
    }
    committed = _run(client.put('/api/v1/research/workspace', json=payload))
    assert committed.status_code == 200
    conflict = _run(client.put('/api/v1/research/workspace', json=payload))
    assert conflict.status_code == 409


def test_workspace_budget_rejects_oversize_document(storage):
    from lib.research.workspace import empty_workspace, normalize_workspace
    draft = empty_workspace(DIRECTION)
    draft['hypothesis'] = 'x' * 500_000
    normalized = normalize_workspace(DIRECTION, 'en', draft, revision=0)
    assert len(normalized['hypothesis']) == 12_000


def test_http_scaffold_and_zip_export_keep_one_versioned_source_tree(storage):
    import server

    server.app.config['TESTING'] = True
    client = server.app.test_client()
    loaded = _run(client.get('/api/v1/research/workspace', query_string={
        'direction': DIRECTION, 'lang': 'en'}))
    workspace = _run(loaded.get_json())['workspace']
    workspace['manuscript']['title'] = 'A bounded & reproducible paper'
    scaffolded = _run(client.post(
        '/api/v1/research/manuscript/scaffold', json={
            'direction': DIRECTION, 'lang': 'en', 'expected_revision': 0,
            'workspace': workspace,
        }))
    assert scaffolded.status_code == 200
    body = _run(scaffolded.get_json())
    assert body['workspace']['revision'] == 1
    assert any(row['path'] == 'main.tex'
               for row in body['workspace']['source_files'])

    archive = _run(client.get(
        '/api/v1/research/manuscript/source.zip', query_string={
            'direction': DIRECTION, 'lang': 'en'}))
    assert archive.status_code == 200
    assert archive.content_type == 'application/zip'
    assert bytes(_run(archive.get_data())).startswith(b'PK')
