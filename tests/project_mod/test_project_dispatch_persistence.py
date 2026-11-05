"""Storage pressure waits at dispatch fences without replaying model/tool effects."""
from copy import deepcopy

import pytest

from lib.llm.llm_errors import AbortedError
from lib.project_dispatch import admission
from lib.storage.errors import StorageError
from tests.project_mod.test_project_dispatch_repository import context
from tests.project_mod.test_project_dispatch_runtime import _lease, runtime
from tests.project_mod.test_coordinator_workers import (
    _worker_task, base_runtime, runtime as worker_runtime,
)

__all__ = ('base_runtime', 'context', 'runtime', 'worker_runtime')
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def immediate_storage_retry(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0.0)


@pytest.mark.parametrize('code', ['database_timeout', 'database_busy', 'database_unavailable'])
@pytest.mark.parametrize('port', ['grant', 'attempt'])
def test_live_authority_waits_for_transient_reads(runtime, monkeypatch, code, port):
    task = runtime.task()
    admission.bind_account(task)
    owner, name = ((runtime.c.client, 'query') if port == 'grant'
                   else (runtime.c.repo, 'attempt_get'))
    original = getattr(owner, name)
    failures = []

    def read(*args, **kwargs):
        if len(failures) < 12:
            failures.append(code)
            assert not task.get('aborted')
            raise StorageError(code, 'deadline_elapsed', retryable=False)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, read)
    assert admission.validate_task(task) == runtime.binding
    assert len(failures) == 12
    assert not task.get('aborted') and not task['_storageWaits']


@pytest.mark.parametrize('phase', ['dispatch_intent', 'settled'])
@pytest.mark.parametrize('committed', [False, True])
def test_model_receipt_recovers_exactly_once(runtime, monkeypatch, phase, committed):
    task = runtime.task()
    admission.bind_account(task)
    first = _lease(task)
    first.mark_dispatched()
    first.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    lease = _lease(task)  # The final request allowed by this legacy finite grant.
    if phase == 'settled':
        lease.mark_dispatched()
    original = runtime.c.client.command
    failures = []

    def write(operation, payload, command_id):
        attempts = payload['value']['assignments'][0]['modelAttempts']
        if attempts[-1]['phase'] == phase and not failures:
            failures.append(deepcopy(attempts[-1]))
            if committed:
                original(operation, payload, command_id)
            raise StorageError('database_timeout', 'deadline_elapsed', retryable=False)
        return original(operation, payload, command_id)

    monkeypatch.setattr(runtime.c.client, 'command', write)
    if phase == 'dispatch_intent':
        lease.mark_dispatched()
    lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    rows = runtime.c.repo.read(runtime.assignment['id'])['modelAttempts']
    assert len(failures) == 1 and len(rows) == 2
    assert all(row['phase'] == 'settled' and row['usage_complete'] for row in rows)
    assert sum(row['reported_tokens'] for row in rows) == 20
    assert not task.get('aborted') and task.get('_executionActualsComplete') is not False


@pytest.mark.parametrize('retryable', [False, True])
def test_long_write_outage_preserves_storage_classification(runtime, monkeypatch, retryable):
    task = runtime.task()
    admission.bind_account(task)
    lease = _lease(task)
    original = runtime.c.client.command
    failures = []

    def write(*args):
        if len(failures) < 12:
            failures.append(True)
            raise StorageError('database_timeout', 'deadline_elapsed', retryable=retryable)
        return original(*args)

    monkeypatch.setattr(runtime.c.client, 'command', write)
    lease.mark_dispatched()
    lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    row, = runtime.c.repo.read(runtime.assignment['id'])['modelAttempts']
    assert len(failures) == 12 and row['usage_complete']
    assert not task.get('aborted')


@pytest.mark.parametrize('boundary', ['read', 'intent'])
@pytest.mark.parametrize('stop', ['human', 'revoked', 'identity'])
def test_wait_never_bypasses_a_later_stop(runtime, monkeypatch, boundary, stop):
    task = runtime.task()
    admission.bind_account(task)
    lease = _lease(task)
    owner, name = ((runtime.c.client, 'query') if boundary == 'read'
                   else (runtime.c.client, 'command'))
    original = getattr(owner, name)
    failures = []

    def unavailable(*args, **kwargs):
        if not failures:
            failures.append(True)
            if stop == 'human':
                task['abort_event'].set()
            elif stop == 'revoked':
                runtime.c.repo.revoke(runtime.assignment['id'], 1)
            else:
                task['_attemptId'] = 'replacement-attempt'
            raise StorageError('database_timeout', 'deadline_elapsed', retryable=False)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, unavailable)
    with pytest.raises(AbortedError):
        lease.mark_dispatched()
    assert not lease.dispatched
    assert task.get('_abort_reason') == {
        'human': None, 'revoked': 'dispatch_grant_revoked',
        'identity': 'dispatch_authority_lost',
    }[stop]
    assert len(failures) == 1


def test_observed_model_usage_is_persisted_after_human_stop(runtime, monkeypatch):
    task = runtime.task()
    admission.bind_account(task)
    lease = _lease(task)
    lease.mark_dispatched()
    original = runtime.c.client.command
    failures = []

    def write(*args):
        if not failures:
            failures.append(True)
            task['aborted'] = True
            task['abort_event'].set()
            raise StorageError('database_timeout', 'deadline_elapsed', retryable=False)
        return original(*args)

    monkeypatch.setattr(runtime.c.client, 'command', write)
    lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    row, = runtime.c.repo.read(runtime.assignment['id'])['modelAttempts']
    assert row['phase'] == 'settled' and row['reported_tokens'] == 10
    assert row['usage_complete'] and task.get('_executionActualsComplete') is not False
    assert '_abort_reason' not in task


@pytest.mark.parametrize('boundary', ['reservation', 'intent', 'settlement'])
@pytest.mark.parametrize('committed', [False, True])
def test_worker_tool_occurrences_recover_without_reentering_effects(
        worker_runtime, monkeypatch, boundary, committed):
    from tests.project_mod.test_coordinator_request_budget import bind
    r = worker_runtime
    bind(r)
    task, assignment = _worker_task(r)
    budget = task['_executionBudget']
    if boundary != 'reservation':
        assert budget.reserve_tool(task) is None
    if boundary == 'settlement':
        event = budget.begin_tool_attempt(task, 'read-once', 'read_files', 1)
    original = r.c.client.command
    failures = []

    def write(*args):
        if not failures:
            failures.append(True)
            if committed:
                original(*args)
            raise StorageError('database_timeout', 'deadline_elapsed', retryable=False)
        return original(*args)

    monkeypatch.setattr(r.c.client, 'command', write)
    if boundary == 'reservation':
        assert budget.reserve_tool(task) is None
    if boundary != 'settlement':
        event = budget.begin_tool_attempt(task, 'read-once', 'read_files', 1)
    budget.finish_tool_attempt(event, phase='executor_returned')
    row = r.c.repo.read(assignment['id'])
    assert len(failures) == 1 and row['toolReservations']['count'] == 1
    occurrence, = row['toolAttempts']
    assert occurrence['phase'] == 'executor_returned'
    assert occurrence['tool_call_id'] == 'read-once'
    assert not task.get('aborted') and budget.actuals_complete()


@pytest.mark.parametrize('code', ['database_integrity', 'database_forbidden'])
def test_permanent_read_failure_still_stops_dispatch(runtime, monkeypatch, code):
    task = runtime.task()
    admission.bind_account(task)
    failures = []

    def read(*args, **kwargs):
        failures.append(True)
        raise StorageError(code, 'permanent refusal')

    monkeypatch.setattr(runtime.c.client, 'query', read)
    with pytest.raises(AbortedError, match='permanent refusal'):
        admission.validate_task(task)
    assert failures == [True]
    assert task['_abort_reason'] == 'dispatch_authority_lost'
