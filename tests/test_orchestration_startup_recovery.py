"""Cross-owner orchestration startup recovery stays an explicit maintenance boundary."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


class _MaintenanceClient:
    def __init__(self):
        self.calls = []

    def maintenance(self, operation, payload=None, deadline=30.0):
        self.calls.append((operation, payload, deadline))
        return {'retired': 4}


def test_startup_recovery_uses_cross_owner_maintenance_operation():
    from lib.orchestration.startup_recovery import (
        retire_interrupted_orchestration_runs,
    )

    storage = _MaintenanceClient()
    factory_calls = []

    def factory(*, write=False):
        factory_calls.append(write)
        return storage

    error = {'kind': 'worker_lost'}
    assert retire_interrupted_orchestration_runs(
        error=error, client=factory) == 4
    assert factory_calls == [True]
    assert storage.calls == [(
        'orchestration.run.retire_interrupted_all',
        {'error': error},
        30.0,
    )]


def test_cross_owner_recovery_is_maintenance_not_user_command():
    from lib.storage_sidecar.operation_domains.workflows import OPERATIONS

    system_recovery = OPERATIONS[
        'orchestration.run.retire_interrupted_all']
    owner_recovery = OPERATIONS['orchestration.run.retire_interrupted']
    assert system_recovery.kind == 'maintenance'
    assert system_recovery.receipt_required is False
    assert owner_recovery.kind == 'command'


def test_cross_owner_handler_has_no_synthetic_personal_owner():
    from lib.storage_sidecar.operations_pkg._runs import (
        _orchestration_run_retire_all,
    )

    class Session:
        statement = ''
        params = ()

        def fetch_all(self, _statement, _params):
            return []

        def execute(self, statement, params):
            self.statement = statement
            self.params = params
            return 3

    session = Session()
    assert _orchestration_run_retire_all(
        session, {'error': {'kind': 'restart'}}) == {'retired': 3}
    assert 'user_id' not in session.statement
    assert 'tenant_id' not in session.statement
    assert 'status NOT IN' in session.statement
    assert not any(value == 1 for value in session.params)


def test_real_sidecar_retires_active_runs_across_owner_and_tenant_boundaries(
    tmp_path,
    monkeypatch,
):
    from lib.orchestration.startup_recovery import (
        retire_interrupted_orchestration_runs,
    )
    from lib.storage import StorageSupervisor

    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    monkeypatch.setenv('TOFU_STORAGE_RPC_CAPACITY', '4')
    supervisor = StorageSupervisor(
        project_root=tmp_path,
        backend='sqlite',
        startup_timeout=60,
    )
    supervisor.start()
    try:
        active_boundaries = (
            ('run-owner-7', 7, 'tenant-a'),
            ('run-owner-8', 8, 'tenant-b'),
        )
        for run_id, owner_user_id, tenant_id in active_boundaries:
            assert supervisor.client.command(
                'orchestration.run.create',
                {
                    'run_id': run_id,
                    'user_id': owner_user_id,
                    'tenant_id': tenant_id,
                    'definition': {'nodes': []},
                },
                f'create:{run_id}',
            ) == {'created': True}

        assert supervisor.client.command(
            'orchestration.run.create',
            {
                'run_id': 'run-terminal',
                'user_id': 7,
                'tenant_id': 'tenant-a',
                'definition': {'nodes': []},
            },
            'create:run-terminal',
        ) == {'created': True}
        assert supervisor.client.command(
            'orchestration.run.update_status',
            {
                'run_id': 'run-terminal',
                'user_id': 7,
                'tenant_id': 'tenant-a',
                'status': 'done',
                'final': 'kept',
            },
            'finish:run-terminal',
        ) == {'changed': True}

        def client_factory(*, write=False):
            assert write is True
            return supervisor.client

        assert retire_interrupted_orchestration_runs(
            error={'kind': 'worker_lost'},
            client=client_factory,
        ) == 2

        for run_id, owner_user_id, tenant_id in active_boundaries:
            row = supervisor.client.query(
                'orchestration.run.get',
                {
                    'run_id': run_id,
                    'user_id': owner_user_id,
                    'tenant_id': tenant_id,
                },
            )
            assert row['status'] == 'error'
            assert row['error'] == {'kind': 'worker_lost'}
        terminal = supervisor.client.query(
            'orchestration.run.get',
            {
                'run_id': 'run-terminal',
                'user_id': 7,
                'tenant_id': 'tenant-a',
            },
        )
        assert terminal['status'] == 'done'
        assert terminal['final'] == 'kept'
    finally:
        supervisor.stop()
