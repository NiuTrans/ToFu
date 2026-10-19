"""Disposable-storage correctness and resource regressions for search v2 projection."""
import orjson
from lib.storage_authority_sql.turn_search_chunks import chunks
from lib.storage_authority_sql.turn_search_projection import LocalSQLiteTurnSearchTarget, TurnSearchProjectionRuntime
from lib.storage_authority_sql.turn_search_sources import turn_snapshot
from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
from tests.turn_lifecycle.test_turn_search_projection import _authority, _insert_authority_turn, _snapshot, _Backend


def test_chunks_keep_tail_overlap_fields_and_roles():
    value = 'x' * 20000 + 'tail-needle'
    rows = list(chunks({'content': value, 'originalContent': value + ' original',
                        'thinking': 'reason'}, 'human'))
    assert all(len(row[3]) <= 4096 for row in rows)
    assert {row[2] for row in rows} == {'content', 'originalContent', 'thinking'}
    assert all(row[1] == 'user' for row in rows)
    content = [row[3] for row in rows if row[2] == 'content']
    assert content[0][-512:] == content[1][:512]
    assert 'tail-needle' in content[-1]
    assert sum(len(text) for text in content) < len(value) * 1.2


def test_large_tool_payload_not_transferred_to_projector():
    connection = _authority()
    try:
        snapshot = _snapshot(content='visible tail')
        snapshot['projection_json'] = orjson.dumps({'content': 'visible tail', 'toolResult': 'x' * 3_000_000})
        _insert_authority_turn(connection, snapshot)
        row = turn_snapshot(SQLiteSession(connection), snapshot['user_id'], snapshot['turn_id'])
        assert len(row['projection_json']) < 150
        assert b'visible tail' in row['projection_json']
    finally:
        connection.close()


def test_history_titles_and_fts_replace_delete(tmp_path):
    connection = _authority()
    target = LocalSQLiteTurnSearchTarget(tmp_path / 'search', 20_000_000)
    target.start()
    try:
        connection.execute("INSERT INTO storage_conversations(id,user_id,title,messages_json) VALUES ('legacy',1,'Historical title',?)",
            (orjson.dumps([{'role': 'user', 'content': 'x' * 12000 + ' frozenneedle'}]),))
        connection.commit()
        runtime = TurnSearchProjectionRuntime(_Backend(connection), target, backfill_delay_s=0)
        runtime._advance_backfill()  # Active source page only.
        runtime._advance_backfill()  # Historical source page.
        db = target._writer
        assert db.execute('SELECT title FROM storage_search_conversations').fetchone()[0] == 'Historical title'
        assert db.execute("SELECT count(*) FROM storage_search_chunks WHERE search_text LIKE '%frozenneedle%'").fetchone()[0] == 1
        assert target.status()['database_bytes'] < 2_000_000
        if db.execute("SELECT 1 FROM sqlite_schema WHERE name='storage_search_chunks_fts'").fetchone():
            assert db.execute("SELECT count(*) FROM storage_search_chunks_fts WHERE storage_search_chunks_fts MATCH 'frozenneedle'").fetchone()[0] == 1
            runtime._advance_backfill()
            target.reset()
            assert db.execute("SELECT count(*) FROM storage_search_chunks_fts WHERE storage_search_chunks_fts MATCH 'frozenneedle'").fetchone()[0] == 0
    finally:
        target.close()
        connection.close()


import pytest
import tracemalloc
from lib.storage_authority_sql.turn_search_sources import history_page, MAX_BYTES
from lib.storage_authority_sql.archived_message_codec import encode_archived_message_sequence_with_metrics

pytestmark = pytest.mark.unit


def _legacy(connection, messages, owner=1, conv='legacy'):
    connection.execute('INSERT INTO storage_conversations(id,user_id,messages_json) VALUES (?,?,?)',
                       (conv, owner, orjson.dumps(messages)))


def test_multipart_and_lower_are_not_json_or_casefold():
    projection = {'content': [{'type': 'text', 'text': 'Straße '},
                              {'type': 'image_url', 'image_url': {'url': 'secret-image'}},
                              {'type': 'text', 'text': 'TAIL'}]}
    assert list(chunks(projection, 'human')) == [(0, 'user', 'content', 'Straße TAIL', 'straße tail')]
    connection = _authority()
    try:
        snapshot = _snapshot()
        snapshot['projection_json'] = orjson.dumps(projection)
        _insert_authority_turn(connection, snapshot)
        result = turn_snapshot(SQLiteSession(connection), 1, 'turn-1')
        assert orjson.loads(result['projection_json'])['content'] == 'Straße TAIL'
        assert turn_snapshot(SQLiteSession(connection), 2, 'turn-1') is None
    finally:
        connection.close()


@pytest.mark.parametrize('checkpoint', [False, True])
def test_real_schema_patch_head_and_checkpoint(checkpoint):
    from lib.turn_projection_patch import build_projection_patch
    connection = _authority()
    try:
        snapshot = _snapshot(content='base')
        _insert_authority_turn(connection, snapshot)
        patch = build_projection_patch({'content': 'base'}, {'content': 'base patched'},
                                       base_revision=1, target_revision=2)
        payload = orjson.dumps({'payload': {'projectionPatch': patch}})
        connection.execute('UPDATE storage_conversation_turns SET current_attempt_id=?,projection_revision=2,'
                           'projection_materialized_revision=1,projection_patch_count=1,projection_patch_bytes=?, '
                           'projection_checkpoint_revision=?', ('attempt', len(orjson.dumps(patch)), 1 if checkpoint else None))
        connection.execute('INSERT INTO storage_attempt_events(attempt_id,sequence,conversation_id,turn_id,'
                           'projection_revision,type,payload_json,payload_bytes,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                           ('attempt', 1, 'conv-1', 'turn-1', 2, 'projection', payload, len(payload), 1))
        if checkpoint:
            base = snapshot['projection_json']
            connection.execute('INSERT INTO storage_turn_projection_checkpoints VALUES (?,?,?,?,?,?,?,?)',
                               ('turn-1', 'conv-1', 1, 'attempt', 1, base, len(base), 1))
            connection.execute("UPDATE storage_conversation_turns SET projection_json='{}'")
        row = turn_snapshot(SQLiteSession(connection), 1, 'turn-1')
        assert orjson.loads(row['projection_json'])['content'] == 'base patched'
        if checkpoint:
            connection.execute('UPDATE storage_turn_projection_checkpoints SET user_id=2')
            with pytest.raises(ValueError, match='malformed_source'):
                turn_snapshot(SQLiteSession(connection), 1, 'turn-1')
    finally:
        connection.close()


def test_archive_compression_roundtrip_and_malformed_budget_evidence():
    connection = _authority()
    try:
        public = {'role': 'user', 'content': 'question ' * 10000,
                  'thinking': 'retained', 'toolResult': 'do not index'}
        encoded = orjson.loads(bytes(encode_archived_message_sequence_with_metrics([public]).stored_document))[0]
        assert '_tofuArchivedMessageCodec' in encoded
        oversized = {'_tofuArchivedMessageCodec': {'decodedBytes': 250001}}
        corrupt = {'_tofuArchivedMessageCodec': {'decodedBytes': 70000}}
        _legacy(connection, [encoded, oversized, corrupt, {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'multipart'}, {'type': 'image_url', 'text': 'not text'}]}])
        page = history_page(SQLiteSession(connection), {'user_id': 1, 'entity_key': 'legacy'}, '')
        assert [orjson.loads(row['projection_json'])['content'] for row in page['rows']] == [public['content'], 'multipart']
        assert page['skipped_reasons'] == {'source_budget': 1, 'malformed_source': 1}
        assert page['skipped'] == 2
        assert b'do not index' not in b''.join(row['projection_json'] for row in page['rows'])
        with pytest.raises(ValueError, match='invalid_history_cursor'):
            history_page(SQLiteSession(connection), {'user_id': 2, 'entity_key': 'legacy'}, page['cursor'])
        assert history_page(SQLiteSession(connection), {'user_id': 2, 'entity_key': 'legacy'}, '')['rows'] == []
    finally:
        connection.close()


def test_large_archive_message_pagination_transfer_and_python_peak():
    connection = _authority()
    try:
        # Old question lies past 500 KB in a multi-megabyte plain array.
        messages = [{'role': 'user', 'content': 'x' * 10000} for _ in range(240)]
        messages[180]['content'] = 'old question beyond the former document cap'
        _legacy(connection, messages)
        del messages
        class MeasuredSession(SQLiteSession):
            returned = 0
            def fetch_one(self, sql, params=()):
                result = super().fetch_one(sql, params)
                if result:
                    self.returned += sum(len(value) for value in result if isinstance(value, (str, bytes)))
                return result
        session = MeasuredSession(connection)
        identity = {'user_id': 1, 'entity_key': 'legacy'}
        cursor, found, ids, max_returned = '', False, set(), 0
        tracemalloc.start()
        try:
            for _ in range(100):
                session.returned = 0
                page = history_page(session, identity, cursor)
                assert len(page['rows']) <= 8
                assert page['skipped'] == 0
                assert len(orjson.dumps(page, default=lambda value: orjson.loads(value))) < MAX_BYTES
                max_returned = max(max_returned, session.returned)
                for row in page['rows']:
                    assert row['turn_id'] not in ids
                    ids.add(row['turn_id'])
                    found |= b'old question' in row['projection_json']
                if not page['remaining']:
                    break
                assert page['cursor'] != cursor
                cursor = page['cursor']
            else:
                pytest.fail('pagination did not terminate')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert found and len(ids) == 240
        assert 0 < max_returned < 100000
        assert peak < 4_000_000  # Includes orjson output buffer and retained ID assertions.
    finally:
        connection.close()


def test_malformed_json_is_skipped_but_database_lock_is_not():
    import sqlite3
    connection = _authority()
    try:
        connection.execute("INSERT INTO storage_conversations(id,user_id,messages_json) VALUES ('broken',1,'[')")
        page = history_page(SQLiteSession(connection), {'user_id': 1, 'entity_key': 'broken'}, '')
        assert page['skipped_reasons'] == {'malformed_source': 1}
        assert not page['remaining']
        class Locked(SQLiteSession):
            def fetch_one(self, sql, params=()):
                raise sqlite3.OperationalError('database is locked')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            history_page(Locked(connection), {'user_id': 1, 'entity_key': 'broken'}, '')
    finally:
        connection.close()


def test_compaction_sources_and_budget_resume_without_duplicates():
    connection = _authority()
    try:
        _legacy(connection, [])
        for source, owner in [('a', 1), ('b', 2), ('c', 1)]:
            messages = [{'role': 'user', 'content': source * 140000} for _ in range(2)]
            connection.execute('INSERT INTO storage_compaction_archives(archive_id,conversation_id,user_id,messages_json,created_at_ms) '
                               'VALUES (?,?,?,?,?)', (source, 'legacy', owner, orjson.dumps(messages), 1))
        cursor, seen = '', []
        for _ in range(12):
            page = history_page(SQLiteSession(connection), {'user_id': 1, 'entity_key': 'legacy'}, cursor)
            assert len(page['rows']) <= 1  # Two admitted messages exceed the shared decode charge.
            assert page['skipped'] == 0
            seen.extend(orjson.loads(row['turn_id'][8:]) for row in page['rows'])
            if not page['remaining']:
                break
            assert page['cursor'] != cursor
            cursor = page['cursor']
        assert seen == [['a', 0], ['a', 1], ['c', 0], ['c', 1]]
    finally:
        connection.close()


def test_private_codec_corruption_cannot_be_ready_text():
    from lib.storage.errors import StorageError
    connection = _authority()
    try:
        snapshot = _snapshot()
        snapshot['projection_json'] = orjson.dumps({'content': 'not ready', '_tofuStorageProjectionCodec': None})
        _insert_authority_turn(connection, snapshot)
        with pytest.raises((StorageError, ValueError)):
            turn_snapshot(SQLiteSession(connection), 1, 'turn-1')
    finally:
        connection.close()


def test_background_rounds_bound_pages_headers_and_exact_ack(tmp_path):
    from lib.storage_authority_sql.turn_search_projection import PROJECTION_NAME
    connection = _authority()
    target = LocalSQLiteTurnSearchTarget(tmp_path / 'bounded', 20_000_000)
    target.start()
    try:
        _legacy(connection, [{'role': 'user', 'content': str(i)} for i in range(20)], conv='a')
        _legacy(connection, [], conv='b')
        connection.execute('INSERT INTO storage_projection_outbox VALUES (?,?,?,?,?,?)',
                           (PROJECTION_NAME, 'conversation', 1, 'a', 'old', 1))
        runtime = TurnSearchProjectionRuntime(_Backend(connection), target, backfill_delay_s=0)
        dirty = dict(runtime._list_dirty()[0])
        assert not runtime._process_conversation(dirty)
        assert target._writer.execute('SELECT count(*) FROM storage_search_chunks').fetchone()[0] == 0
        assert len(runtime._list_dirty()) == 1
        assert not runtime._process_conversation(dirty)
        assert target._writer.execute('SELECT count(*) FROM storage_search_chunks').fetchone()[0] == 8
        # A replacement dirty generation must survive completion of the old one.
        connection.execute("UPDATE storage_projection_outbox SET version_token='new'")
        for _ in range(8):
            if runtime._process_conversation(dirty):
                break
        else:
            pytest.fail('bounded conversation did not finish')
        assert runtime._list_dirty()[0]['version_token'] == 'new'
        assert runtime._metrics['dirty_acked'] == 0
        new = dict(runtime._list_dirty()[0])
        for _ in range(8):
            if runtime._process_conversation(new):
                break
        assert runtime._list_dirty() == []
        assert runtime._metrics['dirty_acked'] == 1
        target.reset()
        assert runtime._advance_backfill()
        assert target._writer.execute('SELECT id FROM storage_search_conversations').fetchall()[0][0] == 'a'
        assert target._writer.execute('SELECT count(*) FROM storage_search_conversations').fetchone()[0] == 1
        assert target.backfill_state() == ('', False)
    finally:
        target.close()
        connection.close()


def test_disk_full_rolls_back_replacement_and_recovers(tmp_path):
    import time
    import sqlite3
    target = LocalSQLiteTurnSearchTarget(tmp_path / 'capacity', 20_000_000)
    target.start()
    identity = {'user_id': 1, 'entity_key': 'turn-1'}
    try:
        target.apply_turn(identity, _snapshot(content='original needle'), 'old')
        target.set_backfill_state('checkpoint', True)
        target._writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        pages = target._writer.execute('PRAGMA page_count').fetchone()[0]
        target._writer.execute(f'PRAGMA max_page_count={pages}')
        with pytest.raises(sqlite3.OperationalError, match='full'):
            target.apply_turn(identity, _snapshot(content='replacement ' * 15000), 'new')
        assert not target._writer.in_transaction
        assert target._writer.execute('SELECT generation_token FROM storage_search_turns').fetchone()[0] == 'old'
        assert target.backfill_state() == ('checkpoint', True)
        target._writer.execute('PRAGMA max_page_count=2147483646')
        target.apply_turn(identity, _snapshot(content='recovered needle'), 'new')
        assert target.status()['state'] == 'ready'
        assert target.query(lambda s: s.fetch_one('SELECT generation_token FROM storage_search_turns'),
                            time.monotonic() + 1)['generation_token'] == 'new'
    finally:
        target.close()


@pytest.mark.parametrize('shared', [False, True])
def test_source_skips_persist_restart_and_reset(tmp_path, shared):
    from lib.storage_authority_sql.turn_search_projection import AuthorityTurnSearchTarget
    connection = _authority()
    def make_target():
        return (AuthorityTurnSearchTarget(_Backend(connection)) if shared else
                LocalSQLiteTurnSearchTarget(tmp_path / 'skips', 20_000_000))
    target = make_target()
    try:
        target.start()
        target.record_skips(7)
        target.close()
        target = make_target()
        target.start()
        assert target.status()['source_skipped'] == 7
        target.reset()
        target.close()
        target = make_target()
        target.start()
        assert target.status()['source_skipped'] == 0
    finally:
        target.close()
        connection.close()


def test_source_generation_change_retries_without_ack(tmp_path, monkeypatch):
    from lib.storage.errors import StorageError
    from lib.storage_authority_sql import turn_search_sources
    connection = _authority()
    target = LocalSQLiteTurnSearchTarget(tmp_path / 'race', 20_000_000)
    target.start()
    try:
        runtime = TurnSearchProjectionRuntime(_Backend(connection), target, backfill_delay_s=0)
        def changed(*args):
            raise ValueError('source_changed')
        monkeypatch.setattr(turn_search_sources, 'turn_snapshot', changed)
        with pytest.raises(StorageError, match='source changed'):
            runtime._process_turn({'user_id': 1, 'entity_key': 'turn-1', 'version_token': 'v'})
        assert runtime._metrics['dirty_acked'] == runtime._metrics['oversize_skipped'] == 0
        assert target.status()['writes'] == 0
    finally:
        target.close()
        connection.close()
