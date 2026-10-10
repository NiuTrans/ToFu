"""Restart replay preserves completed effects and refuses ambiguous invocations."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import (
    ExecutionJournal, RecoveryRefused, read_journal,
)
from lib.orchestration.human_gate_runtime_ports import HumanGateRequestPorts
from lib.orchestration.run_recovery import load_recovery_records, RunRecoveryError
from lib.orchestration.runtime_start_service import OrchestrationRuntimeStartService

pytestmark = pytest.mark.unit


class Crash(BaseException):
    pass


def graph(*nodes):
    nodes = [{'id': 'start', 'type': 'control', 'kind': 'start'}, *nodes,
             {'id': 'stop', 'type': 'control', 'kind': 'stop'}]
    return {'schema': 'tofu.orchestration/v1', 'name': 'Recovery', 'nodes': nodes,
            'edges': [{'from': a['id'], 'to': b['id']} for a, b in zip(nodes, nodes[1:])]}


def role(name):
    return {'id': name, 'type': 'role', 'role': 'general', 'params': {'objective': name}}


def run(definition, runner, *, records=None, sink=None, **options):
    events = []
    def emit(event):
        if sink:
            sink(event)
        events.append(deepcopy(event))
    journal = ExecutionJournal(emit, records=records)
    journal.begin(definition, 'seed')
    result = FlowExecutor(definition, agent_runner=runner, on_event=emit,
                          execution_journal=journal, **options).run(initial_context='seed')
    if result['ok']:
        journal.verify_consumed()
    return result, events


def receipts(events, definition):
    result, parent = read_journal(events, definition=definition, initial_context='seed')
    assert not parent
    return result


def test_crash_after_committed_result_never_repeats_effect():
    definition = graph(role('a'), role('b'))
    calls, events = [], []
    def sink(event):
        events.append(deepcopy(event))
        if event['type'] == 'execution_effect_completed':
            raise Crash()
    with pytest.raises(Crash):
        run(definition, lambda n, *_: calls.append(n['id']) or {'output': 'done'}, sink=sink)
    result, replay = run(definition, lambda n, *_: calls.append(n['id']) or {'output': 'done'},
                         records=receipts(events, definition))
    assert result['ok']
    assert calls == ['a', 'b']
    assert result['agents_run'] == 2
    assert len(receipts(replay, definition)) == 2


def test_crash_during_effect_refuses_replay_even_without_reported_tools():
    definition, events = graph(role('a')), []
    def crash(*args):
        raise Crash()
    with pytest.raises(Crash):
        run(definition, crash, sink=lambda event: events.append(deepcopy(event)))
    with pytest.raises(RecoveryRefused, match='external effects are unknown'):
        receipts(events, definition)


def test_failed_admission_commit_dispatches_nothing():
    calls = []
    def sink(event):
        if event['type'] == 'execution_effect_started':
            raise RecoveryRefused('storage unavailable')
    with pytest.raises(RecoveryRefused):
        run(graph(role('a')), lambda *args: calls.append(args), sink=sink)
    assert not calls


def test_pending_human_gate_reissues_without_repeating_producer():
    definition = graph(role('a'), {'id': 'h', 'type': 'control', 'kind': 'human',
                                 'params': {'mode': 'approve'}}, role('b'))
    calls, events = [], []
    def crash(*args):
        raise Crash()
    with pytest.raises(Crash):
        run(definition, lambda n, *_: calls.append(n['id']) or {'output': n['id']},
            human_gate_owner_user_id=7, human_gate_ports=HumanGateRequestPorts(request_approval=crash),
            sink=lambda event: events.append(deepcopy(event)))
    approved = []
    result, _ = run(definition, lambda n, *_: calls.append(n['id']) or {'output': n['id']},
        records=receipts(events, definition), human_gate_owner_user_id=7,
        human_gate_ports=HumanGateRequestPorts(
            request_approval=lambda *args: approved.append(args) or True))
    assert result['ok'] and len(approved) == 1
    assert calls == ['a', 'b']


def test_fanout_reconstructs_stable_child_ids_and_dependencies():
    definition = graph({'id': 'fan', 'type': 'control', 'kind': 'fanout', 'params': {
        'specs_json': json.dumps([{'id': 'a', 'objective': 'a'},
                                 {'id': 'b', 'objective': 'b', 'depends_on': ['a']}])}})
    calls = []
    def runner(n, context, iteration):
        calls.append(n['id'])
        return {'output': n['params']['objective'], 'tool_names': ['write_file']}
    result, events = run(definition, runner, max_parallel=2)
    assert result['ok']
    prefix = events[:next(i for i, e in enumerate(events)
                         if e['type'] == 'execution_effect_completed') + 1]
    recovered, _ = run(definition, runner, records=receipts(prefix, definition), max_parallel=2)
    assert recovered['ok'] and len(calls) == 3
    assert calls[1] == calls[2] and calls[0] != calls[1]
    assert recovered['final'] == result['final']


def test_result_preserves_tool_accounting_and_long_output():
    definition = graph(role('a'))
    result, events = run(definition, lambda *_: {'output': 'x' * 30000,
        'tool_names': ['write_file', 'read_file'], 'thinking': 'evidence'})
    recovered, _ = run(definition, lambda *_: pytest.fail('replayed effect'),
                       records=receipts(events, definition))
    assert recovered['final'] == result['final']
    assert recovered['transcript'][0]['state_changing'] == result['transcript'][0]['state_changing']


def test_definition_input_and_receipt_corruption_fail_closed():
    definition = graph(role('a'))
    _, events = run(definition, lambda *_: {'output': 'done'})
    with pytest.raises(RecoveryRefused, match='does not match'):
        read_journal(events, definition={**definition, 'name': 'changed'}, initial_context='seed')
    with pytest.raises(RecoveryRefused, match='does not match'):
        read_journal(events, definition=definition, initial_context='changed')
    bad = deepcopy(events)
    next(e for e in bad if e['type'] == 'execution_effect_completed')['input_hash'] = 'wrong'
    with pytest.raises(RecoveryRefused, match='matching admission'):
        receipts(bad, definition)


class Runs:
    def __init__(self, definition, events):
        self.rows = {'old': {'id': 'old', 'definition': definition, 'input': 'seed',
            'status': 'error', 'error': {'kind': 'worker_lost',
                                       'source': 'orchestration.startup_recovery'}}}
        self.events = {'old': events}
    def get(self, key):
        return deepcopy(self.rows.get(key))
    def get_execution_snapshot(self, key):
        return self.get(key)
    def replay(self, key, cursor=0):
        events = self.events[key]
        return SimpleNamespace(events=events[cursor:cursor+3], next_cursor=min(cursor+3, len(events)),
                               cursor_reset=False, caught_up=cursor+3 >= len(events))
    def append_event(self, key, seq, event):
        assert seq == len(self.events[key])
        self.events[key].append(deepcopy(event))
        return True
    def create(self, key, **values):
        if key in self.rows:
            return False
        self.rows[key] = {'id': key, **values}
        return True


def test_resume_request_is_idempotent_and_owner_bound(monkeypatch):
    definition = graph(role('a'))
    _, events = run(definition, lambda *_: {'output': 'done'})
    runs, spawns = Runs(definition, events), []
    def spawn(*args, **kwargs):
        spawns.append(kwargs)
        return kwargs['task_id']
    monkeypatch.setattr('lib.orchestration.runtime_start_service.spawn_runtime_flow', spawn)
    service = OrchestrationRuntimeStartService(None,
        definition_service=lambda: SimpleNamespace(get_definition=lambda _: None), run_service=lambda: runs)
    first = service.resume('old', owner_user_id=9)
    assert service.resume('old', owner_user_id=9) == first
    assert len(spawns) == 1 and spawns[0]['owner_user_id'] == 9
    assert len(spawns[0]['recovery_records']) == 1
    with pytest.raises(RunRecoveryError, match='not found'):
        service.resume('other-owner', owner_user_id=9)


def test_incomplete_seed_loads_parent_but_complete_seed_is_independent():
    definition = graph(role('a'))
    _, events = run(definition, lambda *_: {'output': 'done'})
    runs = Runs(definition, events)
    copied = []
    journal = ExecutionJournal(copied.append, records=receipts(events, definition))
    journal.begin(definition, 'seed', parent_run_id='old')
    runs.rows['new'] = {**runs.rows['old'], 'id': 'new'}
    runs.events['new'] = copied[:1]
    _, result = load_recovery_records(runs, 'new')
    assert result == receipts(events, definition)
    runs.events['new'] = copied
    del runs.rows['old']
    _, result = load_recovery_records(runs, 'new')
    assert len(result) == 1


def test_normal_failure_and_user_abort_are_not_restart_recovery():
    definition = graph(role('a'))
    _, events = run(definition, lambda *_: {'output': 'done'})
    runs = Runs(definition, events)
    runs.rows['old']['status'] = 'aborted'
    with pytest.raises(RunRecoveryError, match='Only a run interrupted'):
        load_recovery_records(runs, 'old')


def test_isolated_subflows_replay_separate_occurrences_with_same_local_ids():
    child = graph(role('a'), role('b'))
    definition = graph(*[{'id': name, 'type': 'subflow', 'role': 'general',
                         'params': {'scope': 'isolated', 'definition': child}}
                        for name in ('left', 'right')])
    original, events = run(definition, lambda n, *_: {'output': n['id']})
    assert original['ok'], original
    records = receipts(events, definition)
    assert len(records) == 4
    recovered, _ = run(definition, lambda *_: pytest.fail('subflow executed again'), records=records)
    assert recovered['ok'] and recovered['final'] == original['final']
    assert recovered['agents_run'] == 4


def test_loop_replays_each_iteration_and_preserves_verdict():
    definition = graph(role('worker'))
    definition['nodes'] = [
        {'id': 's', 'type': 'control', 'kind': 'start'},
        {'id': 'l', 'type': 'control', 'kind': 'loop', 'params': {'max_iterations': 3}},
        role('worker'), {'id': 'critic', 'type': 'role', 'role': 'critic'},
        {'id': 'e', 'type': 'control', 'kind': 'stop'}]
    definition['edges'] = [{'from': a, 'to': b} for a, b in
                           [('s', 'l'), ('l', 'worker'), ('worker', 'critic'), ('critic', 'l'), ('l', 'e')]]
    def runner(node, _, iteration):
        if node['id'] == 'critic':
            return {'output': '[VERDICT: STOP]' if iteration == 2 else '[VERDICT: CONTINUE]'}
        return {'output': f'deliverable {iteration}', 'tool_log': [{
            'toolName': 'write_file', 'status': 'done',
            'toolContent': 'File created: new.txt', 'results': [{'writeOk': True}],
        }]}
    original, events = run(definition, runner)
    records = receipts(events, definition)
    assert len(records) == 4
    recovered, _ = run(definition, lambda *_: pytest.fail('loop effect repeated'), records=records)
    assert recovered['final'] == original['final'] and recovered['loop_exits'] == original['loop_exits']


def test_changed_reference_is_not_resolved_again_during_replay():
    child = graph(role('a'))
    definition = graph({'id': 'ref', 'type': 'subflow', 'role': 'general',
                        'params': {'scope': 'isolated', 'ref': 'stored-child'}})
    original, events = run(definition, lambda *_: {'output': 'kept'}, subflow_resolver=lambda _: child)
    assert original['ok'], original
    recovered, _ = run(definition, lambda *_: pytest.fail('repeated agent'),
                       records=receipts(events, definition),
                       subflow_resolver=lambda _: pytest.fail('changed stored graph read'))
    assert recovered['ok'] and recovered['final'] == original['final']


def test_scope_framing_does_not_alias_adversarial_node_names():
    events = []
    journal = ExecutionJournal(events.append)
    assert journal.child('a:b').identity('agent', 'c') != journal.child('a').identity('agent', 'b:c')


def test_committed_header_without_ready_proves_initial_dispatch_never_started():
    definition = graph(role('a'))
    events = []
    ExecutionJournal(events.append).begin(definition, 'seed')
    assert receipts(events[:1], definition) == {}


def test_concurrent_resume_requests_elect_only_one_launcher(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    definition = graph(role('a'))
    _, events = run(definition, lambda *_: {'output': 'done'})
    runs, spawns = Runs(definition, events), []
    def spawn(*args, **kwargs):
        spawns.append(kwargs['task_id'])
        return kwargs['task_id']
    monkeypatch.setattr('lib.orchestration.runtime_start_service.spawn_runtime_flow', spawn)
    service = OrchestrationRuntimeStartService(None,
        definition_service=lambda: SimpleNamespace(get_definition=lambda _: None), run_service=lambda: runs)
    with ThreadPoolExecutor(max_workers=8) as workers:
        identities = list(workers.map(lambda _: service.resume('old', owner_user_id=1), range(16)))
    assert len(set(identities)) == len(spawns) == 1


def test_required_step_publication_failure_revokes_journal_and_prevents_successor():
    from lib.orchestration.durable_projection import DurableProjectionError
    definition = graph(role('a'), role('b'))
    events, calls = [], []
    def emit(event):
        if event['type'] == 'step_complete':
            raise DurableProjectionError('step publication failed')
        events.append(event)
    journal = ExecutionJournal(emit)
    journal.begin(definition, 'seed')
    with pytest.raises(DurableProjectionError):
        FlowExecutor(definition, execution_journal=journal, on_event=emit,
                     agent_runner=lambda n, *_: calls.append(n['id']) or {'output': 'done'}).run(initial_context='seed')
    assert journal.abort_requested()
    assert calls == ['a']


def _production_journal_flow(monkeypatch, *, crash_at='', sink=None, records=None, definition=None):
    """Real graph/child owners, with only the model execution transport replaced."""
    import lib.tasks_pkg.child_invocation as children
    definition = definition or graph(role('a'))
    events, calls = [], []
    parent = {'id': 'effect-parent', 'convId': '', '_userId': 71,
              '_transientRuntime': True, 'config': {'model': 'chosen'},
              'messages': [{'role': 'user', 'content': 'seed'}]}
    def emit(event):
        events.append(deepcopy(event))
        if sink:
            sink(event)
    journal = ExecutionJournal(emit, records=records)
    journal.begin(definition, 'seed')
    if crash_at == 'preparation':
        def prepare(*args, **kwargs):
            raise Crash()
        monkeypatch.setattr(children, 'create_child_invocation', prepare)
    def execute(child, *, policy):
        assert any(e['type'] == 'execution_effect_started' for e in events)
        calls.append(child['id'])
        if crash_at == 'execution':
            raise Crash()
        child.update(content='settled', status='done', finishReason='stop',
                     streamState='provider_finished')
    monkeypatch.setattr(children, 'execute_invocation', execute)
    engine = FlowExecutor(definition, parent_task=parent, model='chosen',
                          model_routing_policy='selected', on_event=emit,
                          execution_journal=journal)
    return definition, events, calls, engine


def test_production_preparation_crash_replays_without_unknown_external_effect(monkeypatch):
    with monkeypatch.context() as patch:
        definition, events, calls, engine = _production_journal_flow(
            patch, crash_at='preparation')
        with pytest.raises(Crash):
            engine.run(initial_context='seed')
    assert not calls
    assert any(e['type'] == 'execution_effect_prepared' for e in events)
    assert not any(e['type'] == 'execution_effect_started' for e in events)
    records = receipts(events, definition)
    assert records == {}
    _, recovered_events, recovered_calls, recovered = _production_journal_flow(
        monkeypatch, records=records)
    assert recovered.run(initial_context='seed')['ok']
    assert len(recovered_calls) == 1
    assert len(receipts(recovered_events, definition)) == 1


def test_production_dispatched_crash_still_refuses_replay(monkeypatch):
    definition, events, calls, engine = _production_journal_flow(
        monkeypatch, crash_at='execution')
    with pytest.raises(Crash):
        engine.run(initial_context='seed')
    assert len(calls) == 1
    with pytest.raises(RecoveryRefused, match='external effects are unknown'):
        receipts(events, definition)


def test_production_admission_failure_cleans_child_and_dispatches_nothing(monkeypatch):
    import lib.tasks_pkg.child_invocation as children
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    original = children.create_child_invocation
    child_ids = []
    def create(*args, **kwargs):
        child = original(*args, **kwargs)
        child_ids.append(child['id'])
        return child
    monkeypatch.setattr(children, 'create_child_invocation', create)
    def fail(event):
        if event['type'] == 'execution_effect_started':
            raise RecoveryRefused('storage unavailable')
    _, _, calls, engine = _production_journal_flow(monkeypatch, sink=fail)
    with pytest.raises(RecoveryRefused):
        engine.run(initial_context='seed')
    assert not calls
    assert len(child_ids) == 1
    assert chat_task_runtime.get(child_ids[0]) is None


@pytest.mark.parametrize('corruption', ['changed_input', 'duplicate_preparation', 'completion_without_admission'])
def test_preparation_receipt_cannot_forge_execution_or_a_result(corruption):
    definition = graph(role('a'))
    events = []
    journal = ExecutionJournal(events.append)
    journal.begin(definition, 'seed')
    def prepare(admit):
        raise Crash()
    with pytest.raises(Crash):
        journal.call('agent', 'a', 0, ['input'], prepare, prepare_before_admission=True)
    event = deepcopy(events[-1])
    if corruption == 'changed_input':
        event.update(type='execution_effect_started', input_hash='changed')
    elif corruption == 'completion_without_admission':
        event.update(type='execution_effect_completed', result={'output': 'forged'})
    events.append(event)
    with pytest.raises(RecoveryRefused):
        receipts(events, definition)


def test_prepared_runner_cannot_complete_without_admission_or_admit_twice():
    definition = graph(role('a'))
    for twice in (False, True):
        events = []
        journal = ExecutionJournal(events.append)
        journal.begin(definition, 'seed')
        def operation(admit):
            if twice:
                admit()
                admit()
            return {'output': 'forged'}
        with pytest.raises(RecoveryRefused):
            journal.call('agent', 'a', 0, ['input'], operation, prepare_before_admission=True)
        assert not any(e['type'] == 'execution_effect_completed' for e in events)
        assert journal.abort_requested()


def test_child_monitor_start_crash_has_no_external_admission_and_releases_registry(monkeypatch):
    import lib.tasks_pkg.child_invocation as children
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    child_ids = []
    original = children.create_child_invocation
    def create(*args, **kwargs):
        child = original(*args, **kwargs)
        child_ids.append(child['id'])
        return child
    original_start = children.threading.Thread.start
    def start(thread):
        if thread.name.startswith('child-abort-'):
            raise Crash()
        return original_start(thread)
    monkeypatch.setattr(children, 'create_child_invocation', create)
    monkeypatch.setattr(children.threading.Thread, 'start', start)
    definition, events, calls, engine = _production_journal_flow(monkeypatch)
    with pytest.raises(Crash):
        engine.run(initial_context='seed')
    assert not calls
    assert receipts(events, definition) == {}
    assert len(child_ids) == 1
    assert chat_task_runtime.get(child_ids[0]) is None


def test_revoked_preparation_cannot_admit_an_external_effect():
    definition = graph(role('a'))
    events, effects = [], []
    journal = ExecutionJournal(events.append)
    journal.begin(definition, 'seed')
    def prepare(admit):
        journal.revoke()
        admit()
        effects.append('model/tool')
    with pytest.raises(RecoveryRefused):
        journal.call('agent', 'a', 0, ['input'], prepare, prepare_before_admission=True)
    assert not effects
    assert not any(e['type'] == 'execution_effect_started' for e in events)
    assert receipts(events, definition) == {}


def test_completed_production_leaf_is_preserved_when_next_leaf_crashes_in_preparation(monkeypatch):
    import lib.tasks_pkg.child_invocation as children
    definition = graph(role('a'), role('b'))
    original = children.create_child_invocation
    allocations = []
    def create(*args, **kwargs):
        if allocations:
            raise Crash()
        child = original(*args, **kwargs)
        allocations.append(child['id'])
        return child
    with monkeypatch.context() as patch:
        patch.setattr(children, 'create_child_invocation', create)
        _, events, calls, engine = _production_journal_flow(patch, definition=definition)
        with pytest.raises(Crash):
            engine.run(initial_context='seed')
    assert len(calls) == 1
    records = receipts(events, definition)
    assert len(records) == 1
    _, resumed_events, resumed_calls, resumed = _production_journal_flow(
        monkeypatch, records=records, definition=definition)
    assert resumed.run(initial_context='seed')['ok']
    assert len(resumed_calls) == 1
    assert len(receipts(resumed_events, definition)) == 2
