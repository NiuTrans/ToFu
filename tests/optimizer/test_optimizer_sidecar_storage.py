"""Optimizer repository behavior through a real storage.v1 process."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import uuid

import pytest

from lib.storage import StorageSupervisor


pytestmark = pytest.mark.unit


@pytest.fixture
def optimizer_store(tmp_path, monkeypatch):
    import lib.optimizer.storage as storage

    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(
        storage, '_storage', lambda **_kwargs: supervisor.client)
    try:
        yield storage
    finally:
        supervisor.stop()


def test_repository_lifecycle_preserves_wire_compatibility(
        optimizer_store, monkeypatch):
    storage = optimizer_store
    ids = iter(['opt_adapter', 'act_adapter'])
    monkeypatch.setattr(storage, 'short_id', lambda *_args: next(ids))

    proposal_id = storage.create_proposal(
        owner_user_id=7,
        title='Tune bounded queue', rationale='avoid unbounded RSS',
        action_type='set_limit', action_args={'limit': 200},
        evidence=['writer-depth'], status='pending_review')
    assert proposal_id == 'opt_adapter'
    proposal = storage.get_proposal(proposal_id, owner_user_id=7)
    assert proposal['user_id'] == 7
    assert json.loads(proposal['action_args']) == {'limit': 200}
    assert json.loads(proposal['evidence']) == ['writer-depth']
    assert storage.list_proposals(
        owner_user_id=7, status='pending_review')[0]['id'] == proposal_id
    assert storage.get_proposal(proposal_id, owner_user_id=8) is None
    assert storage.list_proposals(owner_user_id=8) == []

    storage.update_proposal_status(
        proposal_id, 'applied', 'approved', owner_user_id=7)
    log_id = storage.record_applied(
        owner_user_id=7,
        proposal_id=proposal_id, ttl_days=2,
        pre_metric={'writer_depth': 10})
    assert log_id == 'act_adapter'
    storage.record_outcome_metric(
        log_id, {'writer_depth': 2}, owner_user_id=7)
    action = storage.get_action_log_for_proposal(
        proposal_id, owner_user_id=7)
    assert action['user_id'] == 7
    assert json.loads(action['pre_metric'])['writer_depth'] == 10
    assert json.loads(action['outcome_metric'])['writer_depth'] == 2
    assert storage.list_applied_actions(
        owner_user_id=7)[0]['p_status'] == 'applied'
    assert storage.get_action_log_for_proposal(
        proposal_id, owner_user_id=8) is None

    storage.mark_reverted(log_id, 'test complete', owner_user_id=8)
    assert storage.list_applied_actions(owner_user_id=7)[0][
        'reverted_at'] == ''
    storage.mark_reverted(log_id, 'test complete', owner_user_id=7)
    assert storage.list_applied_actions(owner_user_id=7) == []
    assert storage.list_applied_actions(
        owner_user_id=7, include_reverted=True)[0][
        'revert_reason'] == 'test complete'


def test_repository_rejects_cross_owner_action_link(
        optimizer_store, monkeypatch):
    from lib.storage.errors import StorageError

    storage = optimizer_store
    monkeypatch.setattr(storage, 'short_id', lambda *_args: 'shared-id')
    storage.create_proposal(
        owner_user_id=7,
        title='Owner seven', rationale='owner isolation',
        action_type='set_limit', action_args={'limit': 7},
    )
    with pytest.raises(StorageError, match='does not exist'):
        storage.record_applied(
            owner_user_id=8,
            proposal_id='shared-id',
            ttl_days=1,
        )

    # The durable identity is (owner, id), so the same opaque id cannot make
    # one owner's proposal visible or uncreatable for another owner.
    assert storage.create_proposal(
        owner_user_id=8,
        title='Owner eight', rationale='owner isolation',
        action_type='set_limit', action_args={'limit': 8},
    ) == 'shared-id'
    assert storage.get_proposal(
        'shared-id', owner_user_id=7)['title'] == 'Owner seven'
    assert storage.get_proposal(
        'shared-id', owner_user_id=8)['title'] == 'Owner eight'


def test_cost_outlier_signal_uses_daily_cost_semantics(monkeypatch):
    from lib.optimizer.analyzer import _signals

    class Client:
        def query(self, operation, payload):
            assert operation == 'daily_cost.latest'
            assert payload == {'user_id': 23}
            return {'conversations': {
                'conv-low': {'cost': 0.5},
                'conv-high': {'cost': 8.25},
            }}

    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: Client())
    assert _signals._collect_cost_outliers(owner_user_id=23) == {
        'top_cost_conversations': [
            {'conv_id': 'conv-high', 'cost_usd': 8.25},
            {'conv_id': 'conv-low', 'cost_usd': 0.5},
        ],
    }


def test_scheduler_signal_query_is_owner_scoped(monkeypatch):
    from lib.optimizer.analyzer import _signals

    class Client:
        def query(self, operation, payload):
            assert operation == 'scheduler.task.list'
            assert payload == {
                'user_id': 23,
                'limit': 1000,
                'enabled_only': False,
            }
            return [{
                'id': 'owner-23-task',
                'name': 'Owner task',
                'task_type': 'command',
                'run_count': 10,
                'fail_count': 7,
            }]

    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: Client())
    result = _signals._collect_scheduler_signals(owner_user_id=23)
    assert [row['id'] for row in result['failing_scheduled_tasks']] == [
        'owner-23-task']


@pytest.mark.parametrize('published_version', [None, 36])
def test_schema_v37_backfills_legacy_optimizer_rows_without_owner_default(
        tmp_path, published_version):
    from lib.storage_sidecar import schema
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession

    connection = sqlite3.connect(tmp_path / f'optimizer-v{published_version}.db')
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.executescript(
        """
        CREATE TABLE optimizer_proposals (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            title TEXT NOT NULL, rationale TEXT NOT NULL,
            action_type TEXT NOT NULL, action_args TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'low',
            confidence REAL NOT NULL DEFAULT 0,
            evidence TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending_review',
            status_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE optimizer_action_log (
            id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
            applied_at TEXT NOT NULL, expires_at TEXT NOT NULL DEFAULT '',
            pre_metric TEXT NOT NULL DEFAULT '',
            outcome_metric TEXT NOT NULL DEFAULT '',
            outcome_recorded_at TEXT NOT NULL DEFAULT '',
            reverted_at TEXT NOT NULL DEFAULT '',
            revert_reason TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(proposal_id) REFERENCES optimizer_proposals(id)
        );
        INSERT INTO optimizer_proposals(
            id, created_at, title, rationale, action_type, action_args)
        VALUES ('legacy-proposal', '2026-01-01', 'Legacy', 'Personal row',
                'set_limit', '{}');
        INSERT INTO optimizer_action_log(id, proposal_id, applied_at)
        VALUES ('legacy-action', 'legacy-proposal', '2026-01-02');
        """
    )
    if published_version is not None:
        connection.execute(
            'CREATE TABLE storage_meta('
            'meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)')
        connection.execute(
            'INSERT INTO storage_meta VALUES (?, ?)',
            ('schema_version', str(published_version)),
        )

    schema.initialize_schema(SQLiteSession(connection))

    proposal = connection.execute(
        'SELECT user_id, id FROM optimizer_proposals').fetchone()
    action = connection.execute(
        'SELECT user_id, id, proposal_id FROM optimizer_action_log').fetchone()
    proposal_columns = {
        row['name']: row for row in connection.execute(
            'PRAGMA table_info(optimizer_proposals)')
    }
    action_columns = {
        row['name']: row for row in connection.execute(
            'PRAGMA table_info(optimizer_action_log)')
    }
    indexes = {
        row['name'] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name IN ('optimizer_proposals', 'optimizer_action_log')")
    }
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()['meta_value']
    foreign_key = {
        (row['table'], row['from'], row['to'])
        for row in connection.execute(
            'PRAGMA foreign_key_list(optimizer_action_log)')
    }
    connection.close()

    assert tuple(proposal) == (1, 'legacy-proposal')
    assert tuple(action) == (1, 'legacy-action', 'legacy-proposal')
    assert foreign_key == {
        ('optimizer_proposals', 'user_id', 'user_id'),
        ('optimizer_proposals', 'proposal_id', 'id'),
    }
    assert proposal_columns['user_id']['dflt_value'] is None
    assert action_columns['user_id']['dflt_value'] is None
    assert (proposal_columns['user_id']['pk'], proposal_columns['id']['pk']) == (1, 2)
    assert (action_columns['user_id']['pk'], action_columns['id']['pk']) == (1, 2)
    assert {
        'idx_opt_prop_owner_created',
        'idx_opt_prop_owner_status',
        'idx_opt_prop_owner_action',
        'idx_opt_actlog_owner_proposal',
        'idx_opt_actlog_owner_applied',
        'idx_opt_actlog_owner_expires',
    }.issubset(indexes)
    assert int(version) == schema.SCHEMA_VERSION


@pytest.mark.skipif(
    os.environ.get('TOFU_STORAGE_TEST_POSTGRES') != '1',
    reason='real PostgreSQL migration parity is opt-in',
)
def test_schema_v37_migrates_postgres_foreign_keys_in_dependency_order():
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    from lib.storage_sidecar import schema
    from lib.storage_sidecar.adapters.postgres import PostgresSession

    dsn_file = Path(os.environ['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE'])
    dsn = dsn_file.read_text(encoding='utf-8').strip()
    namespace = f'tofu_optimizer_v37_{uuid.uuid4().hex}'
    connection = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
            cursor.execute(
                sql.SQL('SET search_path TO {}').format(sql.Identifier(namespace)))
            cursor.execute(
                "CREATE TABLE optimizer_proposals ("
                "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
                "title TEXT NOT NULL, rationale TEXT NOT NULL, "
                "action_type TEXT NOT NULL, action_args TEXT NOT NULL, "
                "severity TEXT NOT NULL DEFAULT 'low', "
                "confidence DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "evidence TEXT NOT NULL DEFAULT '', "
                "status TEXT NOT NULL DEFAULT 'pending_review', "
                "status_reason TEXT NOT NULL DEFAULT '')"
            )
            cursor.execute(
                "CREATE TABLE optimizer_action_log ("
                "id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL "
                "REFERENCES optimizer_proposals(id), "
                "applied_at TEXT NOT NULL, expires_at TEXT NOT NULL DEFAULT '', "
                "pre_metric TEXT NOT NULL DEFAULT '', "
                "outcome_metric TEXT NOT NULL DEFAULT '', "
                "outcome_recorded_at TEXT NOT NULL DEFAULT '', "
                "reverted_at TEXT NOT NULL DEFAULT '', "
                "revert_reason TEXT NOT NULL DEFAULT '')"
            )
            cursor.execute(
                "CREATE TABLE storage_meta ("
                "meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)"
            )
            cursor.execute(
                "INSERT INTO storage_meta VALUES ('schema_version', '36')"
            )
            cursor.execute(
                "INSERT INTO optimizer_proposals("
                "id, created_at, title, rationale, action_type, action_args) "
                "VALUES ('legacy-proposal', '2026-01-01', 'Legacy', "
                "'Personal row', 'set_limit', '{}')"
            )
            cursor.execute(
                "INSERT INTO optimizer_action_log(id, proposal_id, applied_at) "
                "VALUES ('legacy-action', 'legacy-proposal', '2026-01-02')"
            )

        session = PostgresSession(connection)
        schema.initialize_schema(session)
        connection.commit()

        assert session.fetch_one(
            'SELECT user_id, id FROM optimizer_proposals'
        ) == {'user_id': 1, 'id': 'legacy-proposal'}
        assert session.fetch_one(
            'SELECT user_id, id, proposal_id FROM optimizer_action_log'
        ) == {
            'user_id': 1,
            'id': 'legacy-action',
            'proposal_id': 'legacy-proposal',
        }
        assert session.fetch_one(
            'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
            ('schema_version',),
        ) == {'meta_value': str(schema.SCHEMA_VERSION)}
        assert session.fetch_one(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'optimizer_proposals' "
            "AND column_name = 'user_id'"
        ) == {'column_default': None}
    finally:
        connection.rollback()
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(
                    sql.Identifier(namespace)))
        connection.close()
