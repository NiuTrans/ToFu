"""Identical scheduler definition admission and projections on SQLite/private PG."""
import sqlite3

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg import _timers as operations
from lib.storage_authority_sql.schema import initialize_schema

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.api


@pytest.fixture(params=['sqlite', 'postgres'])
def scheduler_session(request, tmp_path):
    if request.param == 'sqlite':
        from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
        connection = sqlite3.connect(tmp_path / 'scheduler.sqlite')
        connection.row_factory = sqlite3.Row
        session = SQLiteSession(connection)
    else:
        import psycopg
        from psycopg.rows import dict_row
        from lib.storage_authority_sql.adapters.postgres import PostgresSession, _PgPool
        connection = psycopg.connect(request.getfixturevalue('postgres_cluster'), row_factory=dict_row)
        _PgPool._configure(connection)
        session = PostgresSession(connection)
    try:
        initialize_schema(session)
        yield session
    finally:
        connection.close()


def test_definitions_cross_old_quota_keep_owner_filter_and_identity(scheduler_session):
    session = scheduler_session
    def payload(index, owner=11):
        return dict(task_id=f'scheduled-{index:04}', user_id=owner,
                    name=f'task {index}', command='status', schedule='* * * * *',
                    created_at='2026-10-10T00:00:00', tools_config={})
    for index in range(105):
        operations._scheduler_create(session, payload(index))
    assert len(operations._scheduler_list(session, dict(user_id=11, limit=0))) == 105
    assert len(operations._scheduler_list_all(session, dict(limit=0))) == 105
    assert len(operations._scheduler_list(session, dict(user_id=11, limit=2001))) == 105
    assert len(operations._scheduler_list(session, dict(user_id=11, limit=3))) == 3
    assert operations._scheduler_list(session, dict(user_id=12, limit=0)) == []
    with pytest.raises(StorageError) as duplicate:
        operations._scheduler_create(session, payload(0, owner=12))
    assert duplicate.value.code == 'database_conflict'
