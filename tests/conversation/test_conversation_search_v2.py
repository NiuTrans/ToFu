"""Bounded search-v2 contract, cursor, recall, and resource regressions."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from lib.conversations.search import search_conversation_page
from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
from lib.storage_authority_sql.turn_search_query import SCAN_PAGE, search_conversations

pytestmark = pytest.mark.unit


@pytest.fixture(params=[False, True], ids=['portable', 'trigram'])
def search_db(request):
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.executescript('''
CREATE TABLE storage_search_conversations (id TEXT,user_id INTEGER,title TEXT DEFAULT '',updated_at_ms INTEGER,generation_token TEXT DEFAULT '',PRIMARY KEY(user_id,id));
CREATE INDEX idx_search_order ON storage_search_conversations(user_id,updated_at_ms,id);
CREATE TABLE storage_search_chunks(user_id INTEGER,conversation_id TEXT,turn_id TEXT,chunk_index INTEGER,ordinal INTEGER,role TEXT,match_field TEXT,search_text TEXT,folded_text TEXT,generation_token TEXT DEFAULT '',PRIMARY KEY(user_id,conversation_id,turn_id,chunk_index));
''')
    if request.param:
        try:
            connection.execute("CREATE VIRTUAL TABLE storage_search_chunks_fts USING fts5(folded_text,content='storage_search_chunks',content_rowid='rowid',tokenize='trigram')")
        except sqlite3.OperationalError:
            pytest.skip('SQLite trigram tokenizer unavailable')
    yield connection, request.param
    connection.close()


def seed(db, cid, text, *, updated=1, owner=1, title='', role='user', field='content', turn_id=None,
         ordinal=0):
    connection, fts = db
    connection.execute('INSERT OR IGNORE INTO storage_search_conversations(id,user_id,title,updated_at_ms) VALUES(?,?,?,?)', (cid, owner, title, updated))
    cursor = connection.execute('INSERT INTO storage_search_chunks(user_id,conversation_id,turn_id,chunk_index,ordinal,role,match_field,search_text,folded_text) VALUES(?,?,?,?,?,?,?,?,?)',
                               (owner, cid, turn_id or cid, 0, ordinal, role, field, text, text.lower()))
    if fts:
        connection.execute('INSERT INTO storage_search_chunks_fts(rowid,folded_text) VALUES(?,?)', (cursor.lastrowid, text.lower()))


def page(db, query, **kwargs):
    return search_conversations(SQLiteSession(db[0]), {'query': query, 'user_id': 1, 'limit': 50, 'page': True, **kwargs})


def all_items(db, query, **kwargs):
    items, cursor, requests = [], None, 0
    while True:
        response = page(db, query, cursor=cursor, **kwargs)
        items.extend(response['items'])
        requests += 1
        cursor = response['next_cursor']
        if cursor is None:
            return items, requests
        assert requests < 100, 'cursor must advance'


def test_contract_limits_match_runtime():
    contract = json.loads(Path('contracts/conversation_search_v2.json').read_text())
    assert contract['limits']['candidateScanPage'] == SCAN_PAGE
    assert contract['limits']['pageResults'] == 50
    from lib.storage_authority_sql.turn_search_query import CHUNK_SCAN_PAGE, CHUNK_SCAN_BATCH, CURSOR_BYTES
    from lib.storage_authority_sql.adapters.search_support import ACCELERATOR_VM_STEPS
    assert contract['limits']['chunkScanPage'] == CHUNK_SCAN_PAGE
    assert contract['limits']['chunkScanBatch'] == CHUNK_SCAN_BATCH
    assert contract['limits']['cursorBytes'] == CURSOR_BYTES
    assert contract['limits']['sqliteAcceleratorVmSteps'] == ACCELERATOR_VM_STEPS


def test_paged_history_has_no_5000_conversation_cutoff(search_db):
    for i in range(5002):
        seed(search_db, f'c{i:05}', 'oldunique' if i == 0 else 'other', updated=i)
    started = time.monotonic()
    items, requests = all_items(search_db, 'oldunique')
    elapsed = time.monotonic() - started
    assert [item['id'] for item in items] == ['c00000']
    assert requests <= 21
    assert elapsed < 8.0  # Includes portable fallback across all 5002 headers.
    if search_db[1]:
        assert requests == 1


def test_pagination_phrase_first_cross_turn_terms_and_no_duplicates(search_db):
    for i in range(55):
        seed(search_db, f'exact-{i:02}', 'alpha gamma', updated=i)
    seed(search_db, 'cross', 'alpha', updated=100)
    seed(search_db, 'cross', 'gamma', updated=100, turn_id='second')
    seed(search_db, 'foreign', 'alpha gamma', updated=200, owner=2)
    items, _ = all_items(search_db, 'alpha gamma', limit=7)
    ids = [item['id'] for item in items]
    assert len(ids) == len(set(ids)) == 56
    assert ids[0] == 'exact-54'
    assert ids[-1] == 'cross'
    assert 'foreign' not in ids


def test_owner_query_bound_and_malformed_cursors(search_db):
    for i in range(3):
        seed(search_db, f'c{i}', 'needle', updated=i)
    cursor = page(search_db, 'needle', limit=1)['next_cursor']
    assert cursor
    for changes in ({'query': 'other'}, {'user_id': 2}, {'cursor': 'not a cursor'}):
        with pytest.raises(StorageError, match='cursor'):
            search_conversations(SQLiteSession(search_db[0]), {
                'query': 'needle', 'user_id': 1, 'page': True, 'cursor': cursor, **changes})


def test_literal_unicode_and_user_original_snippet(search_db):
    seed(search_db, 'typed', '我拖拽了压缩包 Straße ÉTÉ test%_bang!', field='originalContent')
    for query in ('压缩包', '拖拽', 'ÉTÉ', 'test%_bang!', 'straße'):
        items, _ = all_items(search_db, query)
        assert [item['id'] for item in items] == ['typed']
        assert items[0]['role'] == 'user'
        assert items[0]['match_field'] == 'originalContent'
        assert query.lower() in items[0]['snippet'].lower()
    assert all_items(search_db, 'testXXbang')[0] == []


def test_body_hits_address_their_live_turn_and_never_archived_text(search_db):
    seed(search_db, 'live', 'intro', updated=4, turn_id='turn-a', ordinal=2)
    seed(search_db, 'live', 'the needle reply', updated=4, turn_id='turn-b', ordinal=7)
    seed(search_db, 'archived', 'an archived needle', updated=3,
         turn_id='history:["archive",3]', ordinal=3)
    seed(search_db, 'titled', 'needle', updated=2, title='needle title')
    seed(search_db, 'needle-id', 'other', updated=1)
    items, _ = all_items(search_db, 'needle')
    assert [(item['id'], item['match_field'], item['turn_id'], item['ordinal'])
            for item in items] == [
        ('live', 'content', 'turn-b', 7), ('archived', 'content', None, None),
        ('titled', 'title', None, None), ('needle-id', 'id', None, None)]
    assert 'needle' in items[0]['snippet'] and 'needle' in items[1]['snippet']
    # A cross-turn all-words hit names the Turn holding the first word.
    seed(search_db, 'cross', 'alpha', updated=9, turn_id='first', ordinal=1)
    seed(search_db, 'cross', 'gamma', updated=9, turn_id='second', ordinal=2)
    cross, _ = all_items(search_db, 'alpha gamma')
    assert [(item['id'], item['turn_id'], item['ordinal']) for item in cross] == [
        ('cross', 'first', 1)]


def test_title_only_empty_conversation_and_short_content_query(search_db):
    seed(search_db, 'title-hit', '', title='ÉTÉ Research', updated=3)
    seed(search_db, 'text-hit', '研究', updated=2)
    assert all_items(search_db, 'été')[0][0]['match_field'] == 'title'
    assert all_items(search_db, '研究')[0][0]['id'] == 'text-hit'
    assert page(search_db, 'a')['items'] == []


def test_fallback_empty_page_exposes_continuation(search_db):
    if search_db[1]:
        pytest.skip('portable path only')
    for i in range(SCAN_PAGE + 1):
        seed(search_db, f'c{i:04}', 'xy' if i == 0 else 'zz', updated=i)
    first = page(search_db, 'xy')
    assert first['items'] == [] and first['next_cursor'] and first['incomplete']
    second = page(search_db, 'xy', cursor=first['next_cursor'])
    assert [item['id'] for item in second['items']] == ['c0000']
    assert second['next_cursor'] is None


def test_query_and_returned_chunk_bytes_are_bounded(search_db):
    class MeasuredSession(SQLiteSession):
        returned_text = 0
        def fetch_all(self, sql, params=()):
            rows = super().fetch_all(sql, params)
            self.returned_text += sum(len(str(row.get('search_text') or '')) for row in rows)
            return rows
        def fetch_one(self, sql, params=()):
            row = super().fetch_one(sql, params)
            self.returned_text += len(str((row or {}).get('search_text') or ''))
            return row
    for i in range(200):
        seed(search_db, f'c{i:04}', 'needle ' + 'x' * 4080, updated=i)
    session = MeasuredSession(search_db[0])
    response = search_conversations(session, {'query': 'needle', 'user_id': 1, 'page': True, 'limit': 50})
    assert len(response['items']) == 50
    assert session.returned_text <= 50 * 4096
    assert len(json.dumps(response)) < 32_000
    for query in ('x' * 513, ' '.join(['a'] * 33)):
        with pytest.raises(StorageError):
            page(search_db, query)


def test_http_service_batches_metadata_and_drops_stale_foreign_ids():
    calls = []
    class Client:
        def query(self, operation, payload):
            calls.append((operation, payload))
            if operation == 'conversation.search':
                return {'items': [{'id': 'unloaded', 'snippet': 'typed needle', 'role': 'user', 'match_field': 'content',
                                   'turn_id': 'turn-9', 'ordinal': 17}, {'id': 'foreign', 'snippet': 'secret'}],
                        'next_cursor': 'next', 'coverage': 'partial', 'coverage_reasons': ['oversize_source']}
            assert payload['user_id'] == 42
            assert payload['include_messages'] is False and payload['settings_keys'] == []
            return [{'metadata': {'id': 'unloaded', 'title': 'Old chat', 'created_at': 1, 'updated_at': 2, 'msg_count': 3, 'rev': 4}}]
    result = search_conversation_page(Client(), user_id=42, query='needle')
    assert len(calls) == 2
    assert [item['id'] for item in result['items']] == ['unloaded']
    assert result['items'][0]['conversation']['title'] == 'Old chat'
    assert result['items'][0]['matchRole'] == 'user'
    assert (result['items'][0]['matchTurnId'], result['items'][0]['matchTurnOrdinal']) == ('turn-9', 17)
    assert result['nextCursor'] == 'next' and result['coverage'] == 'partial'


@pytest.mark.parametrize('hit, expected', [
    ({'match_field': 'title', 'turn_id': None, 'ordinal': None}, (None, None)),
    ({'match_field': 'content'}, (None, None)),
])
def test_http_service_hits_without_a_live_turn_name_none(hit, expected):
    class Client:
        def query(self, operation, payload):
            if operation == 'conversation.search':
                return {'items': [{'id': 'c', 'snippet': '', 'role': None, **hit}],
                        'next_cursor': None, 'coverage': 'ready', 'coverage_reasons': []}
            return [{'metadata': {'id': 'c', 'title': 't'}}]
    item = search_conversation_page(Client(), user_id=1, query='needle')['items'][0]
    assert (item['matchTurnId'], item['matchTurnOrdinal']) == expected


def test_http_service_rejects_a_turn_without_its_ordinal():
    class Client:
        def query(self, operation, payload):
            return {'items': [{'id': 'c', 'turn_id': 'turn-1', 'ordinal': None}],
                    'next_cursor': None} if operation == 'conversation.search' else [
                        {'metadata': {'id': 'c', 'title': 't'}}]
    with pytest.raises(StorageError, match='turn'):
        search_conversation_page(Client(), user_id=1, query='needle')


def test_service_empty_queries_do_not_fetch_storage():
    class Client:
        def query(self, *_args):
            pytest.fail('empty search must not request storage')
    assert search_conversation_page(Client(), user_id=1, query=' ')['items'] == []
    with pytest.raises(ValueError):
        search_conversation_page(Client(), user_id=1, query='x' * 513)


@pytest.mark.parametrize('query', ['xy', 'alpha gamma'])
def test_one_huge_conversation_resumes_inside_chunks(search_db, query, monkeypatch):
    from lib.storage_authority_sql.turn_search_query import CHUNK_SCAN_PAGE
    monkeypatch.setattr('lib.storage_authority_sql.turn_search_query._fts_available', lambda _: False)
    count = CHUNK_SCAN_PAGE * 2 + 3
    for i in range(count):
        text = ('alpha' if i == 0 else 'gamma' if i == count - 1 else 'other') if ' ' in query else ('xy' if i == count - 1 else 'other')
        seed(search_db, 'huge', text, turn_id=f't{i:06}')
    class Measured(SQLiteSession):
        chunks = 0
        largest_batch = 0
        def fetch_all(self, sql, params=()):
            rows = super().fetch_all(sql, params)
            if 'SELECT ordinal,turn_id,chunk_index,search_text' in sql:
                self.chunks += len(rows)
                self.largest_batch = max(self.largest_batch, len(rows))
            return rows
    cursor, found, calls = None, [], 0
    session = Measured(search_db[0])
    while True:
        session.chunks = 0
        result = search_conversations(session, {'query': query, 'user_id': 1, 'limit': 50, 'page': True, 'cursor': cursor})
        assert session.chunks <= CHUNK_SCAN_PAGE
        assert session.largest_batch <= 16
        found.extend(result['items'])
        calls += 1
        if not result['next_cursor']:
            break
        assert result['next_cursor'] != cursor
        cursor = result['next_cursor']
        assert calls < 10
    assert [item['id'] for item in found] == ['huge']
    assert 3 <= calls <= 6
    assert ('alpha' if ' ' in query else query) in found[0]['snippet']


def test_accelerator_exhaustion_resumes_portable_and_restores_deadline(search_db, monkeypatch):
    if not search_db[1]:
        return
    from lib.storage_authority_sql.adapters import search_support
    monkeypatch.setattr(search_support, 'ACCELERATOR_VM_STEPS', 1000)
    for i in range(600):
        seed(search_db, f'c{i:04}', 'needle' if i == 0 else 'other', updated=i)
    first = page(search_db, 'needle')
    assert first['incomplete'] and first['next_cursor'] and not first['items']
    def no_restart(*_args):
        pytest.fail('resumed fallback must not restart the expensive FTS query')
    monkeypatch.setattr('lib.storage_authority_sql.turn_search_query._fast_page', no_restart)
    cursor, found = first['next_cursor'], []
    for _ in range(5):
        result = page(search_db, 'needle', cursor=cursor)
        found.extend(result['items'])
        cursor = result['next_cursor']
        if not cursor:
            break
    assert [hit['id'] for hit in found] == ['c0000']
    # Temporary acceleration guard must not revoke the outer query deadline.
    session = SQLiteSession(search_db[0])
    session.search_deadline_at = time.monotonic() - 1
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        with search_support.accelerator_budget(session):
            session.fetch_one('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n')
    search_db[0].set_progress_handler(None, 0)


def test_extended_cursor_validation(search_db):
    import base64
    from lib.storage_authority_sql.turn_search_query import _binding
    for pending in ([], {'after': None, 'mask': -1, 'best': None},
                    {'after': [0, 't', -1], 'mask': 0, 'best': None}):
        value = [2, _binding(1, 'needle'), 0, 1, 'c', {'scan': 1, 'pending': pending}]
        cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        with pytest.raises(StorageError, match='cursor'):
            page(search_db, 'needle', cursor=cursor)
    for extension in ({'scan': 1}, {'scan': True, 'pending': None}):
        value = [2, _binding(1, 'needle'), 0, 1, 'c', extension]
        cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        with pytest.raises(StorageError, match='cursor'):
            page(search_db, 'needle', cursor=cursor)


@pytest.mark.parametrize('query', ['abc\x00def', '\x00abcdef'])
def test_nul_query_rejected_at_both_boundaries(search_db, query):
    with pytest.raises(StorageError, match='query'):
        page(search_db, query)
    with pytest.raises(ValueError, match='NUL'):
        search_conversation_page(None, user_id=1, query=query)


@pytest.mark.parametrize('query', ['压缩', 'needle'])
def test_source_preference_survives_portable_chunk_continuation(search_db, query):
    seed(search_db, 'conversation', query + ' assistant', role='assistant', turn_id='000')
    for index in range(520):
        seed(search_db, 'conversation', 'padding', turn_id=f'fill-{index:04}')
    seed(search_db, 'conversation', query + ' human original', role='user',
         field='originalContent', turn_id='zzz')
    hits, _ = all_items(search_db, query)
    assert len(hits) == 1
    assert hits[0]['role'] == 'user'
    assert hits[0]['match_field'] == 'originalContent'
    assert 'human original' in hits[0]['snippet']
