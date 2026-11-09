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


def test_get_bounded_tail_forwards_storage_window_without_full_projection():
    client = _Client({'conversation.get': _document(
        messages=[{'role': 'assistant', 'content': 'tail'}])})

    snapshot = _repository(client).get(
        'c1', user_id=7, message_window=2)

    assert snapshot.messages == [{'role': 'assistant', 'content': 'tail'}]
    assert client.calls == [('conversation.get', {
        'conv_id': 'c1', 'user_id': 7, 'derive_messages': True,
        'message_window': 2,
    })]


def test_get_bounded_page_forwards_owner_scoped_sequence_cursor():
    client = _Client({'conversation.get': _document(
        messages=[{'role': 'user', 'content': 'head'}])})

    snapshot = _repository(client).get(
        'c1', user_id=7, message_window=8, before_sequence=8)

    assert snapshot.messages == [{'role': 'user', 'content': 'head'}]
    assert client.calls == [('conversation.get', {
        'conv_id': 'c1', 'user_id': 7, 'derive_messages': True,
        'message_window': 8, 'before_sequence': 8,
    })]


@pytest.mark.parametrize('message_window', [True, 0, 501, 1.5, '2'])
def test_get_rejects_invalid_or_ambiguous_message_window(message_window):
    client = _Client({'conversation.get': _document()})

    with pytest.raises(ValueError, match='message_window'):
        _repository(client).get(
            'c1', user_id=7, message_window=message_window)

    assert client.calls == []


def test_get_rejects_message_window_without_transcript_projection():
    client = _Client({'conversation.get': _document()})

    with pytest.raises(ValueError, match='include_messages=True'):
        _repository(client).get(
            'c1', user_id=7, include_messages=False, message_window=2)

    assert client.calls == []


@pytest.mark.parametrize('before_sequence', [True, -1, 1.5, '2'])
def test_get_rejects_invalid_message_cursor(before_sequence):
    client = _Client({'conversation.get': _document()})

    with pytest.raises(ValueError, match='before_sequence'):
        _repository(client).get(
            'c1', user_id=7, message_window=2,
            before_sequence=before_sequence)

    assert client.calls == []


def test_get_rejects_message_cursor_without_window():
    client = _Client({'conversation.get': _document()})

    with pytest.raises(ValueError, match='requires message_window'):
        _repository(client).get('c1', user_id=7, before_sequence=2)

    assert client.calls == []


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


def test_metadata_list_forwards_bounded_title_filter():
    client = _Client({'conversation.list': [_document()]})

    _repository(client).list(
        user_id=7,
        title_contains=' Storage wedge ',
        include_messages=False,
    )

    operation, payload = client.calls[0]
    assert operation == 'conversation.list'
    assert payload['title_contains'] == 'Storage wedge'


@pytest.mark.parametrize(
    'title_contains', ['', '   ', 1, True, 'x' * 513]
)
def test_title_filter_rejects_empty_unbounded_or_non_text_values(
    title_contains,
):
    client = _Client({'conversation.list': []})

    with pytest.raises(ValueError, match='title_contains'):
        _repository(client).list(
            user_id=7,
            title_contains=title_contains,
            include_messages=False,
        )

    assert client.calls == []


def test_catalog_page_is_one_owner_scoped_bounded_storage_query():
    client = _Client({'conversation.list': {
        'items': [_document('c2'), _document('c1')],
        'total_count': 7,
        'has_more': True,
    }})

    page = _repository(client).list_catalog_page(
        user_id=7,
        limit=2,
        folder_id=' folder-a ',
        before_updated_at=500,
        before_id='c3',
        settings_keys=['folderId', 'lastMsgRole'],
    )

    assert [snapshot['id'] for snapshot in page.items] == ['c2', 'c1']
    assert page.total_count == 7
    assert page.has_more is True
    assert client.calls == [('conversation.list', {
        'user_id': 7,
        'catalog_page': True,
        'include_messages': False,
        'order_by': 'updated_at_desc',
        'limit': 2,
        'before_id': 'c3',
        'folder_id': 'folder-a',
        'before_updated_at': 500,
        'settings_keys': ['folderId', 'lastMsgRole'],
    })]


@pytest.mark.parametrize(
    'response',
    [
        [],
        {'items': 'bad', 'total_count': 0, 'has_more': False},
        {'items': [_document()], 'total_count': 0, 'has_more': False},
        {'items': [], 'total_count': 0, 'has_more': 0},
    ],
)
def test_catalog_page_rejects_malformed_storage_projection(response):
    client = _Client({'conversation.list': response})
    with pytest.raises(RuntimeError, match='catalog'):
        _repository(client).list_catalog_page(user_id=7, limit=2)


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


@pytest.mark.parametrize('code', [
    'database_protocol_error', 'database_resource_exhausted',
    'database_unavailable',
])
def test_bounded_scan_splits_rejected_transcript_batches(code):
    client = _ScanClient(['c0', 'c1', 'c2', 'c3'], max_full_batch=1)
    original_query = client.query
    def query(operation, payload):
        try:
            return original_query(operation, payload)
        except StorageError as exc:
            raise StorageError(code, exc.message
                               if code == "database_protocol_error"
                               else "database_resource_exhausted") from exc
    client.query = query
    total, iterator = _repository(client).scan_bounded(
        user_id=7, batch_size=4)

    assert total == 4
    assert [row['id'] for row in iterator] == ['c0', 'c1', 'c2', 'c3']
    requested_sizes = [
        len(payload['ids']) for _, payload in client.calls if 'ids' in payload
    ]
    assert requested_sizes == [4, 2, 1, 1, 2, 1, 1]


@pytest.mark.parametrize('code', ['database_unavailable', 'database_integrity'])
def test_bounded_scan_preserves_unavailable_single_transcript(code):
    client = _ScanClient(['c0'])
    original_query = client.query
    failure = StorageError(code, 'synthetic unavailable')
    def query(operation, payload):
        if payload.get('ids') is not None:
            raise failure
        return original_query(operation, payload)
    client.query = query
    _, iterator = _repository(client).scan_bounded(user_id=7)
    with pytest.raises(StorageError) as caught:
        list(iterator)
    assert caught.value is failure


def test_activity_counts_use_owner_scoped_timestamp_only_operation():
    client = _Client({'conversation.activity_dates': {
        'candidate_count': 3,
        'counts': [2, 1],
    }})

    result = _repository(client).activity_counts(
        user_id=7,
        updated_at_gte=100,
        created_at_lt=300,
        day_boundaries_ms=[100, 200, 300],
        limit=50,
    )

    assert result == (3, [2, 1])
    assert client.calls == [('conversation.activity_dates', {
        'user_id': 7,
        'updated_at_gte': 100,
        'created_at_lt': 300,
        'day_boundaries_ms': [100, 200, 300],
        'limit': 50,
    })]


@pytest.mark.parametrize('boundaries', [
    [], [1], [1, 1], [2, 1], [1, True], [1, '2'],
])
def test_activity_counts_reject_invalid_interval_boundaries(boundaries):
    client = _Client({})
    with pytest.raises(ValueError, match='day_boundaries_ms'):
        _repository(client).activity_counts(
            user_id=7,
            updated_at_gte=1,
            day_boundaries_ms=boundaries,
        )
    assert client.calls == []


@pytest.mark.parametrize('response', [
    None,
    {'candidate_count': True, 'counts': [0]},
    {'candidate_count': 1, 'counts': []},
    {'candidate_count': 1, 'counts': [2]},
    {'candidate_count': 1, 'counts': [False]},
])
def test_activity_counts_reject_malformed_authority_projection(response):
    client = _Client({'conversation.activity_dates': response})
    with pytest.raises(RuntimeError, match='activity projection'):
        _repository(client).activity_counts(
            user_id=7,
            updated_at_gte=1,
            day_boundaries_ms=[1, 2],
        )


_TURN_USAGE = {
    'metadata': {'id': 'c1', 'title': 'T', 'created_at': 1, 'updated_at': 2,
                 'settings': {'model': 'm'}},
    'message_count': 3,
    'title_fallback': '',
    'turns': [{'index': 2, 'timestamp': 150, 'usage': {'prompt_tokens': 4},
               'providerId': 'p'}],
}


def test_turn_usage_reads_owner_scoped_usage_projection():
    client = _Client({'conversation.turn_usage': _TURN_USAGE})

    usage = _repository(client).turn_usage(
        'c1', user_id=7, timestamp_gte=100, timestamp_lt=200,
        title_fallback_characters=30)

    assert usage.metadata['settings'] == {'model': 'm'}
    assert usage.message_count == 3
    assert usage.turns == _TURN_USAGE['turns']
    assert client.calls == [('conversation.turn_usage', {
        'user_id': 7, 'conv_id': 'c1', 'timestamp_gte': 100,
        'timestamp_lt': 200, 'title_fallback_characters': 30,
    })]


def test_turn_usage_scan_lists_metadata_then_reads_each_projection():
    class _UsageClient:
        def __init__(self):
            self.calls = []

        def query(self, operation, payload):
            self.calls.append((operation, payload))
            if operation == 'conversation.list':
                return [{'metadata': {'id': conversation_id}, 'messages': []}
                        for conversation_id in ('c1', 'gone')]
            if payload['conv_id'] == 'gone':
                return None
            return _TURN_USAGE

    client = _UsageClient()
    count, projections = _repository(client).scan_turn_usage(
        user_id=7, updated_at_gte=100, created_at_lt=200,
        timestamp_gte=100, timestamp_lt=200, limit=50)

    assert count == 2
    assert [usage.metadata['id'] for usage in projections] == ['c1']
    operation, listing = client.calls[0]
    assert operation == 'conversation.list'
    assert listing['include_messages'] is False
    assert listing['derive_messages'] is False
    assert listing['settings_keys'] == []
    assert [operation for operation, _ in client.calls[1:]] == [
        'conversation.turn_usage', 'conversation.turn_usage']


@pytest.mark.parametrize('response', [
    [],
    {**_TURN_USAGE, 'message_count': True},
    {**_TURN_USAGE, 'title_fallback': None},
    {**_TURN_USAGE, 'metadata': {**_TURN_USAGE['metadata'], 'settings': []}},
    {**_TURN_USAGE, 'turns': [{'index': 3, 'usage': {}}]},
    {**_TURN_USAGE, 'turns': [{'index': 0, 'usage': 'x'}]},
])
def test_turn_usage_rejects_malformed_authority_projection(response):
    client = _Client({'conversation.turn_usage': response})
    with pytest.raises(RuntimeError, match='turn usage projection'):
        _repository(client).turn_usage(
            'c1', user_id=7, timestamp_gte=1, timestamp_lt=2)


@pytest.mark.parametrize('bounds', [
    {'timestamp_gte': 2, 'timestamp_lt': 2},
    {'timestamp_gte': True, 'timestamp_lt': 2},
    {'timestamp_gte': 1, 'timestamp_lt': 2, 'title_fallback_characters': 257},
])
def test_turn_usage_rejects_invalid_window_before_storage(bounds):
    client = _Client({})
    with pytest.raises(ValueError):
        _repository(client).turn_usage('c1', user_id=7, **bounds)
    assert client.calls == []


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


# One refusal per backend: the SQL Sidecar cannot encode a response frame
# above 64 MiB; native TofuDB refuses a response above its byte budget as a
# non-retryable domain_resource_bound. Older engines sent that refusal as
# retryable load shedding, which the client maps to database_unavailable.
_RESPONSE_REFUSALS = {
    'sql': lambda: StorageError(
        'database_protocol_error', 'Storage frame exceeds the size limit'),
    'native': lambda: StorageError(
        'database_resource_exhausted', 'database_resource_exhausted'),
    'native_legacy': lambda: StorageError(
        'database_unavailable', 'database_resource_exhausted', True),
}


def _timed_message(sequence, timestamp, *, size=1):
    return {
        'role': 'user' if sequence % 2 == 0 else 'assistant',
        'content': f'message {sequence}',
        'timestamp': timestamp,
        'size': size,
    }


class _ResponseBudgetClient:
    """Both backends' message-window contract under a response byte budget.

    ``size`` stands in for a message's encoded bytes. Whole hydration and
    windows are refused alike once a response would exceed ``budget``.
    """

    def __init__(self, transcripts, *, budget, refusal):
        self.transcripts = transcripts
        self.budget = budget
        self.refusal = _RESPONSE_REFUSALS[refusal]
        self.windows = []

    def _document(self, conversation_id, messages):
        document = _document(conversation_id, messages=messages)
        document['metadata']['settings'] = {'model': 'm', 'other': 'x'}
        document['metadata']['msg_count'] = len(
            self.transcripts[conversation_id])
        return document

    def _admit(self, messages):
        if sum(message['size'] for message in messages) > self.budget:
            raise self.refusal()

    def query(self, operation, payload):
        if operation == 'conversation.list':
            ids = payload.get('ids')
            if ids is None:
                return [self._document(conversation_id, [])
                        for conversation_id in self.transcripts]
            documents = [
                self._document(conversation_id,
                               self.transcripts[conversation_id])
                for conversation_id in ids
            ]
            self._admit([message for document in documents
                         for message in document['messages']])
            return documents
        assert operation == 'conversation.get'
        assert payload['derive_messages'] is True
        messages = self.transcripts[payload['conv_id']]
        window = payload['message_window']
        before = payload.get('before_sequence')
        end = len(messages) if before is None else max(
            0, min(before, len(messages)))
        start = max(0, end - window)
        try:
            self._admit(messages[start:end])
        except StorageError:
            self.windows.append((window, before, False))
            raise
        self.windows.append((window, before, True))
        document = self._document(payload['conv_id'], messages[start:end])
        document['message_page'] = {
            'total_count': len(messages), 'start': start, 'end': end,
        }
        return document


@pytest.mark.parametrize('refusal', sorted(_RESPONSE_REFUSALS))
def test_bounded_scan_pages_oversized_transcript_back_to_time_bound(refusal):
    oversized = [_timed_message(index, 100 + index) for index in range(10)]
    oversized[1]['timestamp'] = 0
    oversized[7]['timestamp'] = 0
    oversized[8]['size'] = oversized[9]['size'] = 4
    client = _ResponseBudgetClient({
        'small-a': [_timed_message(0, 100)],
        'oversized': oversized,
        'small-b': [_timed_message(0, 100)],
    }, budget=8, refusal=refusal)

    _, iterator = _repository(client).scan_bounded(
        user_id=7, settings_keys=['model'], oversized_messages_since_ms=103)
    snapshots = list(iterator)

    assert [row['id'] for row in snapshots] == [
        'small-a', 'oversized', 'small-b']
    # Message 2 is the newest one timestamped before the bound; message 1
    # carries no timestamp but was appended before it, so it predates the
    # bound too. Message 7 has no timestamp and follows the bound: kept.
    assert snapshots[1].messages == oversized[3:]
    assert snapshots[1]['msg_count'] == 10
    assert snapshots[1]['settings'] == {'model': 'm'}
    # A one-message tail window reports the authoritative total. Windows
    # start at half of it and halve on the same refusal; each accepted
    # window moves the cursor to its start.
    assert client.windows == [
        (1, None, True), (5, 10, False), (2, 10, True), (2, 8, True),
        (2, 6, True), (2, 4, True),
    ]


def test_windowed_transcript_reaches_first_message_when_none_precedes_bound():
    messages = [_timed_message(index, 100 + index, size=3) for index in range(5)]
    client = _ResponseBudgetClient(
        {'oversized': messages}, budget=8, refusal='native')

    _, iterator = _repository(client).scan_bounded(
        user_id=7, oversized_messages_since_ms=50)

    assert [row.messages for row in iterator] == [messages]
    assert client.windows == [
        (1, None, True), (2, 5, True), (2, 3, True), (2, 1, True),
    ]


@pytest.mark.parametrize('listed_count', [None, 0, 3, 1000])
def test_windowed_read_sizes_windows_from_the_authoritative_total(
        listed_count):
    """A listing count can be missing or stale (older native lanes keep no
    maintained count), so it never sizes the windows: the total comes from
    the one-message tail window, and no refused window rereads the whole
    transcript."""
    class StaleListingClient(_ResponseBudgetClient):
        def query(self, operation, payload):
            result = super().query(operation, payload)
            if operation == 'conversation.list' and payload.get('ids') is None:
                for document in result:
                    if listed_count is None:
                        del document['metadata']['msg_count']
                    else:
                        document['metadata']['msg_count'] = listed_count
            return result

    messages = [_timed_message(index, 100 + index) for index in range(16)]
    client = StaleListingClient(
        {'oversized': messages}, budget=6, refusal='native')

    _, iterator = _repository(client).scan_bounded(
        user_id=7, oversized_messages_since_ms=50)
    snapshots = list(iterator)

    assert [row.messages for row in snapshots] == [messages]
    assert snapshots[0]['msg_count'] == 16
    assert client.windows == [
        (1, None, True), (8, 16, False), (4, 16, True), (4, 12, True),
        (4, 8, True), (4, 4, True),
    ]


@pytest.mark.parametrize('refusal', sorted(_RESPONSE_REFUSALS))
def test_bounded_scan_without_time_bound_still_fails_oversized_transcript(
        refusal):
    client = _ResponseBudgetClient({
        'oversized': [_timed_message(index, 100, size=5) for index in range(4)],
    }, budget=8, refusal=refusal)

    _, iterator = _repository(client).scan_bounded(user_id=7)

    with pytest.raises(StorageError) as caught:
        list(iterator)
    assert caught.value.code == _RESPONSE_REFUSALS[refusal]().code
    assert client.windows == []


@pytest.mark.parametrize('refusal', sorted(_RESPONSE_REFUSALS))
def test_windowed_transcript_fails_loudly_when_one_message_exceeds_response(
        refusal):
    messages = [_timed_message(index, 100 + index) for index in range(4)]
    messages[3]['size'] = 9
    client = _ResponseBudgetClient(
        {'oversized': messages}, budget=8, refusal=refusal)

    _, iterator = _repository(client).scan_bounded(
        user_id=7, oversized_messages_since_ms=100)

    with pytest.raises(StorageError) as caught:
        list(iterator)
    assert caught.value.code == _RESPONSE_REFUSALS[refusal]().code
    assert client.windows == [(1, None, False)]


def test_windowed_transcript_refuses_history_truncated_below_its_cursor():
    class TruncatingClient(_ResponseBudgetClient):
        def query(self, operation, payload):
            result = super().query(operation, payload)
            if operation == 'conversation.get':
                del self.transcripts['oversized'][1:]
            return result

    client = TruncatingClient({
        'oversized': [_timed_message(index, 100, size=5) for index in range(4)],
    }, budget=8, refusal='native')

    _, iterator = _repository(client).scan_bounded(
        user_id=7, oversized_messages_since_ms=50)

    with pytest.raises(RuntimeError, match='changed during a windowed read'):
        list(iterator)
