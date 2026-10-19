"""Current v4 folder preset wire behavior over a disposable app authority."""
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path, monkeypatch):
    import routes.api_v1.folders as owner
    monkeypatch.setattr(owner, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    monkeypatch.setattr(owner, '_notify_folders_changed', lambda **kwargs: None)
    return owner


def test_create_requires_only_name_and_lists_confirmed_record(flask_client, store):
    response = flask_client.post('/api/v4/folders', json={'name': 'Name only'})
    assert response.status_code == 201, response.get_data(as_text=True)
    folder = response.get_json()
    assert folder['projectPaths'] == [] and folder['readOnlyPaths'] == []
    assert folder['revision'] == 1
    listed = flask_client.get('/api/v4/folders').get_json()['items']
    assert listed[0]['id'] == folder['id'] and 'ownerUserId' not in listed[0]


def test_confirmed_readonly_defaults_conflict_and_invalid_save(flask_client, store):
    created = flask_client.post('/api/v4/folders', json={'name': 'Work', 'projectPaths': ['/code', '/ref'], 'readOnlyPaths': ['/ref']}).get_json()
    url = f"/api/v4/folders/{created['id']}"
    saved = flask_client.put(url, json={'projectPaths': ['/ref', '/code'], 'readOnlyPaths': ['/ref'], 'expectedRevision': 1})
    assert saved.status_code == 200
    assert saved.get_json()['projectPath'] == '/ref' and saved.get_json()['revision'] == 2
    stale = flask_client.put(url, json={'name': 'stale', 'expectedRevision': 1})
    assert stale.status_code == 409
    bad = flask_client.put(url, json={'projectPaths': ['/ok', None], 'expectedRevision': 2})
    assert bad.status_code == 400
    current = flask_client.get('/api/v4/folders').get_json()['items'][0]
    assert current['revision'] == 2 and current['name'] == 'Work'
    assert current['readOnlyPaths'] == ['/ref']


def test_missing_and_corrupt_store_never_report_success(flask_client, store):
    assert flask_client.put('/api/v4/folders/absent', json={'name': 'x'}).status_code == 404
    with open(store._FOLDERS_PATH, 'w') as file: file.write('{broken')
    response = flask_client.post('/api/v4/folders', json={'name': 'new'})
    assert response.status_code >= 500
    assert open(store._FOLDERS_PATH).read() == '{broken'


def test_unsafe_color_is_a_bad_request_without_partial_create_or_update(flask_client, store):
    payload = '\"><svg/onload=alert(1)>'
    rejected = flask_client.post('/api/v4/folders', json={'name': 'Unsafe', 'color': payload})
    assert rejected.status_code == 400, rejected.get_data(as_text=True)
    assert flask_client.get('/api/v4/folders').get_json()['items'] == []
    created = flask_client.post('/api/v4/folders', json={'name': 'Palette', 'color': '#abc'}).get_json()
    url = f"/api/v4/folders/{created['id']}"
    rejected = flask_client.put(url, json={'name': 'Partial edit', 'color': payload, 'expectedRevision': 1})
    assert rejected.status_code == 400, rejected.get_data(as_text=True)
    expected = {key: value for key, value in created.items() if key != 'ok'}
    assert flask_client.get('/api/v4/folders').get_json()['items'] == [expected]


def test_historical_color_is_sanitized_on_http_read_without_storage_rewrite(flask_client, store):
    import json
    from pathlib import Path
    from lib.identity import PERSONAL_USER_ID
    path = Path(store._FOLDERS_PATH)
    row = {'id': 'legacy', 'name': 'Historical', 'color': '\"><svg/onload=alert(1)>',
           'ownerUserId': PERSONAL_USER_ID}
    path.write_text(json.dumps([row]))
    before = path.read_bytes()
    response = flask_client.get('/api/v4/folders')
    assert response.status_code == 200
    assert response.get_json()['items'][0]['color'] == ''
    assert path.read_bytes() == before
