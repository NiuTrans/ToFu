"""Built-in scheduler identities adopt legacy rows without duplicate runs."""

from __future__ import annotations

import sqlite3

import pytest


pytestmark = pytest.mark.unit


def test_ensure_adopts_legacy_builtin_and_retires_new_duplicate():
    from lib.storage_sidecar import schema
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.operations_pkg._timers import _scheduler_ensure

    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    schema.initialize_schema(session)
    connection.execute(
        "INSERT INTO storage_scheduled_tasks("
        "id,user_id,system_key,name,schedule,task_type,command,created_at,"
        "updated_at,run_count,last_run) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            'legacy-reclaim', 7, '', 'Billing Reserve Reclaim',
            '*/5 * * * *', 'reserve_reclaim', 'legacy-command',
            '2026-08-01T00:00:00', '2026-08-01T00:00:00', 200,
            '2026-08-24T23:55:00',
        ),
    )
    connection.execute(
        "INSERT INTO storage_scheduled_tasks("
        "id,user_id,system_key,name,schedule,task_type,command,created_at,"
        "updated_at,run_count,last_run) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            'system-7-billing-reserve-reclaim', 7,
            'billing-reserve-reclaim', 'Billing Reserve Reclaim',
            '*/5 * * * *', 'reserve_reclaim', 'new-command',
            '2026-08-24T00:00:00', '2026-08-24T00:00:00', 2,
            '2026-08-24T23:55:00',
        ),
    )
    connection.commit()

    result = _scheduler_ensure(session, {
        'system_key': 'billing-reserve-reclaim',
        'task_id': 'system-7-billing-reserve-reclaim',
        'user_id': 7,
        'name': 'Billing Reserve Reclaim',
        'schedule': '*/5 * * * *',
        'task_type': 'reserve_reclaim',
        'command': 'lib.billing.wallet_janitor.sweep_stale_reserves()',
        'description': 'Money-correctness safety net.',
        'enabled': False,
        'reconcile_enabled': True,
        'notify_on_failure': True,
        'notify_on_success': False,
        'max_runtime': 300,
        'created_at': '2026-08-25T00:00:00',
        'updated_at': '2026-08-25T00:00:00',
        'tools_config': {},
        'condition_kind': 'llm',
    })
    rows = connection.execute(
        "SELECT id,system_key,command,run_count,enabled "
        "FROM storage_scheduled_tasks "
        "WHERE user_id=7 AND name='Billing Reserve Reclaim'",
    ).fetchall()
    connection.close()

    assert result['created'] is False
    assert result['updated'] is True
    assert result['task']['id'] == 'legacy-reclaim'
    assert [tuple(row) for row in rows] == [(
        'legacy-reclaim',
        'billing-reserve-reclaim',
        'lib.billing.wallet_janitor.sweep_stale_reserves()',
        200,
        0,
    )]


def test_ensure_does_not_adopt_same_name_with_different_task_type():
    from lib.storage_sidecar import schema
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.operations_pkg._timers import _scheduler_ensure

    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    schema.initialize_schema(session)
    connection.execute(
        "INSERT INTO storage_scheduled_tasks("
        "id,user_id,name,schedule,task_type,command) VALUES(?,?,?,?,?,?)",
        (
            'user-owned-lookalike', 9, 'Billing Reserve Reclaim',
            '0 * * * *', 'prompt', 'Explain billing reserves',
        ),
    )
    connection.commit()

    result = _scheduler_ensure(session, {
        'system_key': 'billing-reserve-reclaim',
        'task_id': 'system-9-billing-reserve-reclaim',
        'user_id': 9,
        'name': 'Billing Reserve Reclaim',
        'schedule': '*/5 * * * *',
        'task_type': 'reserve_reclaim',
        'command': 'lib.billing.wallet_janitor.sweep_stale_reserves()',
        'tools_config': {},
    })
    rows = connection.execute(
        "SELECT id,task_type,system_key FROM storage_scheduled_tasks "
        "WHERE user_id=9 ORDER BY id",
    ).fetchall()
    connection.close()

    assert result['created'] is True
    assert [tuple(row) for row in rows] == [
        ('system-9-billing-reserve-reclaim',
         'reserve_reclaim', 'billing-reserve-reclaim'),
        ('user-owned-lookalike', 'prompt', ''),
    ]


@pytest.mark.parametrize('enabled', [False, True])
def test_manager_reconciles_reserve_builtin_enablement(monkeypatch, enabled):
    import lib.billing.wallet_janitor as wallet_janitor
    from lib.scheduler.manager import ScheduledTaskManager

    manager = ScheduledTaskManager()
    captured = {}
    monkeypatch.setattr(
        wallet_janitor, 'reserve_reclaim_enabled', lambda: enabled)
    monkeypatch.setattr(
        manager, '_ensure_default_task',
        lambda **kwargs: (captured.update(kwargs) or ({'id': 'reserve'}, False)))

    manager._ensure_default_reserve_reclaim_task()

    assert captured['enabled'] is enabled
    assert captured['reconcile_enabled'] is True


@pytest.mark.parametrize('missing', [False, True])
def test_daily_report_builtin_queues_startup_only_when_yesterday_is_missing(
        monkeypatch, missing):
    import lib.daily_report.storage as report_storage
    from lib.identity import PrincipalContext
    from lib.scheduler.manager import ScheduledTaskManager

    manager = ScheduledTaskManager()
    manager._process_principal = PrincipalContext.system(
        subject_id='scheduler-test',
        owner_user_id=23,
        scopes={'scheduler:run'},
    )
    captured = {}
    task = {'id': 'daily-report-task', 'user_id': 23}
    monkeypatch.setattr(
        manager, '_ensure_default_task',
        lambda **kwargs: (captured.update(kwargs) or (task, False)))
    monkeypatch.setattr(
        report_storage, '_load_report',
        lambda _date, *, owner_user_id: (
            None if missing else {'streams': []}))

    assert manager._ensure_default_daily_report_task() == task
    assert captured['system_key'] == 'daily-report-backfill'
    assert captured['task_type'] == 'daily_report_backfill'
    assert captured['schedule'] == '0 */6 * * *'
    assert captured['enabled'] is True
    assert captured['reconcile_enabled'] is True
    assert manager._take_startup_tasks() == (
        {'daily-report-task'} if missing else set())


def test_daily_report_task_rebuilds_exact_owner_maintenance_principal(
        monkeypatch):
    import lib.daily_report
    from lib.scheduler.manager import ScheduledTaskManager

    principals = []

    def backfill(*, principal):
        principals.append(principal)
        return {
            'ok': True,
            'status': 'saved',
            'date': '2026-08-26',
            'streams': 4,
        }

    monkeypatch.setattr(
        lib.daily_report, '_backfill_yesterday_if_missing', backfill)
    manager = ScheduledTaskManager.__new__(ScheduledTaskManager)
    ok, message = manager._execute_task({
        'id': 'daily-report-task',
        'user_id': 23,
        'task_type': 'daily_report_backfill',
        'command': 'ignored',
    })

    assert ok is True
    assert '"status": "saved"' in message
    assert len(principals) == 1
    assert principals[0].kind == 'system'
    assert principals[0].owner_user_id == 23
    assert principals[0].scopes == frozenset({'reports:maintain'})


def test_daily_report_task_is_dispatched_off_the_scheduler_tick(monkeypatch):
    from lib.scheduler.manager import ScheduledTaskManager

    manager = ScheduledTaskManager()
    calls = []
    monkeypatch.setattr(
        manager, '_dispatch_maintenance_task',
        lambda task: calls.append(('async', task['id'])))
    monkeypatch.setattr(
        manager, '_run_and_record',
        lambda task: calls.append(('inline', task['id'])))

    manager._dispatch_claimed_task({
        'id': 'daily-report-task',
        'task_type': 'daily_report_backfill',
    })
    assert calls == [('async', 'daily-report-task')]


def test_missing_report_startup_hint_claims_outside_cron_window(monkeypatch):
    import lib.scheduler.manager as manager_module
    from lib.identity import PrincipalContext

    task = {
        'id': 'daily-report-task',
        'user_id': 23,
        'task_type': 'daily_report_backfill',
        # Intentionally not due in August: startup recovery is a distinct hint.
        'schedule': '0 0 1 1 *',
    }

    class Client:
        @staticmethod
        def query(operation, payload):
            assert operation == 'scheduler.task.list_all'
            assert payload['enabled_only'] is True
            return [task]

    manager = manager_module.ScheduledTaskManager()
    manager._process_principal = PrincipalContext.system(
        subject_id='distributed-scheduler-test',
        scopes={'scheduler:run'},
    )
    manager._queue_startup_task(task['id'])
    claims = []
    dispatched = []
    monkeypatch.setattr(manager_module, '_scheduler_client', lambda **_kw: Client())
    monkeypatch.setattr(
        manager, '_claim_due_task',
        lambda claimed_task, _now: claims.append(claimed_task['id']) or True)
    monkeypatch.setattr(
        manager, '_dispatch_claimed_task',
        lambda claimed_task: dispatched.append(claimed_task['id']))

    manager._check_and_run_due_tasks()

    assert claims == ['daily-report-task']
    assert dispatched == ['daily-report-task']
    assert manager._take_startup_tasks() == set()


def test_startup_hint_survives_query_racing_before_task_insert(monkeypatch):
    import lib.scheduler.manager as manager_module
    from lib.identity import PrincipalContext

    class Client:
        @staticmethod
        def query(operation, payload):
            assert operation == 'scheduler.task.list_all'
            assert payload['enabled_only'] is True
            return []

    manager = manager_module.ScheduledTaskManager()
    manager._process_principal = PrincipalContext.system(
        subject_id='distributed-scheduler-test',
        scopes={'scheduler:run'},
    )
    manager._queue_startup_task('daily-report-task')
    monkeypatch.setattr(
        manager_module, '_scheduler_client', lambda **_kw: Client())

    manager._check_and_run_due_tasks()

    assert manager._take_startup_tasks() == {'daily-report-task'}
