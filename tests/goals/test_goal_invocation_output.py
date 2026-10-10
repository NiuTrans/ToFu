"""One ordinary output protocol from a graph leaf through cold Turn replay."""
from __future__ import annotations

import copy

import pytest

from tests.storage.test_storage_sidecar_contract import storage  # shared isolated process fixture

pytestmark = pytest.mark.unit


def test_child_round_reset_and_inspector_survive_graph_settlement_reload(storage, monkeypatch):  # noqa: F811
    from lib import turn_lifecycle as lifecycle
    from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner
    from lib.orchestration.chat_event_sink import OrchestrationChatTaskEventSink
    from lib.orchestration.chat_flow_adapter import FlowEventAdapter
    from lib.orchestration.chat_turn_persistence import OrchestrationChatTurnPersistence
    from lib.tasks_pkg.child_invocation import run_child_invocation
    import lib.tasks_pkg.child_invocation as child_owner
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.invocation import invocation_output_projection
    from lib.tasks_pkg.orchestrator._finalize import _discard_pretool_prose
    from lib.tasks_pkg.round_evidence import begin_round_evidence, build_round_evidence

    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: storage.client)
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MIN_INTERVAL_S', '0')
    created = lifecycle.create_turn_pair(
        'goal-output', command_id='goal-output-create', user_id=1,
        input_projection={'content': 'Implement and verify'}, config={},
        conversation_defaults={'allowCreate': True, 'title': 'Goal fixture', 'createdAt': 1, 'settings': {}})
    task = {'id': 'goal-output-parent', 'convId': 'goal-output', '_userId': 1,
            '_turnId': created['turn']['turnId'], '_attemptId': created['attempt']['attemptId'],
            'config': {}, 'content': '', 'thinking': ''}
    storage.client.command('turn.attempt.bind', {
        'attempt_id': task['_attemptId'], 'user_id': 1, 'task_id': task['id'],
    }, 'goal-output-bind')
    forwarded = []
    def persist_event(owner, event):
        from lib.agent_core.events import check_event
        check_event(event)
        forwarded.append(copy.deepcopy(event))
        assert lifecycle.record_task_event(owner, event)
    sink = OrchestrationChatTaskEventSink(task, persist_event)
    persistence = OrchestrationChatTurnPersistence(
        task, store_turns=lambda owner, turns: owner.update(_flow_turns=turns),
        sync_turns=lifecycle.sync_visible_run_turns)
    adapter = FlowEventAdapter(emit=persistence, on_stream=sink, projection='autopilot')
    persistence.bind(adapter.messages)
    # Finish the opening phase so the delegated verifier has its own live Turn.
    adapter.on_event({'type': 'step_start', 'role': 'worker', 'node_id': 'worker'})
    adapter.on_event({'type': 'step_delta', 'role': 'worker', 'kind': 'content', 'chunk': 'Worker result'})
    adapter.on_event({'type': 'step_complete', 'role': 'worker', 'output': 'Worker result', 'status': 'completed'})
    adapter.on_event({'type': 'step_start', 'role': 'virtual_user', 'node_id': 'review', 'emits': 'user'})
    verifier_turn = task['_flowLiveTurn']['turnId']
    checkpoint = []
    refs = []
    tool_bodies = []

    def scripted_execution(child, *, policy):
        # Only provider/tool work is scripted. Real event sequencing, graph
        # projection, round reset, settlement and Sidecar replay remain owners.
        ref = begin_round_evidence(child, child['_inspectorTaskId'], 1, 'review')
        refs.append(ref)
        append_event(child, build_round_evidence(
            child, [], ref=ref, kind='request', model='fixture-model',
            prepared_messages=[{'role': 'user', 'content': 'Exact request'}]))
        child.update(content='Inspecting files', thinking='Check the evidence')
        append_event(child, {'type': 'delta', 'content': child['content'], 'thinking': child['thinking']})
        row = {'toolName': 'read_files', 'toolCallId': 'one-occurrence', 'roundNum': 1,
               'llmRound': 0, 'taskId': child['id'], 'query': 'Read fixture', 'toolArgs': '{}',
               'status': 'searching', 'assistantContent': child['content'],
               'thinking': child['thinking'], 'inspectorRef': ref}
        child['toolRounds'] = [row]
        append_event(child, {'type': 'tool_start', **{key: value for key, value in row.items()
                     if key not in ('assistantContent', 'thinking')}})
        _discard_pretool_prose(child, 0)
        assert task['content'] == task['thinking'] == ''
        assert [b['text'] for b in task['segments'] if b['type'] in ('text', 'thinking')] == [
            'Check the evidence', 'Inspecting files']
        row.update(status='done', toolContent='FULL exact result')
        append_event(child, {'type': 'tool_complete', 'roundNum': 1, 'toolCallId': 'one-occurrence',
                             'toolName': row['toolName'], 'toolContent': row['toolContent'],
                             'status': 'done', 'inspectorRef': ref})
        tool_bodies.append(row['toolContent'])
        append_event(child, build_round_evidence(
            child, [], ref=ref, kind='state', model='fixture-model',
            prepared_messages=[{'role': 'tool', 'tool_call_id': row['toolCallId'], 'content': row['toolContent']}]))
        append_event(child, {'type': 'round_end', 'roundNum': 0, 'reason': 'tools'})
        checkpoint.append(lifecycle.get_turn(task['convId'], verifier_turn, user_id=1)['projection'])
        child.update(content='Verified answer', thinking='', status='done', finishReason='stop',
                     streamState='provider_finished')
        append_event(child, {'type': 'delta', 'content': 'Verified answer'})
        from lib.tasks_pkg.segments import assemble_segments
        child['segments'] = assemble_segments(child)
        append_event(child, {'type': 'done', 'finishReason': 'stop', 'usage': {}})

    monkeypatch.setattr(child_owner, 'execute_invocation', scripted_execution)
    result = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=task, model='fixture-model', model_routing_policy='selected'),
        emit=adapter.on_event, abort_check=lambda: False)(
            {'id': 'review', 'role': 'virtual_user', 'params': {'emits': 'user'}}, '', 1)
    assert result.status == 'completed'
    adapter.on_event({'type': 'step_complete', 'node_id': 'review', 'role': 'virtual_user',
                     'emits': 'user', 'output': result.output, 'thinking': result.thinking,
                     'status': result.status, 'outputProjection': result.output_projection})
    settled = lifecycle.get_turn(task['convId'], verifier_turn, user_id=1)
    assert settled['status'] == 'completed'
    projection = settled['projection']

    assert len(projection['toolRounds']) == 1, projection['toolRounds']
    assert len([b for b in projection['segments'] if b['type'] == 'tool_use']) == 1
    assert projection['toolRounds'][0]['taskId'] == result.output_projection['toolRounds'][0]['taskId']
    assert not projection['toolRounds'][0].get('attemptId')
    assert [b['text'] for b in projection['segments'] if b['type'] == 'text'] == [
        'Inspecting files', 'Verified answer']
    assert [b['text'] for b in projection['segments'] if b['type'] == 'thinking'] == ['Check the evidence']
    assert projection['toolRounds'][0]['toolContent'] == tool_bodies[0]
    assert projection['toolRounds'][0]['inspectorRef'] == refs[0]
    assert [b['blockId'] for b in checkpoint[0]['segments']] == [
        b['blockId'] for b in projection['segments'][:len(checkpoint[0]['segments'])]]
    assert not any(e['type'] == 'state' for e in forwarded)  # no cumulative replay event store
    assert [e['type'] for e in forwarded].count('delta_reset') == 1
    from lib.tasks_pkg.request_inspector import get_request_payload
    request = get_request_payload(refs[0]['taskId'], 1, refs[0]['turn'], 'request')
    state = get_request_payload(refs[0]['taskId'], 1, refs[0]['turn'], 'state')
    assert request['messages'][0]['content'] == 'Exact request'
    assert state['messages'][0]['content'] == tool_bodies[0]
    reloaded = storage.client.query('turn.sync.snapshot', {'conversation_id': task['convId'], 'user_id': 1})
    cold = next(row for row in reloaded['turns'] if row['turnId'] == verifier_turn)
    assert cold['projection'] == projection
    from lib.conversation_sync.validation import decode
    for turn in reloaded['turns']:
        decode('TurnProjection', turn['projection'])
    assert run_child_invocation is child_owner.run_child_invocation
