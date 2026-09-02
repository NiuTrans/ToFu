"""Scheduler coverage for the canonical Sidecar backup task."""

from __future__ import annotations

import sqlite3

import pytest


pytestmark = pytest.mark.unit


class _MaintenanceClient:
    def __init__(self, result: dict | None = None):
        self.result = result or {
            'ok': True,
            'backup': 'data/backups/storage.sqlite3',
            'manifest': 'data/backups/storage.sqlite3.manifest.json',
            'bytes': 12 * 1024 * 1024,
        }
        self.calls: list[tuple[str, float | None]] = []

    def maintenance(self, operation: str, *, deadline: float | None = None):
        self.calls.append((operation, deadline))
        return self.result


def test_scheduler_dispatches_backup_through_the_storage_authority(monkeypatch):
    import lib.scheduler.manager as manager_module

    client = _MaintenanceClient()
    monkeypatch.setattr(manager_module, '_scheduler_client', lambda **_kwargs: client)
    monkeypatch.setattr(
        manager_module, '_application_managed_storage_backups_enabled', lambda: True)

    manager = manager_module.ScheduledTaskManager.__new__(
        manager_module.ScheduledTaskManager)
    ok, message = manager._execute_task({
        'id': 'backup-task',
        'user_id': 1,
        'task_type': 'storage_backup',
        'command': 'storage.system.backup',
        'max_runtime': 45,
    })

    assert ok is True
    assert client.calls == [('system.backup', 45.0)]
    assert 'data/backups/storage.sqlite3' in message
    assert '12.0 MiB' in message


def test_distributed_scheduler_never_invokes_application_backup(monkeypatch):
    import lib.scheduler.manager as manager_module

    client = _MaintenanceClient()
    monkeypatch.setattr(manager_module, '_scheduler_client', lambda **_kwargs: client)
    monkeypatch.setattr(
        manager_module, '_application_managed_storage_backups_enabled', lambda: False)

    manager = manager_module.ScheduledTaskManager.__new__(
        manager_module.ScheduledTaskManager)
    ok, message = manager._execute_task({
        'id': 'distributed-backup-task',
        'user_id': 1,
        'task_type': 'storage_backup',
        'command': 'storage.system.backup',
        'max_runtime': 45,
    })

    assert ok is False
    assert 'unavailable on this deployment' in message
    assert client.calls == []


def test_optimizer_task_rebuilds_the_durable_row_owner_principal(monkeypatch):
    import lib.optimizer
    import lib.scheduler.manager as manager_module

    principals = []

    def run_once(*, principal, dry_run):
        principals.append(principal)
        assert dry_run is False
        return {
            'proposals': [], 'applied': [], 'pending_review': [],
            'rejected': [], 'reverts': [],
        }

    monkeypatch.setattr(lib.optimizer, 'run_once', run_once)
    manager = manager_module.ScheduledTaskManager.__new__(
        manager_module.ScheduledTaskManager)
    ok, _ = manager._execute_task({
        'id': 'owner-task',
        'user_id': 23,
        'task_type': 'optimizer',
        'command': 'lib.optimizer.run_once()',
    })

    assert ok is True
    assert len(principals) == 1
    assert principals[0].kind == 'system'
    assert principals[0].owner_user_id == 23
    assert principals[0].scopes == frozenset({'optimizer:maintain'})


def test_schema_36_retires_database_specific_tasks_and_adds_system_identity(
        tmp_path):
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar import schema

    connection = sqlite3.connect(tmp_path / 'schema-v35.db')
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT)')
    connection.execute(
        'INSERT INTO storage_meta VALUES (?, ?)', ('schema_version', '35'))
    latest_task_table = next(
        statement for statement in schema._TABLES
        if 'CREATE TABLE IF NOT EXISTS storage_scheduled_tasks' in statement
    )
    connection.execute(latest_task_table.replace(
        "        system_key TEXT NOT NULL DEFAULT '',\n", ''))
    connection.execute(
        'INSERT INTO storage_scheduled_tasks(id, user_id, name, schedule, '
        'task_type, command) VALUES (?, ?, ?, ?, ?, ?)',
        ('old-backup', 1, 'Database Backup', '0 2 * * *', 'pg_backup', 'old'),
    )
    connection.execute(
        'INSERT INTO storage_scheduled_tasks(id, user_id, name, schedule, '
        'task_type, command) VALUES (?, ?, ?, ?, ?, ?)',
        ('user-task', 7, 'User prompt', '* * * * *', 'prompt', 'status'),
    )

    schema.initialize_schema(SQLiteSession(connection))

    columns = {
        row['name'] for row in connection.execute(
            'PRAGMA table_info(storage_scheduled_tasks)')
    }
    tasks = connection.execute(
        'SELECT id, system_key FROM storage_scheduled_tasks ORDER BY id'
    ).fetchall()
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    connection.close()

    assert int(version) == schema.SCHEMA_VERSION == 40
    assert 'system_key' in columns
    assert [tuple(row) for row in tasks] == [('user-task', '')]


@pytest.mark.parametrize(
    ('application_managed', 'expected_backup_bootstraps'),
    [(True, 1), (False, 0)],
)
def test_scheduler_bootstrap_respects_storage_backup_ownership(
        monkeypatch, application_managed, expected_backup_bootstraps):
    import lib.scheduler.manager as manager_module
    import lib.scheduler.timer as timer_module

    calls: list[str] = []

    class FakeManager:
        def start(self, *, principal):
            assert principal.owner_user_id == 1
            calls.append('start')

        def _ensure_default_optimizer_task(self):
            calls.append('optimizer')

        def _ensure_default_daily_report_task(self):
            calls.append('daily')

        def _ensure_default_storage_backup_task(self):
            calls.append('backup')

        def _ensure_default_reserve_reclaim_task(self):
            calls.append('reserve')

    fake = FakeManager()
    monkeypatch.delenv('TOFU_DISABLE_SCHEDULER', raising=False)
    monkeypatch.setattr(manager_module, 'get_scheduler', lambda: fake)
    monkeypatch.setattr(
        manager_module,
        '_application_managed_storage_backups_enabled',
        lambda: application_managed,
    )
    monkeypatch.setattr(timer_module, 'resume_active_timers', lambda: 0)

    from lib.identity import PrincipalContext

    principal = PrincipalContext.system(
        subject_id='scheduler-test',
        owner_user_id=1,
        scopes={'scheduler:run'},
    )
    assert manager_module.start_scheduler_worker(principal=principal) is fake
    assert calls[:3] == ['start', 'daily', 'optimizer']
    assert calls[-1] == 'reserve'
    assert calls.count('backup') == expected_backup_bootstraps


def test_ownerless_distributed_scheduler_resumes_without_personal_bootstraps(
        monkeypatch):
    import lib.scheduler.manager as manager_module
    import lib.scheduler.timer as timer_module
    from lib.identity import PrincipalContext

    calls: list[str] = []

    class FakeManager:
        def start(self, *, principal):
            assert principal.kind == 'system'
            assert principal.owner_user_id is None
            assert principal.scopes == frozenset({'scheduler:run'})
            calls.append('start')

        def _ensure_default_optimizer_task(self):
            calls.append('optimizer')

        def _ensure_default_daily_report_task(self):
            calls.append('daily')

        def _ensure_default_storage_backup_task(self):
            calls.append('backup')

        def _ensure_default_reserve_reclaim_task(self):
            calls.append('reserve')

    monkeypatch.delenv('TOFU_DISABLE_SCHEDULER', raising=False)
    monkeypatch.setattr(manager_module, 'get_scheduler', FakeManager)
    monkeypatch.setattr(
        manager_module,
        '_application_managed_storage_backups_enabled',
        lambda: True,
    )
    monkeypatch.setattr(
        timer_module, 'resume_active_timers',
        lambda: calls.append('resume') or 2)

    principal = PrincipalContext.system(
        subject_id='distributed-scheduler-test',
        scopes={'scheduler:run'},
    )
    manager_module.start_scheduler_worker(principal=principal)
    assert calls == ['start', 'resume']


def test_scheduler_entrypoint_rejects_ambient_or_unprivileged_identity(
        monkeypatch):
    import lib.scheduler.manager as manager_module
    from lib.identity import PrincipalContext

    monkeypatch.delenv('TOFU_DISABLE_SCHEDULER', raising=False)
    monkeypatch.setattr(
        manager_module, 'get_scheduler',
        lambda: pytest.fail('invalid principal must fail before manager access'))
    invalid_principals = (
        (None, TypeError),
        (
            PrincipalContext.user(
                subject_id='user-scheduler', owner_user_id=7,
                scopes={'scheduler:run'}),
            PermissionError,
        ),
        (
            PrincipalContext.system(
                subject_id='unprivileged-scheduler', scopes=set()),
            PermissionError,
        ),
    )
    for principal, error in invalid_principals:
        with pytest.raises(error):
            manager_module.start_scheduler_worker(principal=principal)
