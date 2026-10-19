"""Prepared registered children retain durable completion and lost-ACK recovery."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig
from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import ExecutionJournal, read_journal
from lib.orchestration.invocation_recovery import completion_resolver, FIELD
from lib.orchestration.prepared_recipients import prepare_flow_recipients
from tests.orchestration.test_orchestration_execution_recovery import graph, role, Crash
from tests.orchestration.test_orchestration_recovery_frontier import source, inspect
from tests.turn_lifecycle.test_task_event_delivery import (
    storage as storage, owner as owner, _task, _discard,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def turn_service(storage, monkeypatch):
    from lib import turn_lifecycle
    from lib.storage import service
    # Keep the exported accessor stable: lazily imported domains must not retain
    # a fixture lambda after this disposable authority has closed.
    runtime = SimpleNamespace(client=lambda **kwargs: storage.client)
    monkeypatch.setattr(service, 'storage_runtime', lambda: runtime)
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    return turn_lifecycle


@pytest.mark.parametrize('lost_completion_ack', [False, True])
def test_prepared_registered_child_completes_with_original_witness_after_lost_ack(
        storage, turn_service, owner, monkeypatch, lost_completion_ack):
    _, parent = _task(turn_service, storage, 'prepared-flow-completion')
    definition = graph(role('worker'), role('after'))
    records, pending, _ = source(definition)
    inputs = inspect(definition, records, pending)
    config = OrchestrationAgentRunnerConfig(parent_task=parent, model='fixture', model_routing_policy='selected')
    events, calls, committed = [], [], []
    command = storage.client.command
    def checkpoint(operation, payload, *args, **kwargs):
        result = command(operation, payload, *args, **kwargs)
        if operation == 'task_results.checkpoint' and FIELD in payload.get('value', {}):
            committed.append(deepcopy(payload))
            if lost_completion_ack and len(committed) == 1:
                raise Crash()
        return result
    monkeypatch.setattr(storage.client, 'command', checkpoint)
    def execute(child, *, policy):
        started = [event for event in events if event['type'] == 'execution_effect_started'][-1]
        assert started['invocation_id'] == child['id']
        prepared = next(row for row in events if row['type'] == 'execution_effect_prepared'
                        and row['key'] == started['key'])
        if child['id'] == prepared_child['id']:
            assert prepared['invocation_id'] == child['id']
            assert prepared['invocation_completion'] == started['invocation_completion']
        identity = storage.client.query('invocation.resolve_child', dict(user_id=1,
            archive_scope=child['_archiveScope']))
        assert identity['parent_task_id'] == parent['id'] and identity['child_task_id'] == child['id']
        calls.append(child['id'])
        child.update(status='done', content='verified prepared result', thinking='complete evidence',
            finishReason='stop', streamState='provider_finished')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.execute_invocation', execute)
    journal = ExecutionJournal(lambda event: events.append(deepcopy(event)))
    journal.begin(definition, 'original context')
    try:
        try:
            with prepare_flow_recipients(inputs, config=config, emit=lambda event: None,
                                         abort_check=lambda: False) as recipients:
                prepared_child = next(iter(recipients.children.values()))
                recipients.bind(journal)
                result = FlowExecutor(definition, execution_journal=journal,
                    **config.executor_options()).run(initial_context='original context')
                assert not lost_completion_ack and result['ok']
        except Crash:
            assert lost_completion_ack and calls == [prepared_child['id']]
        recovered, _ = read_journal(events, definition=definition, initial_context='original context',
            reconcile_agent=completion_resolver(1))
        first = next(iter(recovered.values()))
        assert first['invocation_id'] == prepared_child['id']
        assert first['result']['output'] == 'verified prepared result'
        if lost_completion_ack:
            replay = ExecutionJournal(lambda event: events.append(deepcopy(event)), records=recovered)
            replay.begin(definition, 'original context')
            assert FlowExecutor(definition, execution_journal=replay,
                **config.executor_options()).run(initial_context='original context')['ok']
            replay.verify_consumed()
        assert len(calls) == 2 and len(set(calls)) == 2
        assert len(committed) == 2
    finally:
        _discard(parent)
