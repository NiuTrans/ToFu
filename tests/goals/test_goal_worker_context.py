"""Goal workers retain normal-chat messages; verifier context stays independent."""
from __future__ import annotations

import copy

import pytest

from lib.goal_runs.worker_context import (
    GOAL_OBJECTIVE_INLINE_MAX_CHARS,
    GOAL_OBJECTIVE_ROLE_SECTION_HEADER,
    GoalWorkerContext,
)
from lib.orchestration._builtin_definitions import build_autopilot_definition
from lib.orchestration.engine import FlowExecutor
from tests.goals.test_goal_invocation_integration import runtime as _runtime_fixture, TOOL, TOOL_RESULT

runtime = _runtime_fixture

pytestmark = pytest.mark.unit


def _engine(runtime, *, max_iterations=3, events=None,
            loop_budget_policy='checkpoint_extend'):
    return FlowExecutor(
        build_autopilot_definition(max_iterations=max_iterations),
        parent_task=runtime.parent, all_tools=[TOOL], model='fixture-model',
        model_routing_policy='selected', worker_context_policy='conversation',
        max_iterations=max_iterations, abort_check=runtime.stop.is_set,
        loop_budget_policy=loop_budget_policy,
        on_event=(events.append if events is not None else None))


def test_journal_replay_restores_real_worker_tool_history_before_the_next_invocation(runtime, monkeypatch):
    from lib.orchestration.execution_journal import ExecutionJournal, read_journal
    from lib.llm.stream_result import ProviderStreamResult
    import lib.tasks_pkg.manager._stream as stream
    class Crash(BaseException):
        pass
    definition = {
        'schema': 'tofu.orchestration/v1', 'name': 'Context replay',
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'a', 'type': 'role', 'role': 'worker',
             'params': {'objective': 'Inspect', 'isolation': 'shared-context'}},
            {'id': 'b', 'type': 'role', 'role': 'worker',
             'params': {'objective': 'Finish', 'isolation': 'shared-context'}},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [{'from': 'start', 'to': 'a'}, {'from': 'a', 'to': 'b'},
                  {'from': 'b', 'to': 'stop'}],
    }
    runtime.parent['_todos'] = [{'id': 'verify', 'content': 'Verify evidence', 'status': 'pending'}]
    events = []
    journal = ExecutionJournal(lambda event: events.append(copy.deepcopy(event)),
                               worker_context_policy='conversation')
    journal.begin(definition, 'seed')
    def crash_after_commit(event):
        if event['type'] == 'step_complete' and event.get('node_id') == 'a':
            raise Crash()
    options = dict(parent_task=runtime.parent, all_tools=[TOOL], model='fixture-model',
                   model_routing_policy='selected', worker_context_policy='conversation')
    with pytest.raises(Crash):
        FlowExecutor(definition, on_event=crash_after_commit,
                     execution_journal=journal, **options).run(initial_context='seed')
    records, parent = read_journal(events, definition=definition, initial_context='seed')
    assert not parent and len(records) == 1
    context = next(iter(records.values()))['result']['worker_context']
    assert any(message.get('content') == TOOL_RESULT for message in context['messages'])
    assert context['task_state']['_todos'] == runtime.parent['_todos']
    requests = []
    def finish(body, **kwargs):
        requests.append(copy.deepcopy(body))
        assert any(message.get('content') == TOOL_RESULT for message in body['messages'])
        kwargs['on_content']('Finished using the preserved tool evidence')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Finished using the preserved tool evidence'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10})
    monkeypatch.setattr(stream, 'dispatch_stream', finish)
    replay = ExecutionJournal(lambda event: None, records=records,
                              worker_context_policy='conversation')
    replay.begin(definition, 'seed')
    result = FlowExecutor(definition, execution_journal=replay, **options).run(initial_context='seed')
    assert result['ok']
    replay.verify_consumed()
    assert len(runtime.calls) == 2  # Only the first worker's original tool round.
    assert len(runtime.tool_calls) == 1
    assert len(requests) == 1
    assert runtime.children[-1]['_todos'] == runtime.parent['_todos']


def test_actual_goal_worker_preserves_all_prior_rounds_and_media(runtime, monkeypatch):
    import lib.tasks_pkg.manager._stream as stream
    from lib.llm.stream_result import ProviderStreamResult
    from lib.goal_runs.objective import objective_from_task
    import base64
    from io import BytesIO
    from PIL import Image
    png = BytesIO()
    Image.new('RGB', (32, 32), color='blue').save(png, format='PNG')
    image = {'type': 'image_url', 'image_url': {
        'url': 'data:image/png;base64,' + base64.b64encode(png.getvalue()).decode()}}
    seed = [
        {'role': 'system', 'content': 'Explicit human-configured policy'},
        {'role': 'developer', 'content': 'Native developer policy'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'Verify synthetic evidence'}, image]},
        {'role': 'assistant', 'content': 'Earlier finding retained in full ' + 'x' * 55_000},
        {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': 'prior', 'type': 'function',
            'function': {'name': TOOL['function']['name'], 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'prior', 'content': 'PRIOR TOOL EVIDENCE'},
        {'role': 'user', 'content': 'Finish verification without changing files'},
    ]
    runtime.parent['messages'] = copy.deepcopy(seed)
    requests = []
    worker_iteration = 0
    worker_round = 0
    review_iteration = 0
    review_round = 0
    original_resolve = runtime.parent['_tool_env'].resolve

    def resolve(name):
        if name != TOOL['function']['name']:
            return original_resolve(name)
        def handler(task, tc, name, call_id, args, rn, row, *rest):
            if task.get('_flow_phase') != 'virtual_user':
                return original_resolve(name)(task, tc, name, call_id, args, rn, row, *rest)
            from lib.tasks_pkg.executor._finalize import _finalize_tool_round
            text = 'PRIVATE VERIFIER TOOL RESULT'
            _finalize_tool_round(task, rn, row, [{'output': text}])
            return call_id, text, False
        return handler
    runtime.parent['_tool_env'].resolve = resolve

    def provider(body, **kwargs):
        nonlocal worker_iteration, worker_round, review_iteration, review_round
        requests.append(copy.deepcopy(body))
        is_verifier = any('Stand in for the human' in str(m.get('content', ''))
                          for m in body['messages'])
        if is_verifier and review_round == 0:
            review_iteration += 1
            review_round = 1
            text, finish = 'Checking independently.', 'tool_calls'
            msg = {'role': 'assistant', 'content': text, 'tool_calls': [{
                'id': f'reviewer-{review_iteration}', 'type': 'function',
                'function': {'name': TOOL['function']['name'], 'arguments': '{}'}}]}
        elif is_verifier:
            review_round = 0
            text = f'Review {review_iteration}: inspect the next acceptance criterion. '
            text += '[VERDICT: CONTINUE_WORKER]\n[PROGRESS: resolved=0 remaining=1]'
            finish = 'stop'
            msg = {'role': 'assistant', 'content': text,
                   'reasoning_content': 'PRIVATE VERIFIER REASONING'}
        elif worker_round == 0:
            worker_iteration += 1
            worker_round = 1
            text, finish = f'Work {worker_iteration} investigation.', 'tool_calls'
            msg = {'role': 'assistant', 'content': text, 'tool_calls': [{
                'id': f'worker-{worker_iteration}', 'type': 'function',
                'function': {'name': TOOL['function']['name'], 'arguments': '{}'}}]}
        else:
            worker_round = 0
            text, finish = f'Worker result {worker_iteration}.', 'stop'
            msg = {'role': 'assistant', 'content': text}
        kwargs['on_content'](text)
        return ProviderStreamResult.from_legacy(msg, finish, {'total_tokens': 5})

    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    engine = _engine(runtime)
    result = engine.run(initial_context='BOUNDED VERIFIER SEED')
    assert result['agents_run'] == 6
    assert worker_iteration == review_iteration == 3
    worker_requests = [requests[i] for i in (0, 4, 8)]
    def public(messages):
        return [{k: v for k, v in m.items() if not k.startswith('_')} for m in messages]
    assert public(worker_requests[0]['messages']) == seed
    for iteration, request in enumerate(worker_requests):
        messages = request['messages']
        text = str(messages)
        assert public(messages[:len(seed)]) == seed
        assert text.count('PRIOR TOOL EVIDENCE') == 1
        assert 'PRIVATE VERIFIER REASONING' not in text
        assert 'PRIVATE VERIFIER TOOL RESULT' not in text
        assert 'BOUNDED VERIFIER SEED' not in text
        assert 'delegated objective' not in text
        assert 'FIRST tool call MUST be state-changing' not in text
        assert sum(m.get('content') == TOOL_RESULT for m in messages) == iteration
        for earlier in range(1, iteration + 1):
            assert text.count(f'Worker result {earlier}.') == 1
            assert text.count(f'Review {earlier}:') == 1
        assert '[PROGRESS:' not in text
        assert '[VERDICT:' not in text
        assert objective_from_task({'messages': messages}) == seed[-1]['content']
    for request in (requests[2], requests[6], requests[10]):
        text = str(request['messages'])
        assert 'BOUNDED VERIFIER SEED' in text
        assert 'PRIOR TOOL EVIDENCE' not in text
        assert TOOL_RESULT not in text
    assert runtime.parent['messages'] == seed  # no parent lifecycle/input mutation
    assert engine._default_runner_adapter._worker_context.messages is None


def test_actual_worker_adopts_real_l1_compaction_across_iterations(runtime, monkeypatch):
    import lib.tasks_pkg.manager._stream as stream
    import lib.tasks_pkg.orchestrator._root_agent_loop as loop
    from lib.tasks_pkg.orchestrator._round_message_hygiene import run_round_message_hygiene
    from lib.tasks_pkg.compaction.api import micro_compact
    from lib.llm.stream_result import ProviderStreamResult
    from tests.compaction.test_compaction_config_threading import _mk_conv
    seed = _mk_conv(n_tool=4, tool_chars=3000)
    seed.append({'role': 'user', 'content': 'Finish verification'})
    config = {'steps': ['compact_tool_results'], 'disableForceCompact': True,
              'constant_overrides': {'MICRO_HOT_TAIL': 2}}
    runtime.parent['config']['compaction'] = config
    runtime.parent['messages'] = copy.deepcopy(seed)
    expected = copy.deepcopy(seed)
    micro_compact(expected, conv_id='', steps=config['steps'],
                  constant_overrides=config['constant_overrides'])
    assert expected != seed
    requests = []
    def provider(body, **kwargs):
        requests.append(copy.deepcopy(body))
        text = ('Keep verifying. [VERDICT: CONTINUE_WORKER]\n'
                '[PROGRESS: resolved=0 remaining=1]'
                if len(requests) % 2 == 0 else 'Working conclusion')
        kwargs['on_content'](text)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': text}, 'stop', {'total_tokens': 5})
    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    monkeypatch.setattr(loop, 'run_round_message_hygiene', run_round_message_hygiene)
    result = _engine(runtime, max_iterations=2).run(initial_context='Independent review seed')
    assert result['agents_run'] == 4
    assert len(requests) == 4
    expected_tools = [m['content'] for m in expected if m['role'] == 'tool']
    for request in (requests[0], requests[2]):
        assert [m['content'] for m in request['messages'] if m['role'] == 'tool'] == expected_tools
    assert runtime.parent['messages'] == seed


def test_working_set_adopts_compaction_without_resurrecting_seed_or_losing_plan():
    context = GoalWorkerContext()
    seed = [{'role': 'user', 'content': 'original'},
            {'role': 'assistant', 'content': 'OLD RAW HISTORY'}]
    parent = {'messages': seed, '_todoState': {'version': 3, 'todos': [],
                                             'explanation': '', 'update_count': 0}}
    assert context.prepare(parent) == seed
    compacted = [{'role': 'user', 'content': 'original'},
                 {'role': 'assistant', 'content': 'COMPACTION RECEIPT'}]
    plan = {'version': 3, 'todos': [{'content': 'Verify', 'status': 'in_progress'}],
            'explanation': 'Continue the accepted plan', 'update_count': 1}
    child = {'messages': compacted, '_messages_include_current_run': True,
             '_todoState': plan, 'usage': {'total_tokens': 900},
             '_turnId': 'do-not-carry', 'toolRounds': [{'toolName': 'do-not-carry'}]}
    context.accept(child)
    context.review('Check evidence.\n[PROGRESS: resolved=1 remaining=1]')
    next_messages = context.prepare(parent, directive='Independent verification required')
    assert 'OLD RAW HISTORY' not in str(next_messages)
    assert 'COMPACTION RECEIPT' in str(next_messages)
    assert 'Independent verification required' in str(next_messages)
    fresh_child = {}
    context.hydrate(fresh_child)
    assert fresh_child['_todoState'] == plan
    fresh_child['_todoState']['todos'].clear()
    assert context.task_state['_todoState'] == plan
    assert not {'usage', '_turnId', 'toolRounds'} & fresh_child.keys()
    context.clear()
    assert context.messages is None and context.task_state == {}


def test_next_worker_restores_accepted_checklist_when_compaction_omits_receipts(runtime, monkeypatch):
    import lib.tasks_pkg.manager._stream as stream
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tools.todo import apply_todo_write_to_task, render_todo_list
    accepted = [{'content': 'Keep the accepted human plan', 'status': 'in_progress'}]
    requests = []
    def provider(body, **kwargs):
        requests.append(copy.deepcopy(body))
        child = runtime.children[-1]
        if len(requests) == 1:
            apply_todo_write_to_task(child, {'todos': accepted})
            text = 'First worker conclusion'
        elif len(requests) == 2:
            text = 'Review continuation [VERDICT: CONTINUE_WORKER]\n[PROGRESS: resolved=0 remaining=1]'
            # The next iteration must adopt this post-compaction working set,
            # not reconstruct from the original launch or todo tool receipts.
            engine._default_runner_adapter._worker_context.messages[:] = [
                {'role': 'user', 'content': 'Summary omitted all checklist calls.'}]
        else:
            text = ('Second worker conclusion' if len(requests) == 3 else
                    'Review continuation [VERDICT: CONTINUE_WORKER]\n[PROGRESS: resolved=0 remaining=1]')
        kwargs['on_content'](text)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': text}, 'stop', {'total_tokens': 5})
    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    engine = _engine(runtime, max_iterations=2)
    engine.run(initial_context='Independent verifier')
    assert len(requests) == 4
    assert render_todo_list(accepted) in str(requests[2]['messages'])
    assert render_todo_list(accepted) not in str(requests[1]['messages'])
    assert '_todoState' not in runtime.parent
    assert runtime.children[2]['_todoState']['todos'] == accepted


def test_working_set_does_not_keep_one_snapshot_per_iteration():
    context = GoalWorkerContext()
    parent = {'messages': [{'role': 'user', 'content': 'original'}]}
    context.prepare(parent)
    sizes = []
    for iteration in range(64):
        compacted = [{'role': 'user', 'content': 'original'},
                     {'role': 'assistant', 'content': f'compacted-{iteration:02d}' + 'x' * 2000}]
        context.accept({'messages': compacted, '_messages_include_current_run': True})
        context.review('One bounded review.')
        sizes.append(sum(len(m['content']) for m in context.prepare(parent)))
        assert len(context.messages) == 3
    assert len(set(sizes)) == 1
    context.clear()
    assert context.messages is None


@pytest.mark.parametrize('failure', ['cancel', 'provider', 'delivery'])
def test_goal_working_set_released_on_every_graph_exit(runtime, monkeypatch, failure):
    import lib.tasks_pkg.manager._stream as stream
    from lib.llm.stream_result import ProviderStreamResult
    events = []
    engine = _engine(runtime, events=events)
    def provider(body, **kwargs):
        if failure == 'cancel':
            runtime.stop.set()
        elif failure == 'provider':
            raise ValueError('synthetic provider failure')
        else:
            runtime.children[-1]['_invocationDeliveryError'] = RuntimeError('synthetic delivery failure')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'partial'}, 'stop', {'total_tokens': 5})
    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    engine.run(initial_context='review seed')
    assert engine._default_runner_adapter._worker_context.messages is None
    assert engine._default_runner_adapter._worker_context.task_state == {}


def test_review_fails_closed_on_unmatched_tool_call():
    state = GoalWorkerContext()
    state.prepare({'messages': [{'role': 'user', 'content': 'original'}]})
    state.accept({'_messages_include_current_run': True, 'messages': [
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'pending', 'function': {'name': 'read_files', 'arguments': '{}'}}]}]})
    with pytest.raises(ValueError, match='unfinished tool batch'):
        state.review('Continue')
    with pytest.raises(ValueError, match='canonical model working set'):
        state.accept({'messages': []})


def test_hydrate_restates_the_authoritative_objective_verbatim():
    """Hole A closure: the run's durable objective rides its own registered
    composer block, verbatim and byte-stable across iterations."""
    context = GoalWorkerContext()
    context.prepare({'messages': [{'role': 'user', 'content': 'original'}]})
    objective = 'Migrate the renderer to the new contract AND keep every '
    objective += 'existing visual assertion green — no narrowed substitute.'
    child = {}
    context.hydrate(child, objective='Work the review.', goal_objective=objective)
    block = child['_contextRoleBlock']
    assert block['name'] == 'goal'
    assert block['content'] == 'Work the review.'
    objective_block = child['_goalObjectiveBlock']['content']
    assert GOAL_OBJECTIVE_ROLE_SECTION_HEADER in objective_block
    assert f'<objective>\n{objective}\n</objective>' in objective_block
    # Byte-stable across iterations within a run (prefix-cache friendly).
    second = {}
    context.hydrate(second, objective='Work the review.', goal_objective=objective)
    assert second['_goalObjectiveBlock'] == child['_goalObjectiveBlock']
    # No stamped objective → no block, and the historical brief-only role
    # block is unchanged.
    third = {}
    context.hydrate(third, objective='Work the review.')
    assert '_goalObjectiveBlock' not in third
    assert third['_contextRoleBlock']['content'] == 'Work the review.'
    # An objective beyond the inline bound stays history-carried: no block,
    # never a truncated (tag-corrupting) restatement.
    giant = 'x' * (GOAL_OBJECTIVE_INLINE_MAX_CHARS + 1)
    fourth = {}
    context.hydrate(fourth, objective='Work the review.', goal_objective=giant)
    assert '_goalObjectiveBlock' not in fourth


def test_worker_request_prefix_is_byte_stable_across_iterations(runtime, monkeypatch):
    """Cache parity with ordinary chat: iteration N+1's working set starts
    with iteration N's exact request messages — the working-set handoff never
    inserts or rewrites ahead of the tail.  (This fixture stubs the composer
    injection boundary, so it proves the handoff itself; the composer's own
    cache-epoch stability is covered by its owner suite.  The verbatim
    objective block is asserted in the hydrate unit test above.)"""
    import lib.tasks_pkg.manager._stream as stream
    from lib.llm.stream_result import ProviderStreamResult
    objective = 'Verify the whole pipeline end to end without shrinking scope.'
    runtime.parent['messages'] = [
        {'role': 'user', 'content': objective},
        {'role': 'assistant', 'content': 'Earlier reply.'},
        {'role': 'user', 'content': 'Now finish it properly.'},
    ]
    runtime.parent['_goalObjective'] = objective
    requests = []

    def provider(body, **kwargs):
        requests.append(copy.deepcopy(body))
        is_verifier = any('Stand in for the human' in str(m.get('content', ''))
                          for m in body['messages'])
        if is_verifier:
            text = ('Keep going. [VERDICT: CONTINUE_WORKER]\n'
                    '[PROGRESS: resolved=0 remaining=1]')
        else:
            text = f'Worker output {len(requests)}.'
        kwargs['on_content'](text)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': text}, 'stop',
            {'total_tokens': 5})

    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    engine = _engine(runtime, max_iterations=2)
    engine.run(initial_context='BOUNDED VERIFIER SEED')

    worker_requests = [
        request for request in requests
        if not any('Stand in for the human' in str(m.get('content', ''))
                   for m in request['messages'])
    ]
    assert len(worker_requests) == 2
    first, second = (request['messages'] for request in worker_requests)
    # Prefix-cache parity: the next iteration's request begins with the
    # previous iteration's EXACT request bytes — nothing inserted or
    # rewritten ahead of the tail, so provider-side prompt caching hits on
    # the whole prior conversation exactly like an ordinary chat turn.
    assert second[:len(first)] == first
    assert engine._default_runner_adapter._worker_context.messages is None


def test_completed_worker_offers_checkpoint_only_after_visible_commit(runtime, monkeypatch):
    from tests.orchestration.test_orchestration_execution_recovery import graph, role
    from lib.orchestration.chat_turn_sync import sync_flow_turns_to_conversation
    runtime.parent.update(convId='fixture-conversation', _attemptId='fixture-attempt', _turnId='fixture-turn')
    runtime.parent['_todos'] = [{'id': 'retained', 'content': 'Retained checklist', 'status': 'completed'}]
    node = role('worker')
    node['params']['isolation'] = 'shared-context'
    outcome = FlowExecutor(graph(node), parent_task=runtime.parent,
        all_tools=[TOOL], model='fixture-model', model_routing_policy='selected',
        worker_context_policy='conversation', abort_check=runtime.stop.is_set).run()
    assert outcome['ok']
    checkpoint = runtime.parent['_goalWorkerCheckpoint']
    assert any(row['content'] == 'Retained checklist' and row['status'] == 'completed'
               for row in checkpoint['task_state']['_todos'])
    assert any(m.get('role') == 'tool' and TOOL_RESULT in str(m.get('content'))
               for m in checkpoint['messages'])
    # The provider fixture uses an inline carrier; the production sync owner
    # requires the authoritative parent identity, not that fixture-only flag.
    runtime.parent.pop('_inline_messages', None)
    committed = []
    def commit(task, turns):
        committed.append(turns)
        return 1
    def publish(task):
        assert committed
        assert task.pop('_goalWorkerCheckpoint') is checkpoint
        return True
    monkeypatch.setattr('lib.turn_lifecycle.sync_visible_run_turns', commit)
    monkeypatch.setattr('lib.goal_runs.context_recovery.publish_worker_context', publish)
    assert sync_flow_turns_to_conversation(runtime.parent, [{'role': 'assistant', 'content': 'settled'}]) == 1
    assert '_goalWorkerCheckpoint' not in runtime.parent
