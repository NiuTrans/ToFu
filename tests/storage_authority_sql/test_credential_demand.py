"""Complete credential catalogs retain bearer authority on SQLite/private PG."""
import sqlite3

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg import _credentials as operations
from lib.storage_authority_sql.schema import initialize_schema

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.api


@pytest.fixture(params=['sqlite', 'postgres'])
def credential_session(request, tmp_path):
    if request.param == 'sqlite':
        from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
        connection = sqlite3.connect(tmp_path / 'credentials.sqlite')
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


def test_catalog_crosses_old_quota_keeps_boundary_redaction_and_revocation(credential_session):
    session = credential_session
    boundary = dict(owner_user_id=11, tenant_id='test-catalog')
    def payload(index, **extra):
        return dict(boundary, credential_id=f'key-{index:04}', account_user_id='',
                    name=f'key {index}', prefix='tf_test_', secret_hash=f'{index:064x}',
                    scopes=['read'], rate_limit_rpm=60, rate_limit_tpd=1000,
                    created_at=100.0+index, metadata={}, **extra)
    for index in range(1001):
        operations._credential_create(session, payload(index))
    rows = operations._credential_list(session, boundary)
    assert len(rows) == 1001
    assert rows[0]['id'] == 'key-1000' and rows[-1]['id'] == 'key-0000'
    assert all('secret_hash' not in row for row in rows)
    assert operations._credential_list(session, dict(owner_user_id=12, tenant_id='test-catalog')) == []
    assert operations._credential_list(session, dict(owner_user_id=11, tenant_id='elsewhere')) == []
    assert operations._credential_create_if_owner_empty(session, payload(1002)) is None
    duplicate = payload(1000)
    duplicate.update(owner_user_id=12, credential_id='different-id')
    with pytest.raises(StorageError) as conflict:
        operations._credential_create(session, duplicate)
    assert conflict.value.code == 'database_conflict'
    last = dict(secret_hash=f'{1000:064x}', now=2000.0)
    assert operations._credential_validate(session, last)['id'] == 'key-1000'
    assert operations._credential_revoke(session, dict(boundary, credential_id='key-1000', revoked_at=2001.0))['revoked']
    assert operations._credential_validate(session, dict(last, now=2002.0)) is None
    assert len(operations._credential_list(session, boundary)) == 1000
