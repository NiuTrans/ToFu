"""Swarm scheduling enters the real root/worker execution and tool boundaries."""
from copy import deepcopy
import pytest

from tests.goals import test_goal_invocation_integration as goal_fixture
from tests.goals.test_goal_invocation_integration import TOOL, ANSWER, TOOL_RESULT

runtime = goal_fixture.runtime
from lib.swarm.agent import SubAgent
from lib.swarm.protocol import SubTaskSpec

pytestmark = pytest.mark.unit


def test_swarm_uses_root_requests_tools_and_terminal_evidence(runtime):
    original = deepcopy(runtime.parent['messages'])
    events = []
    agent = SubAgent(SubTaskSpec(role='general', objective='Verify synthetic evidence'),
                     parent_task=runtime.parent, all_tools=[TOOL],
                     model='fixture-model', on_event=events.append)
    result = agent.run()
    assert result.status == 'completed', result.error_message
    assert ANSWER in result.final_answer
    assert len(runtime.calls) == 2
    assert len(runtime.tool_calls) == 1
    assert runtime.calls[0]['tools'] == [TOOL]
    assert any(message.get('role') == 'tool' and TOOL_RESULT in message.get('content', '')
               for message in runtime.calls[1]['messages'])
    assert runtime.parent['messages'] == original
    assert result.rounds_used == 2
    assert result.tool_calls_made == 1
    assert result.total_tokens == 140
    assert len(runtime.children) == 1
    assert not agent.messages and agent._child is None
    assert any(event.get('type') == 'swarm_agent_tool_call' for event in events)


def agent_for(runtime, **kwargs):
    spec = kwargs.pop('spec', SubTaskSpec(role='general', objective='Verify evidence'))
    return SubAgent(spec, parent_task=runtime.parent, all_tools=kwargs.pop('tools', [TOOL]),
                    model='fixture-model', abort_check=runtime.stop.is_set, **kwargs)


def reply(content='Verified answer', calls=None, finish=None, usage=None):
    from lib.llm.stream_result import ProviderStreamResult
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return ProviderStreamResult.from_legacy(message, finish or ('tool_calls' if calls else 'stop'),
        usage or {'prompt_tokens': 20, 'completion_tokens': 3, 'total_tokens': 23})


def tool_call(call_id='call-1', name=None, arguments='{}', **extra):
    return {'id': call_id, 'type': 'function', 'function': {
        'name': name or TOOL['function']['name'], 'arguments': arguments}, **extra}


def scripted_provider(monkeypatch, runtime, responses):
    import lib.tasks_pkg.manager._stream as stream
    dispatches = []
    def dispatch(body, **kwargs):
        index = len(dispatches)
        dispatches.append(kwargs)
        runtime.calls.append(deepcopy(body))
        assert index < len(responses), 'Unexpected extra model request'
        result = responses[index]
        if isinstance(result, BaseException):
            raise result
        if result.message.get('content'):
            kwargs['on_content'](result.message['content'])
        return result
    monkeypatch.setattr(stream, 'dispatch_stream', dispatch)
    return dispatches


def test_one_round_owner_retry_budget_and_lazy_log(runtime, monkeypatch, tmp_path):
    calls = scripted_provider(monkeypatch, runtime, [reply('verified final answer')])
    agent = agent_for(runtime)
    path = tmp_path / 'lazy' / 'worker.log'
    agent.output_file = str(path)
    assert not path.parent.exists()
    result = agent.run()
    assert (result.status, result.rounds_used, result.tool_calls_made) == ('completed', 1, 0)
    assert calls[0]['owner_user_id'] == 771
    assert calls[0]['max_429_attempts'] is None  # Same selected-route policy as root.
    assert path.read_text() == 'verified final answer'
    assert not agent._log_buffer


def test_stream_log_buffer_and_resume_share_finite_disk_budget(runtime, tmp_path):
    from lib.swarm.agent import SWARM_OUTPUT_LOG_BYTES, SWARM_OUTPUT_LOG_BUFFER_BYTES
    path = tmp_path / 'resume.log'
    path.write_bytes(b'x' * (SWARM_OUTPUT_LOG_BYTES - 100))
    agent = agent_for(runtime)
    agent.output_file = str(path)
    for _ in range(200):
        agent._write_stream_log('测')
        assert len(agent._log_buffer) <= SWARM_OUTPUT_LOG_BUFFER_BYTES
    agent._flush_stream_log()
    assert path.stat().st_size <= SWARM_OUTPUT_LOG_BYTES
    assert path.read_text().endswith('测')


def test_unavailable_resume_log_stops_repeated_io(runtime, monkeypatch, tmp_path):
    import lib.swarm.agent as owner
    path = tmp_path / 'resume.log'
    path.touch()
    agent = agent_for(runtime)
    agent.output_file = str(path)
    probes = []
    def unavailable(name):
        probes.append(name)
        raise OSError('synthetic unavailable log volume')
    monkeypatch.setattr(owner.os.path, 'getsize', unavailable)
    for _ in range(200):
        agent._write_stream_log('streaming evidence')
    assert probes == [str(path)]
    assert not agent._log_buffer
    assert not path.read_bytes()


@pytest.mark.parametrize('stop_mode', ['parent', 'deadline'])
def test_blocked_tool_uses_child_cancellation_and_keeps_parent_isolated(runtime, monkeypatch, stop_mode):
    from types import SimpleNamespace
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    import threading
    entered = threading.Event()
    observations = []
    def blocked(task, tc, name, call_id, args, rn, row, *rest):
        entered.set()
        if stop_mode == 'parent':
            runtime.stop.set()
        assert task['abort_event'].wait(3), 'cancellation must reach tools during execution'
        observations.append((task['_userId'], task['_unattended'], task['id']))
        _finalize_tool_round(task, rn, row, [], status='interrupted')
        return call_id, 'interrupted', False
    runtime.parent['_tool_env'] = SimpleNamespace(resolve=lambda name: blocked)
    scripted_provider(monkeypatch, runtime, [reply('Partial evidence', [tool_call()])])
    spec = SubTaskSpec(role='general', objective='Inspect', timeout_seconds=0.4 if stop_mode == 'deadline' else 0)
    result = agent_for(runtime, spec=spec).run()
    assert entered.is_set()
    assert result.status == ('failed' if stop_mode == 'deadline' else 'cancelled')
    assert 'Partial evidence' in result.final_answer
    assert result.total_tokens == 23
    assert len(runtime.calls) == 1
    assert observations == [(771, True, runtime.children[0]['id'])]
    assert not runtime.parent['abort_event'].is_set()
    assert not runtime.parent.get('aborted')
    assert not any(t.name.startswith('child-abort-') and runtime.children[0]['id'][:8] in t.name
                   for t in threading.enumerate())


@pytest.mark.parametrize('finish', ['aborted', 'exception', 'failure', 'malformed', 'premature'])
def test_provider_terminal_outcomes_never_fabricate_success(runtime, monkeypatch, finish):

    # Isolate stream verdicts from the now-shared whole-invocation retry policy.
    runtime.parent['config']['disableAutoTurnRetry'] = True
    from lib.llm.llm_errors import AbortedError
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    value = reply('Partial evidence', finish='aborted')
    if finish == 'exception':
        value = AbortedError('Stop')
    elif finish == 'failure':
        value = RuntimeError('HTTP 400: schema rejected')
    elif finish in {'malformed', 'premature'}:
        value = ProviderStreamResult(message={'role': 'assistant', 'content': 'Partial evidence',
            'tool_calls': [tool_call()]}, compatibility_finish_reason='stop', usage={},
            state=ProviderStreamState.MALFORMED_STREAM if finish == 'malformed' else ProviderStreamState.PREMATURE_CLOSE,
            malformed_frame_count=int(finish == 'malformed'))
    import lib.tasks_pkg.stream_handler._analyse as analyse
    import lib.tasks_pkg.stream_handler._budget as retry_budget
    monkeypatch.setattr(analyse, '_PREMATURE_RETRY_MAX_CLASSIC', 2)
    monkeypatch.setattr(retry_budget, '_interruptible_sleep', lambda *a: None)
    scripted_provider(monkeypatch, runtime, [value] * 4)
    result = agent_for(runtime).run()
    assert result.status == ('cancelled' if finish in {'aborted', 'exception'} else 'failed')
    assert not runtime.tool_calls
    assert len(runtime.calls) <= 4
    if finish == 'aborted':
        assert result.final_answer == 'Partial evidence'
        assert result.total_tokens == 23
        assert not result.error_message


def test_later_provider_failure_preserves_previous_evidence(runtime, monkeypatch):
    scripted_provider(monkeypatch, runtime, [reply('Partial findings', [tool_call()]),
        RuntimeError('HTTP 400: schema rejected')] * 1)
    result = agent_for(runtime).run()
    assert result.status == 'failed'
    assert 'Partial findings' in result.final_answer
    assert result.total_tokens >= 23
    assert len(runtime.tool_calls) == 1
    assert result.actuals_complete is False
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.agent_core.execution_budget import ExecutionBudget
    restored = create_child_invocation(runtime.parent, model='fixture-model', tools=[TOOL],
        objective='Resume paid but incomplete work', event_sink=lambda event: None,
        abort_check=lambda: False, checkpoint_result=result.to_dict())
    budget = ExecutionBudget({'id': 'bounded-resume', 'config': {'executionLimits': {
        'max_tokens': 1000, 'max_tool_calls': 20, 'deadline': None}}})
    budget.attach(restored)
    assert budget.reason() == 'execution_token_usage_unknown'
    runtime.children.append(restored)


def test_productive_rounds_have_no_private_numeric_limit(runtime, monkeypatch):
    tool = deepcopy(TOOL)
    tool['function']['parameters']['properties']['step'] = {'type': 'integer'}
    scripted_provider(monkeypatch, runtime,
        [reply(f'Step {i}', [tool_call(f'call-{i}', arguments='{' + f'"step":{i}' + '}')]) for i in range(12)] + [reply()])
    result = agent_for(runtime, tools=[tool]).run()
    assert result.status == 'completed', result.error_message
    assert (result.rounds_used, result.tool_calls_made) == (13, 12)
    assert all(body['tools'] == [tool] for body in runtime.calls)


@pytest.mark.parametrize('ids', [('', ''), ('same', 'same'), ('left', 'right')])
def test_parallel_calls_keep_distinct_occurrences_and_paired_results(runtime, monkeypatch, ids):
    scripted_provider(monkeypatch, runtime, [reply(calls=[tool_call(i) for i in ids]), reply()])
    result = agent_for(runtime).run()
    assert result.status == 'completed', result.error_message
    assert len(runtime.tool_calls) == 2
    messages = runtime.calls[-1]['messages']
    calls = next(m['tool_calls'] for m in messages if m.get('tool_calls'))
    results = [m for m in messages if m['role'] == 'tool']
    assert len({c['id'] for c in calls}) == 2 and all(c['id'] for c in calls)
    assert [m['tool_call_id'] for m in results] == [c['id'] for c in calls]
    assert all(m['content'] == TOOL_RESULT for m in results)
    assert len({row['tool_call_id'] for row in result.tool_log}) == 2


def test_unknown_tool_and_program_caller_cannot_bypass_child_catalog(runtime, monkeypatch):
    scripted_provider(monkeypatch, runtime, [reply(calls=[
        tool_call('bad', name='ungranted_tool'),
        tool_call('program', caller={'type': 'code_execution', 'tool_id': 'ungranted-program'}),
        tool_call('good')]), reply('The permitted evidence is complete. ' * 20), reply('The permitted evidence is complete. ' * 20)])
    result = agent_for(runtime).run()
    assert result.status == 'completed', result.error_message
    assert len(runtime.tool_calls) == 1
    assert runtime.tool_calls[0][0] == runtime.children[0]['id']


def test_terminal_typed_failures_use_common_breaker_and_failed_timeline(runtime, monkeypatch):
    from types import SimpleNamespace
    from lib.tools.result_envelope import typed_tool_error, tool_result_error
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    failure = typed_tool_error('permission_required', retryable=False, next_action='Report missing authority')
    def handler(task, tc, name, call_id, args, rn, row, *rest):
        _finalize_tool_round(task, rn, row, [], status='error')
        return call_id, failure.to_dict(), False
    runtime.parent['_tool_env'] = SimpleNamespace(resolve=lambda name: handler)
    scripted_provider(monkeypatch, runtime, [reply(calls=[tool_call(f'call-{i}')]) for i in range(8)])
    result = agent_for(runtime).run()
    assert result.status == 'failed'
    assert len(runtime.calls) <= 4
    assert result.tool_log and all(row['status'] == 'failed' for row in result.tool_log)
    assert tool_result_error(runtime.children[0]['toolRounds'][0]['toolContent'])


def test_resume_refreshes_policy_retains_native_tool_evidence_and_new_inspector(runtime, monkeypatch):
    from lib.tasks_pkg.context_composer.injections import append_injection
    checkpoint = [{'role': 'system', 'content': 'stale inherited prompt'}]
    append_injection(checkpoint, 'worker_policy', 'stale privileged authority')
    checkpoint += [{'role': 'user', 'content': 'Original objective'},
        {'role': 'assistant', 'content': '', 'tool_calls': [tool_call('committed')]},
        {'role': 'tool', 'tool_call_id': 'committed', 'content': 'DURABLE EVIDENCE'}]
    original = deepcopy(checkpoint)
    scripted_provider(monkeypatch, runtime, [reply()])
    agent = agent_for(runtime, system_prompt_base='Fresh inherited prompt')
    agent.restore_checkpoint(checkpoint, 5)
    result = agent.run()
    assert result.status == 'completed', result.error_message
    assert result.rounds_used == 6
    assert checkpoint == original
    messages = runtime.calls[0]['messages']
    assert not any('stale' in m.get('content', '') for m in messages)
    assert any(m.get('content') == 'DURABLE EVIDENCE' for m in messages)
    assert not runtime.tool_calls
    request = next(e for e in runtime.snapshots if e['type'] == 'messages_snapshot')
    assert request['inspectorRef']['taskId'].endswith(runtime.children[0]['id'])


def test_tool_live_snapshot_and_inspector_reference_agree(runtime):
    from lib.swarm.master import _bounded_timeline_call
    from lib.swarm.presentation_budget import _bounded_browser_tool_call
    events = []
    result = agent_for(runtime, on_event=events.append).run()
    row = result.tool_log[0]
    request = next(e for e in runtime.snapshots if e['type'] == 'messages_snapshot')
    assert row['inspectorRef'] == request['inspectorRef']
    cold = _bounded_browser_tool_call(_bounded_timeline_call(row, row['tool']))
    assert cold['inspectorRef'] == row['inspectorRef']
    calls = [e for e in events if e.get('callId') == row['tool_call_id']]
    assert calls and calls[-1]['callStatus'] == 'done'
    assert all(e['inspectorRef'] == row['inspectorRef'] for e in calls)
    state = next(e for e in runtime.snapshots if e['type'] == 'messages_snapshot' and e.get('kind') == 'state')
    assert any(m.get('content') == TOOL_RESULT for m in state['messages'])


def test_artifact_tool_uses_common_contract_and_tool_evidence(runtime, monkeypatch):
    from lib.swarm.protocol import ArtifactStore
    store = ArtifactStore()
    scripted_provider(monkeypatch, runtime, [reply(calls=[tool_call(name='store_artifact',
        arguments='{"key":"evidence","content":"saved"}')]), reply()])
    result = agent_for(runtime, artifact_store=store).run()
    assert result.status == 'completed', result.error_message
    assert store.get('evidence') == 'saved'
    assert result.artifacts_written == ['evidence']
    assert result.tool_log[0]['tool'] == 'store_artifact'
    assert result.tool_log[0]['status'] == 'done'
    assert any(m.get('role') == 'tool' and 'Stored artifact' in m.get('content', '')
               for m in runtime.calls[1]['messages'])


@pytest.mark.parametrize('invalid_batch', [False, True])
def test_child_gateway_executes_only_scoped_catalog_and_disables_redelegation(runtime, monkeypatch, invalid_batch):
    import json
    runtime.parent['config']['contextExperiments'] = {'orchestration': {'multiAgent': 'read_only'}}
    calls = [{'name': TOOL['function']['name'], 'arguments': {}}]
    if invalid_batch:
        calls.append({'name': 'ungranted_tool', 'arguments': {}})
    scripted_provider(monkeypatch, runtime, [reply(calls=[tool_call(name='execute_tools', arguments=json.dumps({'calls': calls}))]), reply('Available evidence has been checked. ' * 20), reply('The ungranted tool is unavailable. The permitted evidence supports the conclusion. ' * 10)])
    result = agent_for(runtime).run()
    assert result.status == 'completed', result.error_message
    # Batch validation rejects all members before any side effect.
    assert len(runtime.tool_calls) == (0 if invalid_batch else 1)
    # 'disabled' is the internal-only redelegation fence (not a user-settable
    # mode); it fails closed through the policy/backend chain.
    assert runtime.calls[0]['_multi_agent_mode'] == 'disabled'
    child = runtime.children[0]
    assert set(child['_tool_gateway_names']) == {'search_tools', 'execute_tools', 'execute_program'}
    assert [tool['function']['name'] for tool in child['_executable_tool_catalog']] == [TOOL['function']['name']]
    assert 'execute_tools' in child['_toolContractDocumentsByName']


def test_round_checkpoint_receives_canonical_messages_after_tool_settlement(runtime):
    checkpoints = []
    agent = agent_for(runtime)
    def save(*, final=False):
        checkpoints.append((final, agent.result.rounds_used, deepcopy(agent.messages)))
    agent._checkpoint = save
    result = agent.run()
    assert result.status == 'completed', result.error_message
    tool_checkpoints = [row for row in checkpoints if not row[0] and row[1] == 1]
    intent = next(row for row in checkpoints if not row[0] and row[1] == 0
                  and any(m.get('tool_calls') for m in row[2]))
    assert any(m.get('tool_calls') for m in intent[2])
    assert not any(m.get('role') == 'tool' for m in intent[2])
    assert any(m.get('role') == 'tool' and m.get('content') == TOOL_RESULT
               for m in tool_checkpoints[-1][2])
    assert checkpoints[-1][0] and checkpoints[-1][1] == 2
    assert any(ANSWER in m.get('content', '') for m in checkpoints[-1][2])


def test_failed_owner_control_is_failed_evidence_and_releases_the_child(runtime):
    agent = agent_for(runtime)
    def broken_control():
        raise RuntimeError('Owner control port lost')
    agent.abort_check = broken_control
    result = agent.run()
    assert result.status == 'failed'
    assert 'evidence delivery failed' in result.error_message
    assert not runtime.calls and not runtime.tool_calls
    assert agent._child is None and agent.messages == []


def test_resume_restores_usage_tools_and_elapsed_without_replaying_committed_tool(runtime, monkeypatch):
    scripted_provider(monkeypatch, runtime, [reply('Continued answer')])
    agent = agent_for(runtime)
    prior = {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140,
             'cost_usd': 0.25, 'tool_calls_made': 2, 'elapsed_seconds': 7,
             'actuals_complete': False}
    agent.restore_checkpoint([{'role': 'user', 'content': 'objective'},
        {'role': 'assistant', 'tool_calls': [tool_call('committed')]},
        {'role': 'tool', 'tool_call_id': 'committed', 'content': 'saved receipt'}], 5, prior)
    result = agent.run()
    assert result.status == 'completed', result.error_message
    assert result.total_tokens == 163
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 43
    assert result.tool_calls_made == 2
    assert result.elapsed_seconds >= 7
    assert result.rounds_used == 6
    assert result.actuals_complete is False
    assert not runtime.tool_calls


def test_pretool_durability_failure_refuses_execution(runtime, monkeypatch):
    agent = agent_for(runtime)
    def unavailable(*, final=False):
        if not final and agent.result.checkpoint_phase == 'tools_in_flight':
            raise RuntimeError('durable tool intent unavailable')
    agent._checkpoint = unavailable
    result = agent.run()
    assert result.status == 'failed'
    assert 'durable tool intent unavailable' in result.error_message
    assert not runtime.tool_calls


def test_shutdown_during_tool_keeps_unknown_outcome_fence(runtime, monkeypatch):
    from threading import Event
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    from lib.swarm.restart_recovery import checkpoint_recovery_error
    from types import SimpleNamespace
    runtime.parent['abort_event'] = Event()
    def tool(task, tc, name, call_id, args, round_num, row, *_rest):
        task['aborted'] = True
        task['_abort_reason'] = 'server_shutdown'
        task['abort_event'].set()
        _finalize_tool_round(task, round_num, row, [], status='interrupted')
        return call_id, 'interrupted with unknown external outcome', False
    runtime.parent['_tool_env'] = SimpleNamespace(resolve=lambda name: tool)
    agent = agent_for(runtime)
    result = agent.run()
    assert result.status == 'interrupted'
    assert result.recovery_reason == 'server_shutdown'
    assert result.checkpoint_phase == 'tools_in_flight'
    assert checkpoint_recovery_error({'messages': agent.messages,
        'result': result.to_dict()}) == 'restart_tool_outcome_unknown'


@pytest.mark.parametrize('prior', [None, {
    'status': 'interrupted', 'prompt_tokens': 100, 'completion_tokens': 40,
    'total_tokens': 140, 'cost_usd': 0.125, 'checkpoint_phase': 'ready',
}])
def test_live_checkpoint_preserves_paid_usage_without_execution_limits(runtime, monkeypatch, prior):
    agent = agent_for(runtime)
    if prior:
        agent.restore_checkpoint([{'role': 'user', 'content': 'Continue evidence'}], 2, prior)
    scripted_provider(monkeypatch, runtime, [
        reply('Inspecting', calls=[tool_call()], usage={
            'prompt_tokens': 20, 'completion_tokens': 3, 'total_tokens': 23, 'cost_usd': 0.025}),
        reply('Verified', usage={
            'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12, 'cost_usd': 0.01}),
    ])
    receipts = []
    monkeypatch.setattr(agent, '_checkpoint', lambda **kw: receipts.append(deepcopy(agent.result.to_dict())))
    result = agent.run()
    assert result.status == 'completed', result.error_message
    intent = next(row for row in receipts if row['checkpoint_phase'] == 'tools_in_flight')
    assert intent['total_tokens'] == (prior or {}).get('total_tokens', 0) + 23
    assert intent['cost_usd'] == pytest.approx((prior or {}).get('cost_usd', 0) + 0.025)
    assert result.total_tokens == (prior or {}).get('total_tokens', 0) + 35
    assert result.cost_usd == pytest.approx((prior or {}).get('cost_usd', 0) + 0.035)


def test_resume_prior_receipt_is_visible_at_every_live_admission_boundary(runtime, monkeypatch):
    from lib.agent_core.execution_budget import ExecutionBudget
    agent = agent_for(runtime)
    prior = {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140,
             'cost_usd': 0.125, 'tool_calls_made': 2, 'actuals_complete': True,
             'elapsed_seconds': 7}
    agent.restore_checkpoint([{'role': 'user', 'content': 'Continue evidence'}], 3, prior)
    original_sink = agent._save_checkpoint
    observations = []
    def checkpoint(child, messages, rounds):
        budget = ExecutionBudget(child)
        observations.append((budget.consumption(), child['usage']['total_tokens']))
        original_sink(child, messages, rounds)
    agent._save_checkpoint = checkpoint

    elapsed_checkpoints = []
    agent._checkpoint = lambda **kw: elapsed_checkpoints.append(agent.result.elapsed_seconds)
    scripted_provider(monkeypatch, runtime, [reply(calls=[tool_call()]), reply()])
    result = agent.run()
    assert result.status == 'completed', result.error_message
    assert observations and observations[0] == ((140, 2), 140)
    assert ((163, 2), 163) in observations

    assert elapsed_checkpoints[0] > 7
    assert result.total_tokens == 186


def test_resume_preparation_failure_keeps_prior_paid_receipt(runtime, monkeypatch):
    agent = agent_for(runtime)
    agent.restore_checkpoint([{'role': 'user', 'content': 'Continue evidence'}], 3, {
        'status': 'interrupted', 'prompt_tokens': 100, 'completion_tokens': 40,
        'total_tokens': 140, 'cost_usd': 0.125, 'checkpoint_phase': 'ready'})
    def failed(child):
        raise RuntimeError('synthetic preparation failure')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.run_child_invocation', failed)
    result = agent.run()
    assert result.status == 'failed'
    assert result.total_tokens == 140
    assert result.cost_usd == pytest.approx(0.125)
    assert not runtime.calls


def test_resume_allocation_failure_keeps_paid_receipt_and_checkpoint(runtime, monkeypatch):
    agent = agent_for(runtime)
    agent.restore_checkpoint([{'role': 'user', 'content': 'Continue'}], 3, {
        'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140,
        'cost_usd': 0.125, 'elapsed_seconds': 7, 'actuals_complete': True})
    saved = []
    agent._checkpoint = lambda **kw: saved.append(deepcopy(agent.result.to_dict()))
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.create_child_invocation',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('route unavailable')))
    result = agent.run()
    assert result.status == 'failed' and 'route unavailable' in result.error_message
    assert result.total_tokens == saved[-1]['total_tokens'] == 140
    assert result.cost_usd == saved[-1]['cost_usd'] == pytest.approx(0.125)
    assert result.actuals_complete is True
    assert not runtime.calls


@pytest.mark.parametrize('complete', [True, False])
def test_checkpoint_completeness_follows_provider_receipt_not_shutdown(runtime, monkeypatch, complete):
    agent = agent_for(runtime)
    child_receipts = []
    def run(child):
        child['usage'] = {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}
        child['_invocationCheckpointPhase'] = 'ready'
        if not complete:
            child['_executionActualsComplete'] = False
        child['_invocationCheckpointSink'](child, child['messages'], 1)
        child.update(aborted=True, _abort_reason='server_shutdown', finishReason='aborted')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.run_child_invocation', run)
    agent._checkpoint = lambda **kw: child_receipts.append(deepcopy(agent.result.to_dict()))
    result = agent.run()
    assert result.status == 'interrupted'
    assert result.actuals_complete is complete
    assert all(receipt['actuals_complete'] is complete for receipt in child_receipts)
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.agent_core.execution_budget import ExecutionBudget
    child = create_child_invocation(runtime.parent, model='fixture-model', tools=[TOOL],
        objective='Continue', event_sink=lambda event: None, abort_check=lambda: False,
        checkpoint_result=result.to_dict())
    budget = ExecutionBudget({'id': 'budget', 'config': {'executionLimits': {
        'max_tokens': 1000, 'max_tool_calls': 20, 'deadline': None}}})
    budget.attach(child)
    assert budget.reason() == (None if complete else 'execution_token_usage_unknown')
    runtime.children.append(child)


@pytest.mark.parametrize('elapsed,remaining', [(7, 3), (10, None), (11, None)])
def test_restart_does_not_refund_child_active_timeout(runtime, monkeypatch, elapsed, remaining):
    agent = agent_for(runtime, spec=SubTaskSpec(role='general', objective='work', timeout_seconds=10))
    agent.restore_checkpoint([{'role': 'user', 'content': 'Continue'}], 2, {
        'total_tokens': 140, 'elapsed_seconds': elapsed})
    policies = []
    def run(child):
        policies.append(child['_invocationPolicy'].timeout_seconds)
        child.update(content='continued', finishReason='stop', status='done')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.run_child_invocation', run)
    result = agent.run()
    assert policies == ([] if remaining is None else [remaining])
    if remaining is None:
        assert result.status == 'failed' and 'allowance exhausted' in result.error_message
        assert result.total_tokens == 140
        assert not runtime.children
    assert result.elapsed_seconds >= elapsed


def test_token_only_requests_snapshot_cost_once_through_repeated_resume(runtime, monkeypatch):
    monkeypatch.setattr('lib.cost.compute_cost', lambda usage, **kw: {'costUsd': 0.05})
    agent = agent_for(runtime)
    agent.restore_checkpoint([{'role': 'user', 'content': 'Continue'}], 2, {
        'total_tokens': 140, 'cost_usd': 0.125, 'actuals_complete': True})
    scripted_provider(monkeypatch, runtime, [reply(calls=[tool_call()]), reply()])
    receipts = []
    agent._checkpoint = lambda **kw: receipts.append(deepcopy(agent.result.to_dict()))
    result = agent.run()
    intent = next(row for row in receipts if row['checkpoint_phase'] == 'tools_in_flight')
    assert intent['cost_usd'] == pytest.approx(0.175)
    assert result.cost_usd == pytest.approx(0.225)
    resumed = agent_for(runtime)
    resumed.restore_checkpoint([{'role': 'user', 'content': 'Continue again'}], result.rounds_used,
                               result.to_dict())
    scripted_provider(monkeypatch, runtime, [reply()])
    assert resumed.run().cost_usd == pytest.approx(0.275)


@pytest.mark.parametrize('exception', [False, True])
def test_silent_provider_internal_stall_fails_and_keeps_receipts(runtime, monkeypatch, exception):
    import lib.tasks_pkg.manager._stream as stream
    from lib.llm.llm_errors import AbortedError
    from lib.swarm.liveness import ProgressBeacon
    from lib.turn_verdict import derive_task_verdict
    import time

    def silent(body, **kwargs):
        runtime.calls.append(deepcopy(body))
        if len(runtime.calls) == 1:
            kwargs['on_content']('Partial findings')
            return reply('Partial findings', [tool_call()])
        end = time.monotonic() + 3
        while not kwargs['abort_check']():
            assert time.monotonic() < end, 'guard did not stop silent provider'
            time.sleep(0.01)
        if exception:
            raise AbortedError('physical client abort after internal stop')
        return reply('Safe partial prefix', finish='aborted')

    monkeypatch.setattr(stream, 'dispatch_stream', silent)
    agent = agent_for(runtime)
    agent.progress_beacon = ProgressBeacon(stall_timeout=0.05)
    result = agent.run()
    child = runtime.children[0]
    verdict = derive_task_verdict(child)
    assert result.status == verdict.status.value == 'failed'
    assert verdict.cause == 'invocation_stalled'
    assert 'stalled' in result.error_message.lower()

    assert child['finishReason'] == 'error'
    assert child['error']['context'] == 'invocation-guard'
    assert 'stalled' in child['error']['detail'].lower()
    assert '0.05s' in result.error_message
    assert child['_invocationStopEvidence']['silentSeconds'] >= 0.05
    assert child['aborted'] and child['abort_event'].is_set()
    assert len(runtime.calls) == 2 and len(runtime.tool_calls) == 1
    assert result.total_tokens >= 23
    assert child['toolRounds'][0]['status'] == 'done'
    assert result.final_answer
    assert not runtime.parent.get('aborted') and not runtime.parent['abort_event'].is_set()
    # Snapshot serialization must preserve the existing failed/error carrier.
    import json
    cold = json.loads(json.dumps(result.to_dict()))
    assert cold['status'] == 'failed' and cold['error_message'] == result.error_message

    from lib.swarm.master import MasterOrchestrator
    master = MasterOrchestrator(task_id='root', conv_id='cold-fixture', user_id=771,
                               specs=[agent.spec])
    master._results_by_id[agent.spec.id] = (agent.spec, result)
    live = master._build_agent_snapshot()['agents'][0]
    restored = master._result_from_dict(cold)
    master._results_by_id[agent.spec.id] = (agent.spec, restored)
    snapshot = master._build_agent_snapshot()['agents'][0]
    assert live['status'] == snapshot['status'] == 'failed'
    assert live['error'] == snapshot['error'] == result.error_message

@pytest.mark.parametrize('reason', ['stalled', 'deadline', 'stop'])
def test_exception_after_guard_preserves_authority(runtime, monkeypatch, reason):
    import lib.tasks_pkg.child_invocation as owner
    from lib.tasks_pkg.invocation import invocation_abort_requested
    from lib.swarm.liveness import ProgressBeacon
    from lib.llm.llm_errors import AbortedError
    import time

    def interrupted(child):
        if reason == 'deadline':
            child['_invocationDeadline'] = time.monotonic() - 1
        elif reason == 'stop':
            runtime.stop.set()
        else:
            beacon = ProgressBeacon(stall_timeout=0)
            beacon.touch(child['id'])
            child['_invocationProgressBeacon'] = beacon
            child['_invocationProgressKey'] = child['id']
        assert invocation_abort_requested(child)
        raise AbortedError('Transport cancellation')

    monkeypatch.setattr(owner, 'run_child_invocation', interrupted)
    result = agent_for(runtime).run()
    assert result.status == ('cancelled' if reason == 'stop' else 'failed')
    if reason != 'stop':
        assert ('stalled' if reason == 'stalled' else 'deadline') in result.error_message.lower()
    assert not runtime.parent.get('aborted')


def test_root_silent_provider_guard_fails_with_physical_abort_and_tool_receipts(runtime, monkeypatch):
    import time
    import lib.tasks_pkg.orchestrator._run as runner
    import lib.tasks_pkg.manager._stream as stream
    from lib.tasks_pkg.invocation import execute_invocation, ROOT_INVOCATION_POLICY
    from lib.swarm.liveness import ProgressBeacon
    from lib.turn_verdict import derive_task_verdict
    task = runtime.parent
    original = runner.assemble_round_tools
    def assemble(cfg, current, mcfg):
        if current is task:
            current['_executable_tool_catalog'] = [TOOL]
            return [TOOL], True
        return original(cfg, current, mcfg)
    monkeypatch.setattr(runner, 'assemble_round_tools', assemble)
    beacon = ProgressBeacon(stall_timeout=0.05)
    task['_invocationProgressBeacon'] = beacon
    task['_invocationProgressKey'] = task['id']
    beacon.touch(task['id'])
    def provider(body, **kwargs):
        runtime.calls.append(deepcopy(body))
        if len(runtime.calls) == 1:
            kwargs['on_content']('Paid partial output')
            return reply('Paid partial output', [tool_call()])
        end = time.monotonic() + 3
        while not kwargs['abort_check']():
            assert time.monotonic() < end
            time.sleep(0.01)
        return reply('Safe partial prefix', finish='aborted')
    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    execute_invocation(task, policy=ROOT_INVOCATION_POLICY)
    assert derive_task_verdict(task).status.value == 'failed'
    assert derive_task_verdict(task).cause == 'invocation_stalled'
    assert task['streamState'] == 'client_aborted'
    assert task['aborted'] and task['abort_event'].is_set()
    assert 'stalled' in task['error']['detail'].lower()
    assert len(runtime.calls) == 2 and len(runtime.tool_calls) == 1
    assert task['usage']['total_tokens'] >= 23
    assert task['toolRounds'][0]['status'] == 'done'
    assert any('Paid partial output' in segment.get('text', '')
               for segment in task['segments'])


@pytest.mark.parametrize('owner_stop', [False, True])
def test_master_driver_stall_cannot_impersonate_owner_stop(runtime, monkeypatch, owner_stop):
    import time
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.liveness import ProgressBeacon
    from lib.turn_verdict import derive_task_verdict
    import lib.tasks_pkg.manager._stream as stream

    monkeypatch.setattr(SubAgent, '_checkpoint', lambda *args, **kwargs: None)
    spec = SubTaskSpec(role='general', objective='Inspect driver-first silence')
    master = MasterOrchestrator(runtime.parent['id'], runtime.parent['convId'], [spec],
        user_id=771, model='fixture-model', all_tools=[TOOL], parent_task=runtime.parent,
        abort_check=runtime.stop.is_set)
    master._beacon = ProgressBeacon(stall_timeout=0.05)
    child_agent = master._make_agent(spec)

    def dispatch(body, **kwargs):
        runtime.calls.append(deepcopy(body))
        if len(runtime.calls) == 1:
            kwargs['on_content']('Partial work receipt')
            return reply('Partial work receipt', [tool_call()])
        # Driver has judged silence before the child's next provider poll.
        master._stop_stalled = True
        if owner_stop:
            runtime.stop.set()
        end = time.monotonic() + 3
        while not kwargs['abort_check']():
            assert time.monotonic() < end
            time.sleep(0.01)
        return reply('Preserved evidence', finish='aborted')

    monkeypatch.setattr(stream, 'dispatch_stream', dispatch)
    result = child_agent.run()
    child = runtime.children[0]
    assert child['abort_event'].is_set()
    assert result.status == ('cancelled' if owner_stop else 'failed')
    assert derive_task_verdict(child).cause == ('user_abort' if owner_stop else 'invocation_stalled')
    if not owner_stop:
        assert 'stalled' in result.error_message.lower()
    assert len(runtime.calls) == 2 and len(runtime.tool_calls) == 1
    assert not runtime.parent.get('aborted') and not runtime.parent['abort_event'].is_set()


@pytest.mark.parametrize('reason', ['stalled', 'deadline'])
def test_internal_stop_envelope_survives_canonical_error_serialization(reason):
    import json
    import threading
    import time
    from lib.error_envelope import from_json, to_json
    from lib.tasks_pkg.invocation import invocation_abort_requested
    from lib.tasks_pkg.orchestrator._finalize import _settle_post_loop_finish_reason
    from lib.swarm.liveness import ProgressBeacon
    from lib.turn_verdict import derive_task_verdict

    task = {'id': 'cold-stop', 'aborted': False, 'abort_event': threading.Event()}
    if reason == 'stalled':
        task['_invocationProgressBeacon'] = ProgressBeacon(stall_timeout=0)
    else:
        task['_invocationDeadline'] = time.monotonic() - 1
    assert invocation_abort_requested(task)
    finish = _settle_post_loop_finish_reason(task, 'stop', loop_exit_reason='abort',
        abort_detected_phase='post_stream', model='fixture-model', tid='cold-stop')
    assert finish == 'error'
    # A cold task has no private guard stamp, but the durable error column and
    # terminal finish must retain the exact cause even with physical abort facts.
    cold = json.loads(json.dumps({'status': 'done', 'aborted': True,
        'finishReason': finish, 'streamState': 'client_aborted',
        'error': from_json(to_json(task['error']))}))
    verdict = derive_task_verdict(cold)
    assert verdict.status.value == 'failed' and verdict.cause == 'invocation_' + reason
    assert cold['error']['detail'] == task['error']['detail']


def test_parent_stop_checkpoint_continue_does_not_repeat_committed_tool(runtime, monkeypatch):
    from lib.swarm.master import MasterOrchestrator
    import lib.tasks_pkg.manager._stream as stream

    spec = SubTaskSpec(id='recover-child', role='general', objective='Verify evidence')
    agent = agent_for(runtime, spec=spec)
    master = MasterOrchestrator(task_id=runtime.parent['id'], conv_id='', user_id=771, specs=[spec])
    master._agents[spec.id] = agent
    requests = []
    def dispatch(body, **kwargs):
        requests.append(deepcopy(body))
        if len(requests) == 1:
            return reply('Investigating', [tool_call()])
        master.abort()
        return reply('Partial progress', finish='aborted')
    monkeypatch.setattr(stream, 'dispatch_stream', dispatch)
    checkpoint = {}
    def save(**kwargs):
        checkpoint.update(messages=deepcopy(agent.messages), result=agent.result.to_dict())
    monkeypatch.setattr(agent, '_checkpoint', save)
    result = agent.run()
    assert result.status == 'cancelled'
    assert result.recovery_reason == 'parent_stop'
    assert len(runtime.tool_calls) == 1
    assert any(msg.get('role') == 'tool' for msg in checkpoint['messages'])

    resumed = agent_for(runtime, spec=spec)
    resumed.restore_checkpoint(checkpoint['messages'], result.rounds_used, checkpoint['result'])
    scripted_provider(monkeypatch, runtime, [reply('Finished from prior evidence')])
    completed = resumed.run()
    assert completed.status == 'completed'
    assert len(runtime.tool_calls) == 1
    assert completed.total_tokens > result.total_tokens
    assert completed.rounds_used > result.rounds_used
