"""Temporary authority pressure cannot abandon or replay an accepted Goal effect."""
from types import SimpleNamespace
import threading
import time

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority
from tests.goals.test_goal_conversation_recovery import recovery as recovery
from tests.goals.test_goal_journal_retry import FaultingRuns, packets, run_effect
from lib.goal_runs.execution_journal import read_goal_execution
from lib.orchestration.execution_journal import RecoveryRefused

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.fixture
def clock(monkeypatch):
    from lib.tasks_pkg import persistence_wait
    from lib.storage import retry
    now = [0.0]
    def advance(seconds):
        now[0] += seconds
    fake = SimpleNamespace(time=time.time, monotonic=lambda: now[0], sleep=advance)
    monkeypatch.setattr(persistence_wait, 'time', fake)
    monkeypatch.setattr(retry, 'time', fake)
    return now


@pytest.mark.parametrize('phase', ['execution_journal_ready', 'execution_effect_completed'])
@pytest.mark.parametrize('after_commit', [False, True])
def test_long_pressure_retries_exact_packets_without_repeating_work(
        recovery, clock, phase, after_commit):
    class Pressure(FaultingRuns):
        def project_event(self, run_id, cursor, packet):
            self.persistent = len(self.attempts) < 8
            return super().project_event(run_id, cursor, packet)
    recovery.task['abort_event'] = threading.Event()
    runs = Pressure(recovery.runs, phase=phase, code='database_timeout', after_commit=after_commit)
    journal, runner, calls, snapshot = run_effect(recovery, runs)
    assert runner({'id': 'a'}, 'context', 0).output == 'settled'
    assert len(runs.attempts) == 9
    assert len(set(runs.attempts)) == 1
    assert calls == ['model/tool']
    _, records = read_goal_execution(packets(recovery),
        run=recovery.runs.get_execution_snapshot('goal_source-task'), owner_user_id=recovery.owner)
    assert next(iter(records.values()))['result']['worker_context'] == snapshot
    assert not journal.abort_requested()
    assert not recovery.task.get('_storageWaits')


@pytest.mark.parametrize('shutdown', [False, True])
def test_stop_preserves_completed_effect_but_shutdown_returns_pending_debt(recovery, clock, shutdown):
    class StopAfterEffect(FaultingRuns):
        def project_event(self, run_id, cursor, packet):
            if packet.get('type') == 'execution_effect_completed':
                recovery.task['abort_event'].set()
                if shutdown:
                    recovery.task['_abort_reason'] = 'server_shutdown'
            return super().project_event(run_id, cursor, packet)
    recovery.task['abort_event'] = threading.Event()
    runs = StopAfterEffect(recovery.runs, phase='execution_effect_completed', code='database_busy',
                           persistent=shutdown)
    journal, runner, calls, _ = run_effect(recovery, runs)
    if shutdown:
        with pytest.raises(RecoveryRefused):
            runner({'id': 'a'}, 'context', 0)
        assert journal.abort_requested()
        with pytest.raises(RecoveryRefused, match='external effects are unknown'):
            read_goal_execution(packets(recovery),
                run=recovery.runs.get_execution_snapshot('goal_source-task'), owner_user_id=recovery.owner)
    else:
        assert runner({'id': 'a'}, 'context', 0).output == 'settled'
        _, records = read_goal_execution(packets(recovery),
            run=recovery.runs.get_execution_snapshot('goal_source-task'), owner_user_id=recovery.owner)
        assert len(records) == 1
    assert calls == ['model/tool']
    assert len(runs.attempts) == (1 if shutdown else 2)


@pytest.mark.parametrize('reason', ['stop', 'deadline'])
def test_stop_during_admission_pressure_never_enters_operation(recovery, clock, reason):
    runs = FaultingRuns(recovery.runs, phase='execution_effect_started', code='database_busy',
                        persistent=True)
    recovery.task['abort_event'] = threading.Event()
    def stop(_):
        if reason == 'stop':
            recovery.task['abort_event'].set()
        else:
            recovery.task['config'].setdefault('executionLimits', {})['deadline'] = 0
    runs.on_attempt = stop
    journal, runner, calls, _ = run_effect(recovery, runs)
    with pytest.raises(RecoveryRefused):
        runner({'id': 'a'}, 'context', 0)
    assert calls == []
    assert len(runs.attempts) == 1
    assert journal.abort_requested()


def test_long_run_recurring_lost_ack_keeps_every_effect_exactly_once(recovery, clock):
    from lib.goal_runs.execution_journal import create_goal_execution_journal
    from lib.storage.errors import StorageError

    class RecurringPressure:
        def __init__(self):
            self.attempts = {}
        def __getattr__(self, name):
            return getattr(recovery.runs, name)
        def project_event(self, run_id, cursor, packet):
            if packet.get('type') == 'execution_effect_completed':
                count = self.attempts[cursor] = self.attempts.get(cursor, 0) + 1
                if count <= 6:
                    assert recovery.runs.project_event(run_id, cursor, packet)
                    raise StorageError('database_timeout', 'committed but ACK lost')
            return recovery.runs.project_event(run_id, cursor, packet)

    runs = RecurringPressure()
    journal = create_goal_execution_journal(recovery.task, recovery.definition, recovery.launch, runs=runs)
    calls = []
    runner = journal.runner(lambda node, _, iteration: calls.append(iteration) or {'output': str(iteration)},
        context_snapshot=lambda: {'messages': [{'role': 'user', 'content': 'full objective'}], 'task_state': {}},
        context_restore=lambda _: pytest.fail('live work unexpectedly replayed'))
    for iteration in range(100):
        assert runner({'id': 'a'}, 'context', iteration).output == str(iteration)
    _, records = read_goal_execution(packets(recovery),
        run=recovery.runs.get_execution_snapshot('goal_source-task'), owner_user_id=recovery.owner)
    assert calls == list(range(100))
    assert len(records) == 100
    assert list(runs.attempts.values()) == [7] * 100
    assert not recovery.task.get('_storageWaits')
