"""Diagnostic transport keeps authentication, owner identity and bounded cursors."""
from __future__ import annotations

import json

import pytest

from lib.conversations import diagnostic
from lib.storage import StorageError

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture
def reads(monkeypatch):
    seen = []
    def read(conv_id, *, user_id, **options):
        seen.append((conv_id, user_id, options))
        return {'revision': 7, 'totalCount': 0, 'turnNative': True,
                'document': {'metadata': {'id': conv_id, 'rev': 7}, 'messages': []}}
    monkeypatch.setattr(diagnostic, 'read_diagnostic', read)
    return seen


def test_diagnostic_is_served_on_v4_and_passes_authenticated_owner(flask_client, reads):
    response = flask_client.get('/api/v4/conversations/conv/diagnostic')
    assert response.status_code == 200
    assert response.get_json()['revision'] == 7
    assert reads == [('conv', 1, {'kind': 'header'})]


@pytest.mark.parametrize('query', [
    'kind=bad', 'kind=header&revision=1', 'kind=turn',
    'kind=chunk&revision=1&index=0',
    'kind=chunk&revision=1&index=0&offset=0&limit=65537',
    'kind=chunk&revision=1&index=0&offset=0&limit=0',
    'kind=turn&revision=-1&index=0', 'kind=turn&revision=1&index=true',
    'kind=turn&revision=1&index=1.0', 'kind=header&kind=turn',
    'user_id=2', 'offset=0', 'kind=header&unknown=1',

    'kind=chunk&revision=1&index=0&offset=0',
    'kind=chunk&revision=1&index=0&offset=0&source_token=invalid',
    'kind=chunk&revision=1&index=0&offset=0&source_token=' + 'A' * 64,
    'kind=chunk&revision=1&index=0&offset=0&source_token=' + '0' * 63,
    'kind=header&source_token=' + '0' * 64,
    'kind=turn&revision=1&index=0&source_token=' + '0' * 64,
])
def test_invalid_cursors_never_reach_storage(flask_client, reads, query):
    assert flask_client.get('/api/v4/conversations/conv/diagnostic?' + query).status_code == 400
    assert reads == []


def test_chunk_forwards_source_token_and_authenticated_owner(flask_client, reads):
    token = 'a1' * 32
    response = flask_client.get('/api/v4/conversations/conv/diagnostic?'
        'kind=chunk&revision=7&index=2&offset=65536&limit=64&source_token=' + token)
    assert response.status_code == 200
    assert reads == [('conv', 1, {'kind': 'chunk', 'revision': 7, 'index': 2,
        'offset': 65536, 'limit': 64, 'source_token': token})]


def test_foreign_or_missing_conversation_is_not_found(flask_client, monkeypatch):
    monkeypatch.setattr(diagnostic, 'read_diagnostic', lambda *args, **kwargs: None)
    response = flask_client.get('/api/v4/conversations/conv/diagnostic')
    assert response.status_code == 404
    # The sync test facade maps get_json() back to v1 and drops problem fields.
    # Validate the bytes that the inspector actually receives from the v4 route.
    assert response.headers['Content-Type'].startswith('application/problem+json')
    problem = json.loads(response.get_data())
    assert problem['status'] == 404
    assert problem['code'] == 'diagnostic_conversation_not_found'


def test_revision_conflict_is_not_retried_or_downgraded(flask_client, monkeypatch):
    seen = []
    def conflict(*args, **kwargs):
        seen.append(kwargs)
        raise StorageError('database_conflict', 'revision changed')
    monkeypatch.setattr(diagnostic, 'read_diagnostic', conflict)
    response = flask_client.get('/api/v4/conversations/conv/diagnostic?kind=turn&revision=7&index=0')
    assert response.status_code == 409
    assert len(seen) == 1


def test_old_storage_requires_upgrade_not_full_read_fallback(flask_client, monkeypatch):
    def missing(*args, **kwargs):
        raise StorageError('unknown_operation', 'unknown operation')
    monkeypatch.setattr(diagnostic, 'read_diagnostic', missing)
    response = flask_client.get('/api/v4/conversations/conv/diagnostic')
    assert response.status_code == 503

    assert response.headers['Content-Type'].startswith('application/problem+json')
    problem = json.loads(response.get_data())
    assert problem['status'] == 503
    assert problem['code'] == 'diagnostic_upgrade_required'
    assert 'updated storage service' in str(response.get_json())


@pytest.mark.auth_mode('private')
def test_diagnostic_requires_authentication(flask_client, reads):
    assert flask_client.get('/api/v4/conversations/conv/diagnostic').status_code == 401
    assert reads == []


@pytest.mark.unit
@pytest.mark.parametrize('options', [
    {'kind': 'turn', 'revision': True, 'index': 0},
    {'kind': 'chunk', 'revision': 1, 'index': 0, 'offset': 0, 'limit': False},
    {'kind': 'header', 'index': 0},
])
def test_programmatic_cursor_validation_rejects_bool_and_extraneous_fields(options):
    with pytest.raises(ValueError):
        diagnostic.diagnostic_payload('conv', user_id=1, **options)
