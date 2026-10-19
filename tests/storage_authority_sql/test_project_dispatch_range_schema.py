"""Dispatch pagination seeks a byte range without decoding unrelated records.

The PostgreSQL index is installed only through schema migration. SQLite reuses
its existing BINARY primary key. Both preserve pre-existing record evidence.
"""
import sqlite3

import pytest

from lib.storage_authority_sql.operations_pkg import _project_dispatch as dispatch
from lib.storage_authority_sql.schema import initialize_schema, SCHEMA_VERSION

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])
def test_dispatch_ranges_are_index_seeks_after_schema_upgrade(backend, request):
    if backend == 'postgres':
        import psycopg
        from psycopg.rows import dict_row
        from lib.storage_authority_sql.adapters.postgres import PostgresSession, _PgPool
        connection = psycopg.connect(request.getfixturevalue('postgres_cluster'), row_factory=dict_row)
        _PgPool._configure(connection)
        session = PostgresSession(connection)
    else:
        from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        session = SQLiteSession(connection)
    try:
        initialize_schema(session)
        session.execute('INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) VALUES (?, ?, ?, ?, ?)',
                        ('unrelated', 'durable', '{}', 7, 1))
        session.execute('UPDATE storage_meta SET meta_value = ? WHERE meta_key = ?', ('67', 'schema_version'))
        if backend == 'postgres':
            session.execute('DROP INDEX idx_project_dispatch_record_range')
        initialize_schema(session)
        assert int(session.fetch_one('SELECT meta_value FROM storage_meta WHERE meta_key = ?', ('schema_version',))['meta_value']) == SCHEMA_VERSION
        assert session.fetch_one('SELECT version FROM storage_records WHERE namespace = ?', ('unrelated',))['version'] == 7
        scope = {'user_id': 41, 'project_path': '/project'}
        dispatch.commit(session, {**scope, 'expected_revision': 0, 'head': {}, 'changes': [
            {'collection': 'indexes', 'id': '0001', 'expected_version': 0, 'value': {'assignment': 'one'}}]})
        if backend == 'postgres':
            # Tiny fixture tables legitimately prefer a sequential scan. Prove
            # index eligibility and range predicates, not a size-dependent cost.
            session.execute('SET LOCAL enable_seqscan = off')

        plans = []
        original = session.fetch_all
        def capture(sql, params=()):
            prefix = 'EXPLAIN ' if backend == 'postgres' else 'EXPLAIN QUERY PLAN '
            plans.extend(original(prefix + sql, params))
            return original(sql, params)
        session.fetch_all = capture
        for reverse in (False, True):
            plans.clear()
            assert len(dispatch.list_documents(session, {**scope, 'collection': 'indexes', 'limit': 1, 'reverse': reverse})) == 1
            text = str([dict(row) for row in plans])
            if backend == 'postgres':
                assert 'idx_project_dispatch_record_range' in text and 'Index Cond' in text
                assert 'Sort' not in text and 'Seq Scan' not in text
            else:
                assert 'SEARCH storage_records USING INDEX sqlite_autoindex_storage_records_1' in text
                assert 'record_key>?' in text and 'record_key<?' in text
                assert 'TEMP B-TREE' not in text
    finally:
        connection.close()
