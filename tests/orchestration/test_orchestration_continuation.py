"""Durable decision admission, cancellation and effect-boundary continuation."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from tests.orchestration.test_orchestration_run_service import FakePersistence
from tests.orchestration.test_orchestration_execution_recovery import graph, role
from lib.orchestration.continuation import history
from lib.orchestration.run_service import OrchestrationRunService
from lib.orchestration.runtime_start_service import OrchestrationRuntimeStartService

pytestmark = pytest.mark.unit


class Store(FakePersistence):
    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()

    def update_status(self, run_id, status, **values):
        if self.runs[run_id]['status'] in ('done', 'error', 'aborted'):
            return False
        accepted = super().update_status(run_id, status, **values)
        if accepted:
            self.runs[run_id].update(values)
        return accepted

    def claim_event(self, run_id, seq, event, status=''):
        with self.lock:
            if self.runs[run_id]['status'] in ('done', 'error', 'aborted'):
                return False
            if any(row['seq'] == seq for row in self.events.get(run_id, [])):
                return False
            return super().project_event(run_id, seq, event, status)

    def project_event(self, run_id, seq, event, status=''):
        return self.claim_event(run_id, seq, event, status)


class Runtime:
    def __init__(self):
        self.tasks, self.spawns, self.before_spawn = {}, 0, None

    def create(self, *, task_id, **kwargs):
        task = {'id': task_id, 'abort_event': threading.Event(), 'events': []}
        self.tasks[task_id] = task
        return task

    def append_event(self, task_id, event):
        events = self.tasks[task_id]['events']
        events.append(event)
        return len(events) - 1

    def finish(self, task_id, **kwargs):
        self.tasks[task_id]['finished'] = kwargs
        return True

    def spawn(self, task_id, fn):
        self.spawns += 1
        if self.before_spawn:
            self.before_spawn(task_id)
        fn()

    def abort(self, task_id):
        task = self.tasks.get(task_id)
        if task:
            task['abort_event'].set()
        return SimpleNamespace(ok=bool(task))


@pytest.fixture
def scenario(monkeypatch):
    from lib.orchestration import runtime_service
    from lib.orchestration.engine import FlowExecutor
    calls, runtime, store = [], Runtime(), Store()
    runs = OrchestrationRunService(store, runtime_mutation=runtime)
    def executor(definition, **options):
        options['agent_runner'] = lambda node, *_: calls.append(node['id']) or {'output': node['id']}
        return FlowExecutor(definition, **options)
    monkeypatch.setattr(runtime_service, 'create_flow_executor', executor)
    starts = OrchestrationRuntimeStartService(runtime,
        definition_service=lambda: SimpleNamespace(get_definition=lambda _: None),
        run_service=lambda: runs)
    return starts, runs, runtime, store, calls


def parked(scenario, mode='input'):
    starts, runs, runtime, store, calls = scenario
    run_id = starts.start('durable', graph(role('a'),
        {'id': 'h', 'type': 'control', 'kind': 'human', 'params': {'mode': mode}},
        role('b')), owner_user_id=1)
    assert runs.get(run_id)['status'] == 'paused'
    events, _ = history(runs, run_id)
    assert calls == ['a']
    return run_id, events[-1]['request_id']


def test_concurrent_answers_dispatch_only_once(scenario):
    starts, runs, runtime, store, calls = scenario
    run_id, request = parked(scenario)
    barrier = threading.Barrier(8)
    def answer(_):
        barrier.wait()
        return starts.resolve_human_decision(request, 'yes', owner_user_id=1, mode='input')
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert any(pool.map(answer, range(8)))
    assert calls == ['a', 'b']
    assert runtime.spawns == 2
    assert runs.get(run_id)['status'] == 'done'
    assert not starts.resolve_human_decision(request, 'different', owner_user_id=1, mode='input')


@pytest.mark.parametrize('when', ['parked', 'handoff'])
def test_cancel_fences_a_parked_or_claimed_generation(scenario, when):
    starts, runs, runtime, store, calls = scenario
    run_id, request = parked(scenario)
    if when == 'handoff':
        runtime.before_spawn = lambda _: runs.abort(run_id)
        assert starts.resolve_human_decision(request, 'yes', owner_user_id=1, mode='input')
    else:
        assert runs.abort(run_id).ok
        assert not starts.resolve_human_decision(request, 'yes', owner_user_id=1, mode='input')
    assert runs.get(run_id)['status'] == 'aborted'
    assert calls == ['a']


@pytest.mark.parametrize('decision', [False, 'expired'])
def test_rejection_and_expiration_never_dispatch_followers(scenario, monkeypatch, decision):
    starts, runs, runtime, store, calls = scenario
    run_id, request = parked(scenario, 'approve')
    if decision == 'expired':
        monkeypatch.setattr('lib.orchestration.continuation.time', SimpleNamespace(time=lambda: 10**12))
        assert runs.get(run_id)['status'] == 'aborted'
        assert not starts.resolve_human_decision(request, True, owner_user_id=1, mode='approve')
    else:
        assert starts.resolve_human_decision(request, decision, owner_user_id=1, mode='approve')
    assert runs.get(run_id)['status'] == 'aborted'
    assert calls == ['a']


def test_decision_storage_failure_does_not_dispatch(scenario, monkeypatch):
    starts, runs, runtime, store, calls = scenario
    run_id, request = parked(scenario)
    monkeypatch.setattr(store, 'claim_event', lambda *args: False)
    assert not starts.resolve_human_decision(request, 'yes', owner_user_id=1, mode='input')
    assert runtime.spawns == 1
    assert runs.get(run_id)['status'] == 'paused'
    assert calls == ['a']


def test_parallel_human_waits_join_workers_and_reuse_sibling_effects(scenario):
    starts, runs, runtime, store, calls = scenario
    def control(name, kind, **params):
        return {'id': name, 'type': 'control', 'kind': kind, 'params': params}
    definition = {'schema': 'tofu.orchestration/v1', 'name': 'Parallel waits',
        'nodes': [control('s', 'start'), control('p', 'parallel'), role('a'), role('b'),
                  control('h1', 'human', mode='input'), control('h2', 'human', mode='input'),
                  control('join', 'barrier'), role('c'), control('e', 'stop')],
        'edges': [{'from': a, 'to': b} for a, b in [
            ('s', 'p'), ('p', 'a'), ('p', 'b'), ('a', 'h1'), ('b', 'h2'),
            ('h1', 'join'), ('h2', 'join'), ('join', 'c'), ('c', 'e')]]}
    run_id = starts.start('durable', definition, owner_user_id=1)
    assert sorted(calls) == ['a', 'b']
    assert not any(t.name.startswith('flow-par') for t in threading.enumerate())
    for _ in range(2):
        assert runs.get(run_id)['status'] == 'paused'
        events, _ = history(runs, run_id)
        assert starts.resolve_human_decision(events[-1]['request_id'], 'answer',
                                            owner_user_id=1, mode='input')
    assert sorted(calls) == ['a', 'b', 'c']
    assert runs.get(run_id)['status'] == 'done'
    events, _ = history(runs, run_id)
    # Continuations reference the same log: no inherited result body copies.
    assert not any(e.get('inherited') for e in events)


def test_interrupted_continuation_header_recovers_committed_decision(scenario):
    from lib.orchestration.execution_journal import ExecutionJournal, read_journal, RecoveryRefused
    from copy import deepcopy
    starts, runs, runtime, store, calls = scenario
    run_id, request = parked(scenario)
    runtime.spawn = lambda *args: None
    assert starts.resolve_human_decision(request, 'yes', owner_user_id=1, mode='input')
    events, _ = history(runs, run_id)
    definition = runs.get_execution_snapshot(run_id)['definition']
    records, _ = read_journal(events, definition=definition, initial_context='')
    new = []
    ExecutionJournal(new.append, records=records).begin(definition, '', continuation=True)
    assert read_journal(events + new[:1], definition=definition, initial_context='')[0] == records
    with pytest.raises(RecoveryRefused):
        read_journal(new, definition=definition, initial_context='')
    forged = {'type': 'execution_effect_completed', 'key': 'forged',
              'kind': 'human', 'input_hash': 'fake', 'result': {}, 'inherited': True}
    with pytest.raises(RecoveryRefused):
        read_journal(events + new[:1] + [forged], definition=definition, initial_context='')
    bad = deepcopy(new[:1])
    bad[0]['version'] = 99
    with pytest.raises(RecoveryRefused):
        read_journal(events + bad, definition=definition, initial_context='')


def test_failed_handoff_is_visible_and_decision_is_recoverable(scenario):
    from lib.orchestration.run_recovery import load_recovery_records
    starts, runs, runtime, store, calls = scenario
    run_id, request = parked(scenario)
    def fail(*args):
        raise RuntimeError('fixture spawn failure')
    runtime.spawn = fail
    with pytest.raises(RuntimeError, match='fixture spawn failure'):
        starts.resolve_human_decision(request, 'yes', owner_user_id=1, mode='input')
    assert runs.get(run_id)['status'] == 'error'
    source, records = load_recovery_records(runs, run_id)
    assert len(records) == 2
    assert calls == ['a']


def test_deadline_intent_survives_interrupted_terminal_publication(scenario, monkeypatch):
    from lib.orchestration.errors import RunServiceError
    starts, runs, runtime, store, calls = scenario
    run_id, request = parked(scenario, 'approve')
    monkeypatch.setattr('lib.orchestration.continuation.time', SimpleNamespace(time=lambda: 10**12))
    update = store.update_status
    def fail_terminal(run_id, status, **values):
        return False if status == 'aborted' else update(run_id, status, **values)
    monkeypatch.setattr(store, 'update_status', fail_terminal)
    with pytest.raises(RunServiceError):
        runs.get(run_id)
    assert store.runs[run_id]['status'] == 'paused'
    assert store.events[run_id][-1]['continuation_expired']
    monkeypatch.setattr(store, 'update_status', update)
    assert runs.get(run_id)['status'] == 'aborted'
    assert not starts.resolve_human_decision(request, True, owner_user_id=1, mode='approve')
    assert calls == ['a']
