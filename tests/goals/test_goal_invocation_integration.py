"""Goal leaf integration: mature requests, streams, tools and completion evidence.

Context acquisition, provider transport and tool bodies are synthetic. Durable
cases use an isolated real Sidecar; no network model or user workspace is touched.
"""
from __future__ import annotations

import copy
import threading
from types import SimpleNamespace

import pytest

from lib.identity import PrincipalContext
from lib.orchestration.agent_runner import (
    OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner,
)
from lib.orchestration.chat_event_sink import OrchestrationChatTaskEventSink
from lib.orchestration.chat_flow_adapter import FlowEventAdapter

from tests.storage.test_storage_sidecar_contract import storage  # isolated Sidecar fixture
pytestmark = pytest.mark.unit

NARRATION = 'I will inspect the synthetic evidence before answering. '
REASONING = 'First compare the inputs. '
FINAL_REASONING = 'The hermetic result supports the conclusion. '
ANSWER = 'Verified synthetic conclusion. ' * 24
TOOL_RESULT = 'Synthetic evidence row: alpha=17.\n' * 24
TOOL = {'type': 'function', 'function': {
    'name': 'fixture_goal_probe', 'description': 'Read synthetic evidence only',
    'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
}}


@pytest.fixture
def runtime(monkeypatch):
    import lib.tasks_pkg.orchestrator._run as runner
    import lib.tasks_pkg.orchestrator._root_agent_loop as loop
    import lib.tasks_pkg.orchestrator._ports as ports
    import lib.tasks_pkg.manager._stream as stream
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    from lib.tasks_pkg.manager import create_task, discard_task

    calls, children, snapshots, tool_calls = [], [], [], []
    stop = threading.Event()
    principal = PrincipalContext.user(subject_id='goal-integration', owner_user_id=771)
    parent = create_task('', [{'role': 'user', 'content': 'Verify synthetic evidence'}],
                         {'model': 'fixture-model', 'preset': '', 'memoryEnabled': False,
                          'searchMode': 'off', 'thinkingEnabled': True},
                         principal=principal, transient=True, supersede=False)

    def handler(task, tc, name, call_id, args, rn, row, *rest):
        tool_calls.append((task['id'], copy.deepcopy(args)))
        _finalize_tool_round(task, rn, row, [{'title': 'Synthetic evidence', 'output': TOOL_RESULT}])
        return call_id, TOOL_RESULT, False

    parent['_tool_env'] = SimpleNamespace(resolve=lambda name: handler if name == TOOL['function']['name'] else None)
    for name in ('start_prefetches', 'stop_prefetches', 'maybe_run_memory_prefetch',
                 'inject_context_and_emit_chips', 'setup_project_context'):
        monkeypatch.setattr(runner, name, lambda *a, **kw: None)
    monkeypatch.setattr(runner, 'run_turn_prelude', lambda task, cfg, tid: cfg)
    monkeypatch.setattr(runner, 'check_autopilot_kick', lambda task: False)
    monkeypatch.setattr(loop, 'run_round_message_hygiene', lambda *a, **kw: None)
    monkeypatch.setattr(loop, 'drain_and_inject_inbox', lambda **kw: None)
    monkeypatch.setattr(ports, 'build_request_body', lambda model, messages, **kw: {
        'model': model, 'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(kw['tools'])})

    import lib.tasks_pkg.child_invocation as child_owner
    original_create = child_owner.create_child_invocation
    def create(*args, **kwargs):
        sink = kwargs['event_sink']
        def evidence(event):
            snapshots.append(copy.deepcopy(event))
            sink(event)
        kwargs['event_sink'] = evidence
        child = original_create(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(child_owner, 'create_child_invocation', create)

    def provider(body, **kwargs):
        index = len(calls)
        assert index < 2, 'unexpected extra provider dispatch'
        calls.append(copy.deepcopy(body))
        assert kwargs['owner_user_id'] == 771
        text = NARRATION if index == 0 else ANSWER
        thought = REASONING if index == 0 else FINAL_REASONING
        kwargs['on_thinking'](thought)
        kwargs['on_content'](text)
        message = {'role': 'assistant', 'content': text, 'reasoning_content': thought}
        if index == 0:
            message['tool_calls'] = [{'id': 'goal-fixture-call', 'type': 'function',
                'function': {'name': TOOL['function']['name'], 'arguments': '{}'}}]
        return ProviderStreamResult.from_legacy(
            message, 'tool_calls' if index == 0 else 'stop',
            {'prompt_tokens': 40, 'completion_tokens': 30, 'total_tokens': 70})
    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    try:
        yield SimpleNamespace(parent=parent, children=children, calls=calls,
                              snapshots=snapshots, tool_calls=tool_calls, stop=stop)
    finally:
        for task in [parent, *children]:
            discard_task(task['id'])


def _run_goal(runtime):
    forwarded = []
    sink = OrchestrationChatTaskEventSink(runtime.parent,
        lambda task, event: forwarded.append((copy.deepcopy(event), copy.deepcopy({
            key: task.get(key) for key in ('content', 'thinking', 'segments', 'toolRounds')}))))
    adapter = FlowEventAdapter(on_stream=sink, projection='autopilot')
    node = {'id': 'goal-worker', 'type': 'role', 'role': 'worker',
            'params': {'objective': 'Verify synthetic evidence', 'emits': 'assistant'}}
    adapter.on_event({'type': 'step_start', 'role': 'worker', 'emits': 'assistant'})
    result = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(parent_task=runtime.parent, model='fixture-model',
            model_routing_policy='selected', all_tools=[TOOL]),
        emit=adapter.on_event, abort_check=runtime.stop.is_set)(node, 'Synthetic context', 1)
    before_seal = copy.deepcopy({key: runtime.parent.get(key)
                               for key in ('content', 'thinking', 'segments', 'toolRounds')})
    adapter.on_event({'type': 'step_complete', 'role': 'worker', 'emits': 'assistant',
                     'status': result.status, 'outputProjection': result.output_projection})
    return result, before_seal, adapter, forwarded


def test_goal_actual_two_round_runtime_preserves_tool_request_and_owner(runtime):
    result, live, adapter, forwarded = _run_goal(runtime)
    assert result.status == 'completed', result.error
    assert len(runtime.calls) == 2
    child = runtime.children[0]
    assert runtime.tool_calls == [(child['id'], {})]
    assert not child['_turnId'] and not child['_attemptId']
    assert runtime.calls[0]['model'] == runtime.calls[1]['model'] == 'fixture-model'
    second = runtime.calls[1]['messages']
    assert any(m['role'] == 'tool' and m['content'] == TOOL_RESULT for m in second)
    requests = [e for e in runtime.snapshots if e['type'] == 'messages_snapshot' and e.get('kind') == 'request']
    assert [e['roundNum'] for e in requests] == [1, 2]
    owner = f"{runtime.parent['id']}#agent:{child['id']}"
    assert all(e['inspectorRef']['taskId'] == owner for e in requests)
    rows = result.output_projection['toolRounds']
    assert len(rows) == 1
    assert rows[0]['status'] == 'done'
    assert rows[0]['toolContent'] == TOOL_RESULT
    assert rows[0]['inspectorRef'] == requests[0]['inspectorRef']
    assert any(e['type'] == 'tool_result' and state['toolRounds'][0]['status'] == 'done'
               for e, state in forwarded)
    assert runtime.parent['status'] != 'done'


def test_goal_live_completion_seal_retains_full_ordered_evidence_and_ids(runtime):
    result, live, adapter, forwarded = _run_goal(runtime)
    assert result.status == 'completed', result.error
    projection = result.output_projection
    blocks = projection['segments']
    text = ''.join(b.get('text', '') for b in blocks if b['type'] == 'text')
    thinking = ''.join(b.get('text', '') for b in blocks if b['type'] == 'thinking')
    assert NARRATION.rstrip() in text and ANSWER in text
    assert REASONING.rstrip() in thinking and FINAL_REASONING.rstrip() in thinking
    ids = [b['blockId'] for b in blocks]
    assert len(ids) == len(set(ids))
    assert [b['blockId'] for b in live['segments']] == ids
    assert adapter.messages[-1]['segments'] == blocks
    # _round is a private runtime backreference; segments/toolRounds are the
    # public carriers. Compare every public field, not object-internal links.
    public_blocks = [{k: v for k, v in b.items() if not k.startswith('_')}
                     for b in blocks]
    assert runtime.parent['segments'] == public_blocks
    assert runtime.parent['toolRounds'] == projection['toolRounds']


def test_root_and_goal_share_preparation_and_second_request_tool_state(runtime, monkeypatch):
    from lib.tasks_pkg.invocation import execute_invocation, ROOT_INVOCATION_POLICY
    import lib.tasks_pkg.orchestrator._run as runner

    original_assembly = runner.assemble_round_tools
    def assemble(cfg, task, mcfg):
        if task is runtime.parent:
            task['_executable_tool_catalog'] = [TOOL]
            return [TOOL], True
        return original_assembly(cfg, task, mcfg)
    monkeypatch.setattr(runner, 'assemble_round_tools', assemble)
    original_messages = copy.deepcopy(runtime.parent['messages'])
    # Ordinary interactive chat enables per-round Inspector state; headless
    # roots deliberately omit that optional mirror unless explicitly enabled.
    runtime.parent['_attended'] = True
    execute_invocation(runtime.parent, policy=ROOT_INVOCATION_POLICY)
    assert runtime.parent['finishReason'] == 'stop', runtime.parent.get('error')
    root_requests = copy.deepcopy(runtime.calls)
    assert len(root_requests) == 2
    root_ref = copy.deepcopy(runtime.parent['toolRounds'][0]['inspectorRef'])
    assert root_ref['taskId'] == runtime.parent['id']
    assert root_ref['roundNum'] == 1 and root_ref['turn']

    root_events = runtime.parent['events']
    for kind in ('tool_start', 'tool_complete'):
        event = next(event for event in root_events if event['type'] == kind)
        assert event['inspectorRef'] == root_ref
        assert event['taskId'] == runtime.parent['id']
    root_state = next(event for event in root_events
                      if event['type'] == 'messages_snapshot'
                      and event.get('kind') == 'state'
                      and event['roundNum'] == root_ref['roundNum'])
    assert root_state['inspectorRef'] == root_ref
    assert any(message['role'] == 'tool' and message['content'] == TOOL_RESULT
               for message in root_state['messages'])
    runtime.calls.clear()
    runtime.tool_calls.clear()
    runtime.parent['status'] = 'running'
    result, _, _, _ = _run_goal(runtime)
    assert result.status == 'completed', result.error
    child_ref = result.output_projection['toolRounds'][0]['inspectorRef']
    assert child_ref['roundNum'] == root_ref['roundNum']
    assert child_ref['taskId'] != root_ref['taskId']
    assert child_ref['turn'] != root_ref['turn']
    for root_body, child_body in zip(root_requests, runtime.calls, strict=True):
        assert root_body['model'] == child_body['model']
        assert root_body['tools'] == child_body['tools']
        assert [m for m in root_body['messages'] if m['role'] == 'tool'] == [
            m for m in child_body['messages'] if m['role'] == 'tool']
    assert root_requests[0]['messages'] == original_messages
    child_first = runtime.calls[0]['messages']
    assert any('Synthetic context' in m.get('content', '') for m in child_first)
    assert any(m.get('_injection') or 'delegated objective' in m.get('content', '')
               for m in child_first)


@pytest.mark.parametrize('lost_ack', [False, True])
def test_goal_shared_loop_durable_accounting_and_ack_replay(runtime, storage, monkeypatch, lost_ack):  # noqa: F811
    """Real request/tool loop → registered child → exact durable phase and usage."""
    from uuid import uuid4
    import lib.storage.service as storage_service
    import lib.agent_core.store as stores
    from lib import turn_lifecycle as lifecycle
    from lib.storage import StorageEventBatcher, StorageError
    from lib.tasks_pkg import event_log, persistence_store
    from lib.tasks_pkg.manager import append_event, create_task, discard_task
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.tasks_pkg.compaction._archive import _archive_transcript
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    from lib.tasks_pkg.compaction._compaction_usage import (
        get_compaction_usage, record_compaction_usage, reset_compaction_usage,
    )
    from lib.tasks_pkg.compaction._settlement import settle_compaction_usage
    from lib.tasks_pkg.request_inspector import get_request_payload, invalidate_task_cache
    from lib.tasks_pkg.snapshot_delta import forget_projector_task
    from lib.orchestration.chat_turn_persistence import OrchestrationChatTurnPersistence
    from lib.orchestration.graph import FlowEventDeliveryError

    client = storage.client
    # Keep the exported accessor stable: lazy imports must not retain a closure
    # for this Sidecar after teardown. Patch its runtime lookup instead.
    isolated_runtime = SimpleNamespace(client=lambda **kw: client)
    monkeypatch.setattr(storage_service, 'storage_runtime', lambda: isolated_runtime)
    monkeypatch.setattr(persistence_store, '_storage_client', lambda **kw: client)
    store = persistence_store.DefaultConversationStore()
    monkeypatch.setattr(stores, 'get_conversation_store', lambda: store)
    batcher = StorageEventBatcher(client_provider=lambda **kw: client,
        on_commit=event_log._invalidate_event_read_caches)
    monkeypatch.setattr(event_log, '_SIDECAR_BATCHER', batcher)
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MIN_INTERVAL_S', '0')
    conv = 'durable-goal-' + uuid4().hex
    child = None
    try:
        created = lifecycle.create_turn_pair(
            conv, command_id=conv, user_id=771,
            input_projection={'content': 'Verify synthetic evidence'}, config={},
            conversation_defaults={'allowCreate': True, 'title': 'Durable Goal',
                                   'createdAt': 1, 'settings': {}})
        old_parent = runtime.parent
        parent = create_task(conv, copy.deepcopy(old_parent['messages']),
            {**old_parent['config'], '_turnId': created['turn']['turnId'],
             '_attemptId': created['attempt']['attemptId']},
            principal=PrincipalContext.from_payload(old_parent['_principalContext']),
            supersede=False)
        parent['_tool_env'] = old_parent['_tool_env']
        discard_task(old_parent['id'])
        runtime.parent = parent
        client.command('turn.attempt.bind', {'attempt_id': parent['_attemptId'],
            'task_id': parent['id'], 'user_id': 771}, conv + ':bind')
        faulted = []
        def sync(owner, turns):
            if lost_ack and len(turns) == 2 and not faulted:
                # The real transaction commits, but its ACK never updates the
                # caller's phase cursor. No provider/tool work is replayed.
                lifecycle.sync_visible_run_turns(dict(owner), turns)
                faulted.append(True)
                raise RuntimeError('synthetic lost commit ACK')
            return lifecycle.sync_visible_run_turns(owner, turns)
        persistence = OrchestrationChatTurnPersistence(parent,
            store_turns=lambda owner, turns: owner.update(_flow_turns=turns),
            sync_turns=sync)
        adapter = FlowEventAdapter(emit=persistence,
            on_stream=OrchestrationChatTaskEventSink(parent, append_event),
            projection='autopilot')
        persistence.bind(adapter.messages)
        adapter.on_event({'type': 'step_start', 'role': 'planner'})
        adapter.on_event({'type': 'step_delta', 'role': 'planner', 'kind': 'content', 'chunk': 'Plan'})
        adapter.on_event({'type': 'step_complete', 'role': 'planner', 'output': 'Plan', 'status': 'completed'})
        adapter.on_event({'type': 'step_start', 'role': 'worker'})
        phase_turn = parent['_flowLiveTurn']['turnId']
        record_compaction_usage(conv, {'prompt_tokens': 19})
        parent_ledger = copy.deepcopy(get_compaction_usage(conv))
        parent_usage = copy.deepcopy(parent.get('usage'))
        archives = []
        tool_handler = parent['_tool_env'].resolve(TOOL['function']['name'])
        def tool(task, *args):
            key = compaction_owner_key(task)
            assert task['_archiveScope'] == key
            identity = client.query('invocation.resolve_child', {'archive_scope': key, 'user_id': 771})
            assert identity['parent_task_id'] == parent['id']
            archives.append(_archive_transcript(conv, task['messages'], user_id=771,
                                               task=task, emit_event=False))
            record_compaction_usage(key, {'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14})
            return tool_handler(task, *args)
        parent['_tool_env'] = SimpleNamespace(resolve=lambda name: tool)
        result = OrchestrationInvocationRunner(OrchestrationAgentRunnerConfig(
            parent_task=parent, model='fixture-model', model_routing_policy='selected', all_tools=[TOOL]),
            emit=adapter.on_event, abort_check=runtime.stop.is_set)(
                {'id': 'worker', 'role': 'worker', 'params': {'emits': 'assistant'}}, 'Synthetic context', 1)
        assert result.status == 'completed', result.error
        child = runtime.children[-1]
        assert len(runtime.calls) == 2 and len(runtime.tool_calls) == 1
        assert child['usage']['prompt_tokens'] == 91
        assert child['usage']['completion_tokens'] == 63
        assert child['usage']['total_tokens'] == 154
        usage = copy.deepcopy(child['usage'])
        rounds = copy.deepcopy(child['apiRounds'])
        settle_compaction_usage(child)
        settle_compaction_usage(child)
        assert child['usage'] == usage and child['apiRounds'] == rounds
        assert len([r for r in rounds if r.get('kind') == 'compaction']) == 1
        assert get_compaction_usage(compaction_owner_key(child)) == {}
        assert get_compaction_usage(conv) == parent_ledger
        assert parent.get('usage') == parent_usage
        assert chat_task_runtime.get(child['id']) is None
        assert child['_invocationPublicationClosed']
        assert archives[0] and store.get_compaction_archive(child['_archiveScope'], archives[0], user_id=771)
        assert store.get_compaction_archive(conv, archives[0], user_id=771) is None
        with pytest.raises(StorageError):
            store.list_compaction_archives(child['_archiveScope'], user_id=772)

        completion = {'type': 'step_complete', 'role': 'worker', 'status': result.status,
                      'outputProjection': result.output_projection}
        if lost_ack:
            with pytest.raises(FlowEventDeliveryError, match='lost commit ACK'):
                adapter.on_event(completion)
            assert parent.get('_flowLiveTurn')
        else:
            adapter.on_event(completion)
        assert persistence.finalize()
        assert not parent.get('_flowLiveTurn')
        first = client.query('turn.sync.snapshot', {'conversation_id': conv, 'user_id': 771})
        assert persistence.finalize()
        assert client.query('turn.sync.snapshot', {'conversation_id': conv, 'user_id': 771}) == first
        persisted = next(turn for turn in first['turns'] if turn['turnId'] == phase_turn)
        assert persisted['status'] == 'completed'
        projection = persisted['projection']
        assert projection['usage']['prompt_tokens'] == 91
        assert len(projection['toolRounds']) == 1
        assert len([b for b in projection['segments'] if b['type'] == 'tool_use']) == 1
        assert [b['text'] for b in projection['segments'] if b['type'] == 'text'] == [NARRATION.rstrip(), ANSWER]
        ref = projection['toolRounds'][0]['inspectorRef']
        discard_task(parent['id'])
        invalidate_task_cache(ref['taskId'])
        state = get_request_payload(ref['taskId'], ref['roundNum'], ref['turn'], 'state')

        from routes.api_v1.tasks import _request_inspector_task_accessible
        assert _request_inspector_task_accessible(ref['taskId'], user_id=771)
        assert not _request_inspector_task_accessible(ref['taskId'], user_id=772)
        assert not _request_inspector_task_accessible(
            f'{parent["id"]}#agent:nonexistent', user_id=771)
        assert any(m['role'] == 'tool' and m['content'] == TOOL_RESULT for m in state['messages'])
        assert get_request_payload(ref['taskId'], ref['roundNum'], 'wrong-request', 'state') is None


        # A new root attempt revokes both raw live writes and idempotent phase
        # receipts from this old invocation, even after successful ACK replay.
        client.command('turn.event.record', {
            'attempt_id': parent['_attemptId'], 'user_id': 771,
            'terminal': True, 'status': 'interrupted',
            'settlement': {'outcome': 'interrupted', 'cause': 'user_abort',
                           'resumeOptions': []},
        }, conv + ':stop-root')
        root_turn = lifecycle.get_turn(conv, parent['_turnId'], user_id=771)
        successor = lifecycle.create_attempt(
            conv, parent['_turnId'], command_id=conv + ':regenerate',
            operation='regenerate', config=parent['config'], user_id=771,
            expected_projection_revision=root_turn['projectionRevision'])
        assert successor['attempt']['attemptId'] != parent['_attemptId']
        before_stale = client.query('turn.sync.snapshot', {
            'conversation_id': conv, 'user_id': 771})
        parent['content'] = 'must not overwrite the successor'
        assert not lifecycle.record_task_event(parent, {})
        with pytest.raises(FlowEventDeliveryError, match='Turn authority'):
            persistence.finalize()
        assert client.query('turn.sync.snapshot', {
            'conversation_id': conv, 'user_id': 771}) == before_stale
        assert len(runtime.calls) == 2 and len(runtime.tool_calls) == 1
    finally:
        assert batcher.close(timeout=3)
        reset_compaction_usage(conv)
        if child is not None:
            reset_compaction_usage(compaction_owner_key(child))
            forget_projector_task(child['_inspectorTaskId'])
            invalidate_task_cache(child['_inspectorTaskId'])


def test_goal_parent_cancelled_before_prep_has_no_dispatch(runtime):
    runtime.stop.set()
    result, live, adapter, forwarded = _run_goal(runtime)
    assert result.status == 'cancelled'
    assert runtime.calls == [] and runtime.tool_calls == []
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    assert chat_task_runtime.get(runtime.children[0]['id']) is None


def test_goal_tool_cancellation_persistence_failure_and_late_publication(runtime, monkeypatch):
    """Parent stop reaches a real tool; failed evidence cannot escape as success."""
    import lib.tasks_pkg.event_log as event_log
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    captured, persistence_attempts = [], []

    def fail_persistence(owner, seq, event):
        persistence_attempts.append((owner, event['type']))
        raise RuntimeError('synthetic evidence persistence failure')

    monkeypatch.setattr(event_log, 'append_persistent_event', fail_persistence)

    def blocked_tool(task, tc, name, call_id, args, rn, row, *rest):
        captured.append((task, task['_invocationEventSink']))
        runtime.stop.set()
        # No mocked executor: the child monitor must signal the ordinary tool
        # carrier while the mature invocation is blocked inside this handler.
        assert task['abort_event'].wait(2)
        assert task['aborted']
        task['_invocationStorageFree'] = False
        with pytest.raises(RuntimeError, match='synthetic evidence persistence failure'):
            from lib.tasks_pkg.round_evidence import build_round_evidence
            append_event(task, build_round_evidence(
                task, [], ref=task['_inspectorRef'], kind='request',
                model='fixture-model', prepared_messages=[]))
        return call_id, 'cancelled synthetic tool', False

    runtime.parent['_tool_env'] = SimpleNamespace(resolve=lambda name: blocked_tool)
    with pytest.raises(RuntimeError, match='evidence delivery failed'):
        _run_goal(runtime)
    assert len(runtime.calls) == 1  # Cancellation prevents another model request.
    child, late_sink = captured[0]
    assert persistence_attempts == [(child['_inspectorTaskId'], 'messages_snapshot')]
    assert child['_invocationPublicationClosed']
    assert chat_task_runtime.get(child['id']) is None
    assert not runtime.parent['abort_event'].is_set()  # No upward cancellation.
    before = copy.deepcopy({key: runtime.parent.get(key)
                            for key in ('content', 'thinking', 'segments', 'toolRounds', 'events')})
    snapshot_count = len(runtime.snapshots)
    with pytest.raises(RuntimeError, match='publication revoked'):
        late_sink({'type': 'delta', 'content': 'must never publish after return'})
    assert len(runtime.snapshots) == snapshot_count
    assert {key: runtime.parent.get(key) for key in before} == before
    assert not [thread for thread in threading.enumerate()
                if thread.name == f'child-abort-{child["id"][:8]}']


def test_shared_goal_budget_stops_before_first_provider_dispatch(runtime):
    runtime.parent['config']['executionLimits'] = {
        'max_tokens': 1, 'max_tool_calls': 2, 'deadline': 4_000_000_000}
    runtime.parent['usage'] = {'prompt_tokens': 1}
    result, *_ = _run_goal(runtime)
    assert result.status != 'completed'
    assert not runtime.calls
    assert runtime.children[0]['_executionLimitReason'] == 'execution_token_budget_exhausted'


def test_shared_goal_budget_prevents_second_model_round(runtime):
    runtime.parent['config']['executionLimits'] = {
        'max_tokens': 60, 'max_tool_calls': 2, 'deadline': 4_000_000_000}
    result, *_ = _run_goal(runtime)
    assert result.status != 'completed'
    assert len(runtime.calls) == 1
    assert runtime.children[0]['_executionLimitReason'] == 'execution_token_budget_exhausted'


def test_unlimited_goal_budget_dispatches_beyond_old_token_cap(runtime):
    runtime.parent['config']['executionLimits'] = {
        'max_tokens': None, 'max_tool_calls': None, 'deadline': None}
    runtime.parent['usage'] = {'prompt_tokens': 211730}
    result, *_ = _run_goal(runtime)
    assert result.status == 'completed', result.error
    assert len(runtime.calls) == 2
    child = runtime.children[0]
    assert runtime.tool_calls == [(child['id'], {})]
    assert not child.get('_executionLimitReason')
    assert '_invocationDeadline' not in child
    assert runtime.parent['_executionBudget'].consumption() == (211870, 1)
