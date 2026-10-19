"""One persistence lifecycle independent of agent creation provenance."""
from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest

from lib.storage.errors import StorageError
from lib.tasks_pkg import persistence_wait as owner
from lib.tasks_pkg.persistence_wait import wait_for_persistence

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(owner, '_BASE_DELAY_SECONDS', 0.0)


def task(parent=False):
    return {'id': 'agent', 'config': {}, 'abort_event': Event(),
            '_parentTaskId': 'creator' if parent else None}


@pytest.mark.parametrize('parent', [False, True])
@pytest.mark.parametrize('code', ['database_busy', 'database_timeout', 'database_unavailable'])
def test_pressure_outlives_old_retry_ceiling_without_agent_failure(parent, code):
    carrier = task(parent)
    attempts = []
    previous = carrier['phase'] = {'phase': 'working'}
    def write():
        attempts.append(1)
        if len(attempts) <= 25:
            if len(attempts) > 1:
                assert carrier['_storageWaits']
                assert carrier['phase']['phase'] == 'retrying'
            raise StorageError(code, 'temporary pressure', retryable=False)
        return 'acknowledged'
    assert wait_for_persistence(carrier, write, label='exact checkpoint') == 'acknowledged'
    assert len(attempts) == 26
    assert carrier['phase'] is previous
    assert not carrier['_storageWaits']
    assert not carrier.get('aborted') and not carrier.get('error')


@pytest.mark.parametrize('parent', [False, True])
def test_stop_during_backpressure_prevents_another_dispatch(parent):
    from lib.llm.llm_errors import AbortedError
    carrier = task(parent)
    attempts = []
    def write():
        attempts.append(1)
        carrier['abort_event'].set()
        raise StorageError('database_busy', 'pressure')
    with pytest.raises(AbortedError):
        wait_for_persistence(carrier, write, label='intent')
    assert attempts == [1]
    assert carrier['aborted'] and not carrier['_storageWaits']


def test_terminal_evidence_commits_after_stop_without_changing_outcome():
    carrier = task()
    carrier.update(aborted=True, error=None, content='completed effect')
    attempts = []
    def write():
        attempts.append(1)
        if len(attempts) == 1:
            raise StorageError('database_busy', 'pressure')
        return True
    assert wait_for_persistence(carrier, write, label='terminal', cancellable=False)
    assert carrier['content'] == 'completed effect' and carrier['error'] is None
    assert len(attempts) == 2


def test_permanent_and_wrapped_transient_failures_have_distinct_verdicts():
    carrier = task()
    failure = StorageError('database_integrity', 'invalid checksum')
    with pytest.raises(StorageError) as raised:
        wait_for_persistence(carrier, lambda: (_ for _ in ()).throw(failure), label='write')
    assert raised.value is failure and not carrier['_storageWaits']
    attempts = []
    def wrapped():
        attempts.append(1)
        if len(attempts) == 1:
            try:
                raise StorageError('database_busy', 'pressure')
            except StorageError as error:
                raise RuntimeError('evidence not acknowledged') from error
        return True
    assert wait_for_persistence(carrier, wrapped, label='wrapped')
    assert len(attempts) == 2


def test_wait_does_not_mask_deadline_but_is_not_a_liveness_stall():
    from lib.tasks_pkg.invocation import invocation_abort_requested
    from lib.llm.llm_errors import AbortedError
    carrier = task()
    observations = []
    carrier['_invocationProgressBeacon'] = SimpleNamespace(
        is_making_progress=lambda key: not observations)
    def write():
        if not observations:
            observations.append('pressure')
            raise StorageError('database_busy', 'pressure')
        assert not invocation_abort_requested(carrier)
        carrier['_invocationDeadline'] = 0
        raise StorageError('database_busy', 'pressure')
    with pytest.raises(AbortedError):
        wait_for_persistence(carrier, write, label='write')
    assert carrier['_invocationStopReason'] == 'deadline'


def test_shutdown_retains_original_error_and_never_authorizes_effect():
    carrier = task()
    failure = StorageError('database_busy', 'pressure')
    attempts = []
    def write():
        attempts.append(1)
        carrier['_abort_reason'] = 'server_shutdown'
        raise failure
    with pytest.raises(StorageError) as raised:
        wait_for_persistence(carrier, write, label='terminal', cancellable=False)
    assert raised.value is failure and len(attempts) == 1


def test_swarm_command_reuses_frozen_receipt_after_lost_ack(monkeypatch):
    from lib.swarm import persistence
    attempts, committed = [], {}
    def command(operation, payload, command_id):
        attempts.append((operation, deepcopy(payload), command_id))
        committed.setdefault(command_id, deepcopy(payload))
        if len(attempts) == 1:
            raise StorageError('database_unavailable', 'lost acknowledgement')
        return {'ok': True}
    monkeypatch.setattr(persistence, '_storage', lambda **kw: SimpleNamespace(command=command))
    persistence.save_agent('wave', 'agent', role='coder', objective='verify', status='running',
        messages=[{'role': 'user', 'content': 'retained input'}], required=True, task=task())
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert len(committed) == 1


def test_registration_wait_keeps_child_identity_and_no_execution(monkeypatch):
    from lib.tasks_pkg.invocation_registration import register_child_invocation
    import lib.storage as storage
    import lib.tasks_pkg.manager as manager
    child = task(True)
    parent = {'id': 'creator', 'convId': 'conversation'}
    attempts = []
    def command(operation, payload, command_id):
        attempts.append((operation, deepcopy(payload), command_id))
        if len(attempts) <= 20:
            raise StorageError('database_busy', 'pressure')
        return {'archive_scope': 'owned-child'}
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kw: SimpleNamespace(command=command))
    monkeypatch.setattr(manager, 'task_user_id', lambda parent: 771)
    register_child_invocation(child, parent)
    assert child['_archiveScope'] == 'owned-child'
    assert len(attempts) == 21 and all(entry == attempts[0] for entry in attempts)


def test_common_retry_never_replays_an_admitted_tool(monkeypatch):
    from lib.tasks_pkg.orchestrator._finalize import _maybe_auto_retry_turn
    carrier = task()
    carrier.update(toolCallCount=1, error={'kind': 'network'}, config={})
    assert not _maybe_auto_retry_turn(carrier, {})


def test_nested_waits_keep_one_base_phase_and_do_not_grow_with_retries(record_property):
    import sys
    carrier = task()
    base = carrier['phase'] = {'phase': 'working'}
    outer_calls, inner_calls, sizes = [], [], []
    def inner():
        inner_calls.append(1)
        sizes.append(sys.getsizeof(carrier['_storageWaits']) + sum(
            sys.getsizeof(value) for value in carrier['_storageWaits'].values()))
        if len(inner_calls) <= 50:
            raise StorageError('database_busy', 'pressure')
        return True
    def outer():
        outer_calls.append(1)
        if len(outer_calls) == 1:
            raise StorageError('database_busy', 'pressure')
        assert wait_for_persistence(carrier, inner, label='inner')
        assert len(carrier['_storageWaits']) == 1
        return True
    assert wait_for_persistence(carrier, outer, label='outer')
    assert carrier['phase'] is base and not carrier['_storageWaits']
    assert len(set(sizes[1:])) == 1
    record_property('nested_wait_bytes', max(sizes))
    record_property('retry_count', len(inner_calls))
    assert not carrier.get('_storageWaitPreviousPhase')


def test_scheduler_beacon_retains_wait_witness_and_retires_on_ack(monkeypatch):
    from lib.swarm.liveness import ProgressBeacon
    clock = [1.0]
    monkeypatch.setattr('lib.swarm.liveness.time.monotonic', lambda: clock[0])
    carrier = task()
    beacon = carrier['_invocationProgressBeacon'] = ProgressBeacon(stall_timeout=1)
    carrier['_invocationProgressKey'] = 'agent'
    beacon.touch('agent')
    attempts = []
    def write():
        attempts.append(1)
        if len(attempts) == 1:
            raise StorageError('database_busy', 'pressure')
        clock[0] = 100
        assert beacon.is_making_progress('agent') and beacon.is_making_progress()
        assert not beacon.stalled_agents()
        return True
    assert wait_for_persistence(carrier, write, label='checkpoint')
    assert not beacon._persistence_waits
    clock[0] = 102
    assert not beacon.is_making_progress('agent')
    beacon.forget('agent')
    assert not beacon.tracked_agents()


@pytest.mark.parametrize('status', ['pending', 'done'])
def test_task_checkpoint_waits_with_same_receipt_and_per_rpc_budget(monkeypatch, status):
    from lib.tasks_pkg.manager import _persist
    import lib.storage as storage
    carrier = task()
    carrier.update(_userId=771, convId='', created_at=1, status=status)
    attempts = []
    def query(operation, payload, **kwargs):
        return None
    def command(operation, payload, command_id, **kwargs):
        attempts.append((deepcopy(payload), command_id, deepcopy(kwargs)))
        if len(attempts) <= 25:
            raise StorageError('database_busy', 'pressure')
        return {'version': 1, 'guard_contract': _persist.TASK_RESULT_CHECKPOINT_GUARD_CONTRACT}
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kw: SimpleNamespace(query=query, command=command))
    assert _persist._upsert_task_row(carrier, '', content='retained', thinking='', status=status,
        error_json=None, tr_json=None, meta_json=None,
        admission_budget_seconds=.5 if status == 'pending' else None)
    assert len(attempts) == 26 and all(row == attempts[0] for row in attempts)
    assert attempts[0][1] and not carrier['_storageWaits']
    if status == 'pending':
        assert attempts[0][2]['deadline'] == .5


@pytest.mark.parametrize('source', ['account', 'local'])
def test_execution_deadline_stops_storage_wait_before_another_write(source):
    from lib.llm.llm_errors import AbortedError
    carrier = task(True)
    attempts = []
    def write():
        attempts.append(1)
        if source == 'account':
            carrier['_executionBudget'] = SimpleNamespace(limits={'deadline': 0})
        else:
            carrier['config']['executionLimits'] = {'deadline': 0}
        raise StorageError('database_busy', 'pressure')
    with pytest.raises(AbortedError):
        wait_for_persistence(carrier, write, label='registration')
    assert attempts == [1]
    assert carrier['_executionLimitReason'] == 'execution_deadline_exceeded'
    assert not carrier['_storageWaits']
