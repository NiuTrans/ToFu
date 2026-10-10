"""Opt-in real PostgreSQL coverage for bounded portable search continuation."""
import os
from pathlib import Path

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.turn_search_chunks import CHUNK_SCHEMA
from lib.storage_sidecar.turn_search_query import search_conversations
from tests.support.postgres_isolation import isolated_postgres_dsn


pytestmark = [pytest.mark.slow, pytest.mark.skipif(
    os.environ.get('TOFU_STORAGE_TEST_POSTGRES') != '1',
    reason='requires explicitly provisioned PostgreSQL test authority',
)]


@pytest.mark.parametrize('query', ['压缩', 'needle'])
def test_postgres_source_preference_and_owner_survive_continuation(tmp_path, query):
    import psycopg
    from psycopg.rows import dict_row
    from lib.storage_sidecar.adapters.postgres import PostgresSession

    source = Path(os.environ['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE'])
    with isolated_postgres_dsn(source, tmp_path) as dsn_file:
        with psycopg.connect(dsn_file.read_text(), row_factory=dict_row) as connection:
            session = PostgresSession(connection)
            session.execute('CREATE TABLE storage_search_conversations ('
                            'id TEXT,user_id BIGINT,title TEXT,updated_at_ms BIGINT,'
                            'generation_token TEXT,PRIMARY KEY(user_id,id))')
            for statement in CHUNK_SCHEMA:
                session.execute(statement)
            session.execute('INSERT INTO storage_search_conversations VALUES '
                            "('conversation',1,'',1,'g'),('foreign',2,'',2,'g')")
            for index in range(522):
                text = query + (' human original' if index == 521 else ' assistant')
                if 0 < index < 521:
                    text = 'padding'
                session.execute('INSERT INTO storage_search_chunks VALUES (?,?,?,?,?,?,?,?,?,?)',
                                (1, 'conversation', f't{index:04}', 0, index,
                                 'user' if index == 521 else 'assistant',
                                 'originalContent' if index == 521 else 'content',
                                 text, text.lower(), 'g'))
            session.execute('INSERT INTO storage_search_chunks VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (2, 'foreign', 't', 0, 0, 'user', 'content', query, query.lower(), 'g'))
            payload = {'query': query, 'user_id': 1, 'page': True, 'limit': 1}
            first = search_conversations(session, payload)
            assert first['items'] == [] and first['incomplete'] and first['next_cursor']
            with pytest.raises(StorageError, match='cursor'):
                search_conversations(session, {**payload, 'user_id': 2, 'cursor': first['next_cursor']})
            hits, cursor = [], first['next_cursor']
            for _ in range(5):
                result = search_conversations(session, {**payload, 'cursor': cursor})
                hits.extend(result['items'])
                cursor = result['next_cursor']
                if cursor is None:
                    break
            assert cursor is None
            assert hits == [{'id': 'conversation', 'snippet': '…' + query + ' human original…',
                             'role': 'user', 'match_field': 'originalContent'}]
