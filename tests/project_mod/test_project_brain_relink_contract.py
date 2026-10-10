"""Project Brain checkpoint relinking parity, with opt-in real PostgreSQL."""
from contextlib import ExitStack
import os
from pathlib import Path
import sqlite3

import pytest

from lib.storage.errors import StorageError
from tests.support.postgres_isolation import isolated_postgres_dsn


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('backend', ['sqlite', pytest.param('postgres', marks=pytest.mark.skipif(
    os.environ.get('TOFU_STORAGE_TEST_POSTGRES') != '1',
    reason='requires explicitly provisioned PostgreSQL test authority',
))])
@pytest.mark.parametrize('checkpoint', ['valid', 'foreign_owner', 'foreign_path', 'invalid'])
def test_checkpoint_and_relink_preserve_owner_event_state(tmp_path, monkeypatch, backend, checkpoint):
    from lib.storage_sidecar.operations_pkg._common import _dump, _load
    from lib.storage_sidecar.operations_pkg import _project_brain as brain
    from lib.storage_sidecar.schema import initialize_schema

    with ExitStack() as resources:
        if backend == 'postgres':
            import psycopg
            from psycopg.rows import dict_row
            from lib.storage_sidecar.adapters.postgres import PostgresSession, _PgPool
            source = Path(os.environ['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE'])
            dsn_file = resources.enter_context(isolated_postgres_dsn(source, tmp_path))
            connection = resources.enter_context(psycopg.connect(dsn_file.read_text(), row_factory=dict_row))
            _PgPool._configure(connection)
            session = PostgresSession(connection)
        else:
            from lib.storage_sidecar.adapters.sqlite import SQLiteSession
            connection = sqlite3.connect(':memory:')
            resources.callback(connection.close)
            connection.row_factory = sqlite3.Row
            session = SQLiteSession(connection)
        with connection:
            initialize_schema(session)
            monkeypatch.setattr(brain, 'EVENT_CHECKPOINT_THRESHOLD', 2)
            monkeypatch.setattr(brain, 'EVENT_CHECKPOINT_TAIL', 1)
            scope = {'owner_user_id': 7, 'project_key': '/old'}
            for index in range(2):
                brain._project_brain_narrative_add(session, {
                    **scope, 'kind': 'decision', 'text': f'kept-{index}', 'timestamp': index + 1,
                })
            foreign = {**scope, 'owner_user_id': 8}
            brain._project_brain_narrative_add(session, {
                **foreign, 'kind': 'decision', 'text': 'foreign', 'timestamp': 1,
            })
            before = brain._project_brain_rebuild(session, scope)['projection']
            assert before['checkpointSequence'] == 3
            if checkpoint != 'valid':
                event = _load(session.fetch_one('SELECT event_json FROM storage_events '
                                               'WHERE owner_user_id=?', (7,))['event_json'])
                if checkpoint == 'invalid':
                    event['payload'] = None
                elif checkpoint == 'foreign_owner':
                    event['payload']['snapshot']['ownerUserId'] = 8
                else:
                    event['payload']['snapshot']['projectKey'] = '/foreign'
                encoded = bytes(_dump(event))
                session.execute('UPDATE storage_events SET event_json=? WHERE owner_user_id=?', (encoded, 7))
                connection.commit()
                with pytest.raises(StorageError, match='checkpoint') as raised:
                    with connection.transaction() if backend == 'postgres' else connection:
                        brain._project_brain_relink_scope(session, 7, '/old', '/new')
                assert raised.value.code == 'database_integrity'
                assert bytes(session.fetch_one('SELECT event_json FROM storage_events '
                                               'WHERE owner_user_id=? AND project_key=?', (7, '/old'))['event_json']) == encoded
                assert session.fetch_one('SELECT count(*) AS n FROM storage_project_brain_projects '
                                         'WHERE owner_user_id=? AND project_key=?', (7, '/new'))['n'] == 0
                return
            assert brain._project_brain_relink_scope(session, 7, '/old', '/new')
            after = brain._project_brain_rebuild(
                session, {**scope, 'project_key': '/new'})['projection']
            assert after == {**before, 'projectKey': '/new'}
            assert [item['text'] for item in after['narratives']] == ['kept-0', 'kept-1']
            assert brain._project_brain_rebuild(session, foreign)['projection']['narratives'][0]['text'] == 'foreign'
            assert session.fetch_one('SELECT count(*) AS n FROM storage_events '
                                     'WHERE owner_user_id=? AND project_key=?', (7, '/old'))['n'] == 0
            connection.commit()
