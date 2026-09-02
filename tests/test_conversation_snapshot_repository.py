"""Domain contract for owner-scoped conversation snapshots."""

from __future__ import annotations

import pytest

from lib.conversations.repository import ConversationRepository
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def query(self, operation, payload):
        self.calls.append((operation, payload))
        return self.responses.get(operation)


def _document(cid='c1', messages=None):
    return {
        'metadata': {
            'id': cid, 'user_id': 7, 'title': 'Title', 'settings': {},
            'created_at': 1, 'updated_at': 2, 'msg_count': len(messages or []),
            'rev': 3,
        },
        'messages': list(messages or []),
    }


def _repository(client):
    return ConversationRepository(lambda *, write=False: client)


def test_get_is_owner_scoped_and_requests_turn_projection():
    client = _Client({'conversation.get': _document(
        messages=[{'role': 'user', 'content': 'hello'}])})
    snapshot = _repository(client).get('c1', user_id=7)
    assert snapshot['id'] == 'c1'
    assert snapshot.messages[0]['content'] == 'hello'
    assert client.calls == [('conversation.get', {
        'conv_id': 'c1', 'user_id': 7, 'derive_messages': True,
    })]


def test_metadata_list_never_loads_transcripts_and_bounds_settings():
    client = _Client({'conversation.list': [_document()]})
    snapshots = _repository(client).list(
        user_id=7,
        project_path='/projects/alpha',
        include_messages=False,
        settings_keys=['projectSummary'],
    )
    assert snapshots[0].messages == []
    operation, payload = client.calls[0]
    assert operation == 'conversation.list'
    assert payload['include_messages'] is False
    assert payload['derive_messages'] is False
    assert payload['project_path'] == '/projects/alpha'
    assert payload['settings_keys'] == ['projectSummary']


@pytest.mark.parametrize('project_path', ['', 1, True, 'x' * 4097])
def test_project_filter_rejects_unbounded_or_non_text_values(project_path):
    client = _Client({'conversation.list': []})
    with pytest.raises(ValueError, match='project_path'):
        _repository(client).list(user_id=7, project_path=project_path)
    assert client.calls == []


class _ScanClient:
    def __init__(self, conversation_ids, *, max_full_batch=None):
        self.conversation_ids = list(conversation_ids)
        self.max_full_batch = max_full_batch
        self.calls = []

    def query(self, operation, payload):
        assert operation == 'conversation.list'
        self.calls.append((operation, dict(payload)))
        ids = payload.get('ids')
        if ids is None:
            return [_document(conversation_id) for conversation_id
                    in self.conversation_ids]
        if self.max_full_batch is not None and len(ids) > self.max_full_batch:
            raise StorageError(
                'database_protocol_error',
                'Storage frame exceeds the size limit',
            )
        return [
            _document(conversation_id, messages=[{
                'role': 'user', 'content': conversation_id,
            }])
            for conversation_id in reversed(ids)
        ]


def test_bounded_scan_lists_metadata_then_hydrates_small_ordered_batches():
    client = _ScanClient([f'c{index}' for index in range(7)])
    total, iterator = _repository(client).scan_bounded(
        user_id=7,
        updated_at_gte=100,
        created_at_lt=200,
        limit=50,
        settings_keys=['model'],
        batch_size=3,
    )

    snapshots = list(iterator)

    assert total == 7
    assert [row['id'] for row in snapshots] == [f'c{index}' for index in range(7)]
    metadata_payload = client.calls[0][1]
    assert metadata_payload == {
        'user_id': 7,
        'order_by': 'updated_at_desc',
        'limit': 50,
        'include_messages': False,
        'derive_messages': False,
        'settings_keys': [],
        'updated_at_gte': 100,
        'created_at_lt': 200,
    }
    body_payloads = [payload for _, payload in client.calls[1:]]
    assert [payload['ids'] for payload in body_payloads] == [
        ['c0', 'c1', 'c2'], ['c3', 'c4', 'c5'], ['c6'],
    ]
    assert all(payload['settings_keys'] == ['model'] for payload in body_payloads)


def test_bounded_scan_splits_only_oversize_transcript_frames():
    client = _ScanClient(['c0', 'c1', 'c2', 'c3'], max_full_batch=1)
    total, iterator = _repository(client).scan_bounded(
        user_id=7, batch_size=4)

    assert total == 4
    assert [row['id'] for row in iterator] == ['c0', 'c1', 'c2', 'c3']
    requested_sizes = [
        len(payload['ids']) for _, payload in client.calls if 'ids' in payload
    ]
    assert requested_sizes == [4, 2, 1, 1, 2, 1, 1]


def test_search_returns_ids_without_leaking_storage_hits():
    client = _Client({'conversation.search': [
        {'id': 'c2', 'snippet': 'match'}, {'id': 'c3', 'snippet': 'match'},
    ]})
    assert _repository(client).search_ids(
        'needle', user_id=7, limit=20) == ['c2', 'c3']
    assert client.calls[0] == ('conversation.search', {
        'query': 'needle', 'user_id': 7, 'limit': 20,
    })


@pytest.mark.parametrize('user_id', [None, 0, -1, True, '7'])
def test_owner_is_required_and_typed(user_id):
    client = _Client({'conversation.get': None})
    with pytest.raises(ValueError, match='user_id'):
        _repository(client).get('c1', user_id=user_id)


@pytest.mark.parametrize('document', [
    {'metadata': {}, 'messages': '[]'},
    {'metadata': 'bad', 'messages': []},
    {'metadata': {}, 'messages': ['bad']},
])
def test_malformed_authority_projection_fails_loudly(document):
    client = _Client({'conversation.get': document})
    with pytest.raises(RuntimeError, match='malformed'):
        _repository(client).get('c1', user_id=7)
