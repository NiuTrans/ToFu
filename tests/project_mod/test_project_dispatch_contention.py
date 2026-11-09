"""Dispatch contention is re-read and re-validated, never an authority verdict.

Incident 2026-11-06: ten long-running Workers stopped with dispatch_authority_lost,
"Concurrent/uncertain dispatch update; read before retrying". Every dispatch write
commits through one head per owner/project, so a Worker's own model settlement
lost its bounded CAS attempts to concurrent Coordinator and Worker writes, and to
the herd a storage stall released at once. Admission read that as lost authority,
a tool intent failed as a tool error, and Coordinator reads returned "retry
reading". Only a real verdict (revocation, Stop, changed binding or source) may
stop or refuse; contention re-reads under the persistence-wait liveness contract.
"""
import time
import uuid

import pytest

from lib.llm.llm_errors import AbortedError
from lib.project_dispatch import admission
from lib.project_dispatch.repository import LIMITS, NAMESPACE, DispatchContention
from lib.storage.errors import StorageError
from tests.project_mod.test_project_dispatch_repository import context
from tests.project_mod.test_project_dispatch_runtime import _lease, runtime as dispatch_runtime
from tests.project_mod.test_coordinator_workers import (
    _worker_task, base_runtime, runtime as coordinator_runtime,
)
from tests.project_mod.test_coordinator_request_budget import bind, spend, usage
from tests.project_mod.test_coordinator_continuation import run as _continuation_run

__all__ = ('base_runtime', 'context')
pytestmark = pytest.mark.unit

# More than one repository call's bounded CAS attempts: the loser must outlast
# several DispatchContention verdicts, not merely one lost compare-and-swap.
RACES = 2 * LIMITS['casAttempts'] + 1


@pytest.fixture
def runtime(context, monkeypatch):
    """The Worker dispatch runtime, defined here so its dependencies resolve."""
    return dispatch_runtime.__wrapped__(context, monkeypatch)


@pytest.fixture
def worker_runtime(base_runtime, monkeypatch):
    """The Coordinator worker runtime, with its own continuation helpers."""
    return coordinator_runtime.__wrapped__(base_runtime, monkeypatch)


@pytest.fixture
def continuation_run(worker_runtime, monkeypatch):
    """A live request with a Coordinator continuation, from the continuation suite."""
    return _continuation_run.__wrapped__(worker_runtime, monkeypatch)


@pytest.fixture(autouse=True)
def immediate_storage_retry(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0.0)


class RacingWriters:
    """Another writer commits between each read of the victim and its CAS."""

    def __init__(self, client, monkeypatch, races, *, write):
        self.left, self.write, self.inside = races, write, False
        self.original = client.command
        monkeypatch.setattr(client, 'command', self.command)

    def command(self, operation, args, command_id):
        if self.left and not self.inside:
            self.left -= 1
            self.inside = True
            try:
                self.write()
            finally:
                self.inside = False
        return self.original(operation, args, command_id)


def foreign_commit(client, key):
    """Any other dispatch write of the project: it advances the shared head."""
    def write():
        row = client.query('record.get', {'namespace': NAMESPACE, 'key': key})
        client.command('record.put', {'namespace': NAMESPACE, 'key': key, 'value': row['value'],
                                      'expected_version': row['version']}, 'other-writer:' + uuid.uuid4().hex)
    return write


def torn_snapshots(monkeypatch, repository, count, *, meanwhile=None):
    """``count`` segmented reads whose head advanced under them (their v2 verdict)."""
    load = repository._load
    torn = []

    def contended(*args, **kwargs):
        if len(torn) < count:
            torn.append(True)
            if meanwhile is not None:
                meanwhile()
            raise DispatchContention('Concurrent/uncertain dispatch snapshot; retry reading') from StorageError(
                'database_conflict', 'Dispatch snapshot advanced', retryable=True)
        return load(*args, **kwargs)

    monkeypatch.setattr(repository, '_load', contended)
    return torn


@pytest.mark.parametrize('phase', ['dispatch_intent', 'settled'])
def test_racing_dispatch_writes_never_stop_a_workers_model_occurrence(runtime, monkeypatch, phase):
    r = runtime
    task = r.task()
    admission.bind_account(task)
    lease = _lease(task)
    if phase == 'settled':
        lease.mark_dispatched()
    coordinator = r.c.repository()
    delegated = []

    def delegate():
        delegated.append(coordinator.authorize(r.c.request, r.c.principal, f'race-{len(delegated)}'))

    race = RacingWriters(r.c.client, monkeypatch, RACES, write=delegate)
    if phase == 'dispatch_intent':
        lease.mark_dispatched()
    lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    assert race.left == 0 and len(delegated) == RACES
    row, = r.c.repo.read(r.assignment['id'])['modelAttempts']
    assert row['phase'] == 'settled' and row['reported_tokens'] == 10 and row['usage_complete']
    assert not task.get('aborted') and '_abort_reason' not in task
    assert task.get('_executionActualsComplete') is not False
    assert {grant['id'] for grant in delegated} <= {grant['id'] for grant in r.c.repo.list()}


@pytest.mark.parametrize('boundary', ['reservation', 'intent', 'settlement'])
def test_racing_dispatch_writes_never_fail_a_workers_tool_occurrence(worker_runtime, monkeypatch, boundary):
    r = worker_runtime
    bind(r)
    task, assignment = _worker_task(r)
    budget = task['_executionBudget']
    if boundary != 'reservation':
        assert budget.reserve_tool(task) is None
    if boundary == 'settlement':
        event = budget.begin_tool_attempt(task, 'read-once', 'read_files', 1)
    race = RacingWriters(r.c.client, monkeypatch, RACES, write=foreign_commit(r.c.client, r.c.repo.key))
    if boundary == 'reservation':
        assert budget.reserve_tool(task) is None
    if boundary != 'settlement':
        event = budget.begin_tool_attempt(task, 'read-once', 'read_files', 1)
    budget.finish_tool_attempt(event, phase='executor_returned')
    assert race.left == 0
    row = r.c.repo.read(assignment['id'])
    assert row['toolReservations']['count'] == 1
    occurrence, = row['toolAttempts']
    assert occurrence['phase'] == 'executor_returned' and occurrence['tool_call_id'] == 'read-once'
    assert not task.get('aborted') and budget.actuals_complete()


def test_torn_authority_snapshots_are_reread_not_refused(runtime, monkeypatch):
    r = runtime
    task = r.task()
    admission.bind_account(task)
    torn = torn_snapshots(monkeypatch, r.c.repo, 3)
    assert admission.validate_task(task) == r.binding
    assert len(torn) == 3
    assert not task.get('aborted') and '_abort_reason' not in task and not task['_storageWaits']


@pytest.mark.parametrize('contended', [False, True])
def test_a_real_revocation_still_stops_at_once(runtime, monkeypatch, contended):
    r = runtime
    task = r.task()
    admission.bind_account(task)
    coordinator = r.c.repository()
    if contended:
        # The Coordinator cancels while the Worker's read is torn: the re-read
        # meets the revocation and stops on it, it does not keep waiting.
        torn = torn_snapshots(monkeypatch, r.c.repo, 1,
                              meanwhile=lambda: coordinator.revoke(r.assignment['id'], 1))
    else:
        coordinator.revoke(r.assignment['id'], 1)
    with pytest.raises(AbortedError, match='Grant revoked'):
        admission.validate_task(task)
    assert task['_abort_reason'] == 'dispatch_grant_revoked'
    assert 'Grant revoked' in task['_abortReasonDetail']
    assert not contended or torn == [True]


@pytest.mark.parametrize('boundary', ['read', 'intent'])
def test_a_stop_during_contention_keeps_the_human_cause(runtime, monkeypatch, boundary):
    r = runtime
    task = r.task()
    admission.bind_account(task)
    lease = _lease(task)

    def stop():
        task['abort_event'].set()

    if boundary == 'read':
        torn_snapshots(monkeypatch, r.c.repo, 1, meanwhile=stop)
    else:
        other = foreign_commit(r.c.client, r.c.repo.key)
        RacingWriters(r.c.client, monkeypatch, LIMITS['casAttempts'], write=lambda: (stop(), other()))
    with pytest.raises(AbortedError):
        lease.mark_dispatched()
    assert not lease.dispatched
    assert task.get('_abort_reason') is None


@pytest.mark.parametrize('port', ['occurrence', 'authority'])
def test_coordinator_request_account_rereads_through_contention(worker_runtime, monkeypatch, port):
    r = worker_runtime
    source = bind(r)
    if port == 'occurrence':
        race = RacingWriters(r.c.client, monkeypatch, RACES, write=foreign_commit(r.c.client, r.c.repo.key))
    else:
        torn = torn_snapshots(monkeypatch, r.c.repo, 3)
    spend(r.task, 10)
    assert race.left == 0 if port == 'occurrence' else len(torn) == 3
    state = usage(r, source)
    assert (state['tokens'], state['modelRequests']) == (10, 1) and state['usageComplete']
    assert not r.task.get('aborted')


def _delegated(r):
    r.c.now[0] = time.time()
    r.attempts['coordinator-attempt']['createdAt'] = int(r.c.now[0] * 1000)
    return r.call('delegate_project_work', **r.args)


def test_delegation_reruns_through_contention_instead_of_returning_uncertain(worker_runtime, monkeypatch):
    r = worker_runtime
    torn = torn_snapshots(monkeypatch, r.c.repo, 3)
    result = _delegated(r)
    assert 'error' not in result, result
    assert len(torn) == 3
    assert r.call('delegate_project_work', **r.args)['assignment_id'] == result['assignment_id']
    assert [row['id'] for row in r.c.repo.list()] == [result['assignment_id']]


@pytest.mark.parametrize('tool', ['get_project_workers', 'get_project_assignment', 'await_project_workers'])
def test_coordinator_reads_never_return_dispatch_contention(worker_runtime, monkeypatch, tool):
    r = worker_runtime
    delegated = _delegated(r)
    arguments = {'get_project_workers': {},
                 'get_project_assignment': {'assignment_id': delegated['assignment_id']},
                 'await_project_workers': {'assignment_ids': [delegated['assignment_id']],
                                           'timeout_seconds': 0}}[tool]
    torn = torn_snapshots(monkeypatch, r.c.repo, 3)
    result = r.call(tool, **arguments)
    assert 'error' not in result, result
    assert len(torn) == 3


def test_an_unacknowledged_tool_settlement_is_named_not_reported_as_a_budget(worker_runtime, monkeypatch):
    # No maxToolCalls is set. A settlement whose durable ACK fails leaves its
    # intent; the next effect is refused with that occurrence named.
    r = worker_runtime
    bind(r)
    task, assignment = _worker_task(r)
    budget = task['_executionBudget']
    assert budget.reserve_tool(task) is None
    event = budget.begin_tool_attempt(task, 'lost-ack', 'run_command', 1)
    original = admission.wait_for_dispatch

    def lose_settlement(task_, operation, *, label, **kwargs):
        if label == 'project dispatch tool occurrence':
            raise RuntimeError('settlement ACK lost')
        return original(task_, operation, label=label, **kwargs)
    monkeypatch.setattr(admission, 'wait_for_dispatch', lose_settlement)
    budget.finish_tool_attempt(event, phase='executor_returned')
    monkeypatch.setattr(admission, 'wait_for_dispatch', original)
    assert r.c.repo.read(assignment['id'])['toolAttempts'][0]['phase'] == 'intent'
    assert budget.reserve_tool(task) is None
    with pytest.raises(AbortedError) as refused:
        budget.begin_tool_attempt(task, 'next', 'write_file', 2)
    message = str(refused.value)
    assert 'not a call budget' in message and 'run_command #1 lost-ack phase=intent' in message
    assert 'exhausted' not in message


@pytest.mark.parametrize('boundary', ['read', 'commit'])
def test_review_project_work_rereads_through_contention(continuation_run, monkeypatch, boundary):
    """A contended review is re-read and recorded, never returned as 'uncertain'.

    Incident 2026-11-06 16:15 CST: with about 17 Workers live,
    review_project_work returned {"error": "Concurrent/uncertain dispatch
    update; read before retrying"} and the Coordinator replayed it by hand.
    """
    from tests.project_mod.test_coordinator_continuation import activate, ready
    r = continuation_run
    worker = ready(r)
    activate(r)
    arguments = {'assignment_id': worker['id'], 'result_revision': 1,
                 'decision': 'accept', 'note': 'Verified evidence'}
    if boundary == 'read':
        torn = torn_snapshots(monkeypatch, r.c.repo, 3)
    else:
        RacingWriters(r.c.client, monkeypatch, RACES, write=foreign_commit(r.c.client, r.c.repo.key))
    reviewed = r.call('review_project_work', **arguments)
    assert reviewed.get('decision') == 'accept', reviewed
    assert r.c.repo.read(worker['id'])['review'] == {
        'actor': 'coordinator', 'decision': 'accept', 'note': 'Verified evidence', 'resultRevision': 1}
    assert not r.task.get('aborted')
    # Replaying the identical review is a no-op; a different one is immutable.
    assert r.call('review_project_work', **arguments) == reviewed
    assert 'immutable' in r.call('review_project_work', **{**arguments, 'note': 'Changed'})['error']


def test_a_real_verdict_during_a_contended_review_still_wins(continuation_run, monkeypatch):
    """Contention is never a licence to keep reviewing a revoked grant."""
    from tests.project_mod.test_coordinator_continuation import activate, ready
    r = continuation_run
    worker = ready(r)
    activate(r)
    arguments = {'assignment_id': worker['id'], 'result_revision': 1,
                 'decision': 'accept', 'note': 'Verified evidence'}

    def revoke_meanswhile():
        r.c.repo.revoke(worker['id'], worker['grant']['revision'])

    torn = torn_snapshots(monkeypatch, r.c.repo, 1, meanwhile=revoke_meanswhile)
    result = r.call('review_project_work', **arguments)
    assert 'Grant revoked' in result.get('error', ''), result
    assert r.c.repo.read(worker['id'])['review'] is None
    assert torn == [True]


@pytest.mark.parametrize('boundary', ['read', 'commit'])
def test_cancel_project_work_rereads_through_contention(continuation_run, monkeypatch, boundary):
    from tests.project_mod.test_coordinator_continuation import activate, delegate, ready
    r = continuation_run
    cancelled_worker = ready(r)
    activate(r)
    other = delegate(r, 'other')
    stopped = []
    monkeypatch.setattr(r.service, '_cancel_execution',
                        lambda attempt_id, owner, provenance: stopped.append((attempt_id, provenance)))
    if boundary == 'read':
        torn = torn_snapshots(monkeypatch, r.c.repo, 3)
    else:
        RacingWriters(r.c.client, monkeypatch, RACES, write=foreign_commit(r.c.client, r.c.repo.key))
    result = r.call('cancel_project_work', assignment_id=cancelled_worker['id'])
    assert result.get('revoked') is True, result
    grant = r.c.repo.read(cancelled_worker['id'])['grant']
    assert grant['revoked'] is True and grant['revision'] == cancelled_worker['grant']['revision'] + 1
    assert [attempt for attempt, _ in stopped] == [cancelled_worker['execution']['attemptId']]
    assert all('cancel_project_work' in provenance for _, provenance in stopped)
    # A replay is the same cancellation retried: the grant does not advance again.
    replay = r.call('cancel_project_work', assignment_id=cancelled_worker['id'])
    assert replay == result
    assert r.c.repo.read(cancelled_worker['id'])['grant'] == grant
    # Another assignment's authority is untouched.
    assert r.c.repo.read(other['id'])['grant']['revoked'] is False


def test_finish_project_request_rereads_through_contention(continuation_run, monkeypatch):
    from tests.project_mod.test_coordinator_continuation import activate, ready, report
    r = continuation_run
    worker = ready(r)
    activate(r)
    arguments = report(r)
    reviewed = r.call('review_project_work', assignment_id=worker['id'],
                      result_revision=1, decision='accept', note='Verified')
    assert reviewed['decision'] == 'accept'
    torn = torn_snapshots(monkeypatch, r.c.repo, 4)
    done = r.call('finish_project_request', **arguments)
    assert done.get('reason') == 'complete', done
    assert len(torn) == 4
    # The receipt is immutable and its replay is a no-op.
    assert r.call('finish_project_request', **arguments) == done
    assert 'settled' in r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'late'})['error']


def test_request_user_decision_rereads_through_contention(continuation_run, monkeypatch):
    from tests.project_mod.test_coordinator_continuation import (
        _bound_request, _decision_report, delegate,
    )
    r = continuation_run
    _bound_request(r)
    delegate(r)
    report_id = _decision_report(r)
    timers = []
    monkeypatch.setattr('lib.project_dispatch.continuation.schedule_wake',
                        lambda owner, project, due, **kw: timers.append(due))
    torn = torn_snapshots(monkeypatch, r.c.repo, 4)
    result = r.call('request_user_decision', report_turn_id=report_id,
                    recommendation='Use option A', wait_seconds=600)
    assert 'error' not in result, result
    assert len(torn) == 4
    due = result['pending_decision']['dueAt']
    assert timers == [due] and due == pytest.approx(r.c.now[0] + 600)
    # The replay keeps the FIRST due time rather than renewing the deadline.
    assert r.call('request_user_decision', report_turn_id=report_id,
                  recommendation='Use option A', wait_seconds=60)['pending_decision']['dueAt'] == due
