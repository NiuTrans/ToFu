"""Identical SQL provider ownership semantics across SQLite and private PG."""
import sqlite3

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.operations_pkg import _providers as providers
from lib.storage_sidecar.schema import initialize_schema

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.api


@pytest.fixture(params=['sqlite', 'postgres'])
def provider_session(request, tmp_path):
    if request.param == 'sqlite':
        from lib.storage_sidecar.adapters.sqlite import SQLiteSession
        connection = sqlite3.connect(tmp_path / 'provider.sqlite')
        connection.row_factory = sqlite3.Row
        session = SQLiteSession(connection)
    else:
        import psycopg
        from psycopg.rows import dict_row
        from lib.storage_sidecar.adapters.postgres import PostgresSession, _PgPool
        connection = psycopg.connect(request.getfixturevalue('postgres_cluster'), row_factory=dict_row)
        _PgPool._configure(connection)
        session = PostgresSession(connection)
    try:
        initialize_schema(session)
        yield session
    finally:
        connection.close()


def payload(index, owner=11, label='workspace-a'):
    return dict(provider_id=f'provider-{index:03}', owner_user_id=owner,
                tenant_id=label, name=f'provider {index}', base_url='https://example.test/v1',
                api_key_ciphertext='opaque-test-envelope', key_hint='', models=[],
                extra_headers={}, thinking_format='', created_at=float(index))


def test_catalog_crosses_retired_quota_and_preserves_owner_label_and_redaction(provider_session):
    session = provider_session
    for index in range(65):
        providers._provider_create(session, payload(index))
    boundary = dict(owner_user_id=11, tenant_id='workspace-a')
    listed = providers._provider_list(session, boundary)
    assert [row['id'] for row in listed] == [f'provider-{index:03}' for index in reversed(range(65))]
    assert all('api_key_ciphertext' not in row for row in listed)
    assert providers._provider_list(session, dict(owner_user_id=12, tenant_id='workspace-a')) == []
    assert providers._provider_list(session, dict(owner_user_id=11, tenant_id='workspace-b')) == []
    with pytest.raises(StorageError) as duplicate:
        providers._provider_create(session, payload(0, owner=12, label='workspace-b'))
    assert duplicate.value.code == 'database_conflict'
    assert providers._provider_delete(session, dict(boundary, provider_id='provider-064')) == {'deleted': True}
    assert len(providers._provider_list(session, boundary)) == 64
