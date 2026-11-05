"""The workspace-delivery resume loop works in proportion to deliveries needing action.

Idle ticks read nothing, however long the delivery history; a committed
delivery-state change wakes the worker; startup and error recovery resume every
pending receipt; held receipts keep their per-tick retry until they settle.
"""
import json
import threading
from pathlib import Path

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery

pytestmark = pytest.mark.unit

ROOT = '/fixture/canonical'
PAGE = 'integration.workspace.delivery_action_page'
PENDING = {'state': 'pending', 'phase': 'checkpoint', 'taskOutcome': 'completed'}
ACKNOWLEDGED = {
    'executionMode': 'isolated-task', 'deliveryCheckpoint': {'status': 'disabled'},
    'sourceRetirement': 'reclaimed',
    'delivery': {'state': 'delivered', 'taskOutcome': 'completed', 'publicationMode': 'working_tree'},
}
RETAINED = {**ACKNOWLEDGED, 'sourceRetirement': 'retained'}
COLD = (ACKNOWLEDGED, {'delivery': {'state': 'preserved', 'taskOutcome': 'failed'}},
        {'delivery': {'state': 'delivered'}}, {})
FRESH_SCHEDULE = (('_RECOVERY_CURSOR', 0), ('_RECOVERY_RETAINED', False),
                  ('_ACTION_OUTSTANDING', False), ('_SCAN_REQUESTED', False),
                  ('_RECOVERY_REQUESTED', False))


@pytest.fixture
def fresh_schedule(monkeypatch):
    for name, value in FRESH_SCHEDULE:
        monkeypatch.setattr(delivery, name, value)


@pytest.fixture
def storage(tmp_path, monkeypatch, fresh_schedule):
    from lib.storage import StorageSupervisor
    from lib.storage.runtime import StorageRuntime
    from lib.storage.service import install_runtime_for_test
    runtime = StorageRuntime(supervisor=StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    monkeypatch.setattr(ic, '_push', lambda *a, **k: None)
    try:
        yield
    finally:
        install_runtime_for_test(None)


def register(task, origin, *, managed=False):
    state.register_workspace(
        user_id=1, project_root=ROOT, task_id=task, title=task,
        workspace_path=f'/fixture/work/{task}', managed=managed, base_sha='a' * 40,
        now=1.0, origin=origin or None)


def stub_dispatch(monkeypatch):
    """Record each receiver call; held deliveries return their receipt unchanged."""
    calls = []
    def settle(row, receipt):
        calls.append(('settle', row['task_id']))
        return receipt
    def recover(row):
        calls.append(('recover', row['task_id']))
        return delivery._receipt(row)
    monkeypatch.setattr(delivery, '_settle_registered', settle)
    monkeypatch.setattr(delivery, '_recover_working_registration', recover)
    monkeypatch.setattr(delivery, '_checkpoint_delivered_workspace',
                        lambda row: calls.append(('checkpoint', row['task_id'])))
    monkeypatch.setattr(delivery, '_reclaim_completed_source',
                        lambda row: calls.append(('reclaim', row['task_id'])))
    return calls


def count_reads(monkeypatch):
    reads = []
    query = state._query
    def counted(operation, payload):
        reads.append(operation)
        return query(operation, payload)
    monkeypatch.setattr(state, '_query', counted)
    return reads


def drain():
    while delivery.resume_due_workspace_deliveries():
        pass


def narrow_pages(monkeypatch, size=2):
    """Serve real pages of ``size`` rows so a few rows span a multi-page pass."""
    retained = []
    page = state.delivery_action_page
    def narrow(after_id, _limit, **kwargs):
        retained.append(kwargs.get('retained'))
        return page(after_id, size, **kwargs)
    monkeypatch.setattr(state, 'delivery_action_page', narrow)
    return retained


def test_idle_ticks_read_nothing_however_long_the_history(storage, monkeypatch):
    for index in range(12):
        register(f'cold-{index:03}', COLD[index % len(COLD)], managed=True)
    calls = stub_dispatch(monkeypatch)
    reads = count_reads(monkeypatch)
    delivery.request_workspace_delivery_scan()
    assert delivery.resume_due_workspace_deliveries() is False
    assert reads == [PAGE], 'a pass costs one page read, never a read per row'
    reads.clear()
    for _ in range(5):
        assert delivery.resume_due_workspace_deliveries() is False
    assert reads == [] and calls == []


def test_boot_recovery_resumes_every_pending_receipt_and_rechecks_retained_sources(
        storage, monkeypatch):
    pending = [f'pending-{index}' for index in range(3)]
    for index, task in enumerate(pending):
        register(task, {'delivery': dict(PENDING)})
        register(f'cold-{index}', ACKNOWLEDGED, managed=True)
    register('retained', RETAINED, managed=True)
    register('orphan', {'delivery': {'state': 'working'}})
    calls = stub_dispatch(monkeypatch)
    retained = narrow_pages(monkeypatch)
    delivery.request_workspace_delivery_scan(recovery=True)
    drain()
    assert sorted(task for kind, task in calls if kind == 'settle') == pending
    assert ('recover', 'orphan') in calls
    assert [task for kind, task in calls if kind == 'reclaim'] == ['retained']
    assert retained == [True] * 3, 'every page of the recovery pass rechecks retained sources'
    calls.clear()
    retained.clear()
    # Held receipts are retried by the next pass; retained sources wait for recovery.
    drain()
    assert sorted(task for kind, task in calls if kind == 'settle') == pending
    assert ('recover', 'orphan') in calls
    assert not [task for kind, task in calls if kind == 'reclaim']
    assert retained == [False] * 2


def test_held_receipt_is_retried_each_tick_until_it_settles(storage, monkeypatch):
    register('held', {'delivery': {**PENDING, 'state': 'blocked',
                                   'reason': 'task_background_commands_running'}})
    for index in range(5):
        register(f'cold-{index:03}', ACKNOWLEDGED, managed=True)
    attempts = []
    def settle(row, receipt):
        attempts.append(row['task_id'])
        if len(attempts) < 3:
            return receipt  # The cause clears without a commit; only a retry notices.
        return delivery._save(row, receipt, state='preserved', reason='')
    monkeypatch.setattr(delivery, '_settle_registered', settle)
    delivery.request_workspace_delivery_scan()
    for _ in range(3):
        delivery.resume_due_workspace_deliveries()
    assert attempts == ['held'] * 3
    reads = count_reads(monkeypatch)
    for _ in range(3):
        assert delivery.resume_due_workspace_deliveries() is False
    assert reads == [PAGE], 'one empty pass confirms the settlement, then ticks are free'


def test_committed_pending_receipt_wakes_an_idle_worker(storage, monkeypatch):
    monkeypatch.setenv('TOFU_INTEGRATION_AUTORUN', '1')
    # The idle poll is far longer than the wait below: only the commit can wake it.
    monkeypatch.setenv('TOFU_INTEGRATION_IDLE_POLL_BASE_SECONDS', '30')
    monkeypatch.setenv('TOFU_INTEGRATION_IDLE_POLL_MAX_SECONDS', '600')
    register('task', {'executionMode': 'isolated-task'})
    for index in range(5):
        register(f'cold-{index:03}', ACKNOWLEDGED, managed=True)
    resumed = threading.Event()
    def settle(row, receipt):
        if row['task_id'] == 'task':
            resumed.set()
        return receipt
    monkeypatch.setattr(delivery, '_settle_registered', settle)
    passes, boot = [], threading.Event()
    page = state.delivery_action_page
    def observed(*args, **kwargs):
        result = page(*args, **kwargs)
        passes.append(kwargs.get('retained'))
        boot.set()
        return result
    monkeypatch.setattr(state, 'delivery_action_page', observed)
    assert ic.ensure_worker_started()
    try:
        assert boot.wait(60), 'the worker never ran its boot recovery pass'
        assert not resumed.is_set()
        state.set_workspace_meta(project_root=ROOT, task_id='task', user_id=1, now=2.0,
                                 patch={'delivery': dict(PENDING)})
        assert resumed.wait(20), 'a committed pending receipt waited for the idle poll'
    finally:
        assert ic.stop_worker(timeout=60)
    assert passes[0] is True, 'boot recovery also rechecks retained sources'
    assert passes[1] is False


def test_worker_error_requests_a_recovery_pass_and_idle_ticks_skip_the_scan(
        monkeypatch, fresh_schedule):
    from lib.storage import StorageError
    passes = []
    monkeypatch.setattr(delivery, 'resume_pending_workspace_deliveries',
                        lambda *, retained=False: passes.append(retained) or False)
    claims = iter([False, StorageError('database_unavailable', 'transient', retryable=True)])
    def process_ready_once():
        outcome = next(claims, False)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(ic, 'process_ready_once', process_ready_once)
    monkeypatch.setattr(ic, '_peek_ready', lambda: {'id': 1})
    autorun = iter([True] * 5)
    monkeypatch.setattr(ic, '_autorun_enabled', lambda: next(autorun, False))
    monkeypatch.setattr(ic, '_wait_for_worker', lambda *_args, **_kwargs: (True, False))
    ic._worker_loop()
    # Boot recovery, then recovery after the storage error; idle ticks read nothing.
    assert passes == [True, True]


def test_delivery_state_hints_name_catalog_commands():
    catalog = json.loads((Path(__file__).resolve().parents[2]
                          / 'contracts/storage_operations_v1.json').read_text())
    kinds = {operation['name']: operation['kind'] for operation in catalog['operations']}
    assert 'integration.workspace.set_meta' in delivery.DELIVERY_STATE_OPERATIONS
    assert {kinds.get(name) for name in delivery.DELIVERY_STATE_OPERATIONS} == {'command'}
