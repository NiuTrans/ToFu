"""Behavior parity for the root policy adapter on the shared runner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import lib.tasks_pkg.orchestrator._root_agent_loop as root_loop
pytestmark = pytest.mark.unit


def _state():
    return SimpleNamespace(
        model='kimi-k3',
        preset='default',
        thinking_enabled=False,
        exit_reason='running',
        abort_phase=None,
        consecutive_tool_timeouts=0,
        assistant_msg=None,
        last_finish_reason=None,
        last_usage=None,
        accumulated_usage={},
        api_rounds=[],
        tool_call_happened=False,
        tool_round_num=0,
        last_checkpoint_ts=0.0,
    )


def _request(task, state):
    return root_loop.RootLoopRequest(
        task=task,
        state=state,
        messages=[],
        tool_list=[{'type': 'function', 'function': {'name': 'read_files'}}],
        all_search_results_text=[],
        cfg={},
        tid='abcdef12',
        thinking_depth='low',
        temperature=0.2,
        max_tokens=1024,
        response_format=None,
        project_path=None,
        project_enabled=False,
        search_enabled=False,
    )


def _install_common(monkeypatch, events, *, tool_timeout=False):
    monkeypatch.setattr(
        root_loop, 'check_task_resource_budget', lambda *a, **k: False)
    monkeypatch.setattr(
        root_loop, 'emit_round_open',
        lambda task, state, rnd: events.append(('open', rnd)))
    monkeypatch.setattr(
        root_loop, 'run_round_message_hygiene',
        lambda *a, **k: events.append(('hygiene', k['round_num'])))
    monkeypatch.setattr(
        root_loop, 'drain_and_inject_inbox',
        lambda **k: events.append(('inbox', k['round_num'])))
    monkeypatch.setattr(
        root_loop, 'build_round_request',
        lambda task, state, messages, tools, **k: (tools, {'messages': []}))
    monkeypatch.setattr(
        root_loop, 'build_stream_accumulator',
        lambda *a, **k: SimpleNamespace(announced_tc_map={}))
    monkeypatch.setattr(
        root_loop, 'stamp_round_cache_accounting',
        lambda *a, **k: events.append(('cache', k['round_num'])))
    monkeypatch.setattr(
        root_loop, 'settle_stream_accumulator',
        lambda *a, **k: events.append(('settle', k['round_num'])))
    monkeypatch.setattr(
        root_loop, 'check_round_gates',
        lambda *a, **k: events.append(('gate', k['round_num'])) or False)
    monkeypatch.setattr(
        root_loop, 'append_assistant_tool_call_message',
        lambda *a, **k: events.append(('tool_open', k['round_num'])))

    def abort_before_tools(task, state, messages, *, round_num, tid):
        events.append(('abort_before_tools', round_num))
        if not task.get('aborted'):
            return False
        state.abort_phase = f'before_tool_exec_round_{round_num}'
        state.exit_reason = f'aborted_before_tools_round_{round_num}'
        return True

    def abort_at_round_start(task, state, *, round_num, tid):
        events.append(('abort_at_start', round_num))
        state.abort_phase = f'loop_start_round_{round_num}'
        state.exit_reason = f'aborted_at_round_{round_num}'
        return True

    monkeypatch.setattr(
        root_loop, 'handle_abort_before_tools', abort_before_tools)
    monkeypatch.setattr(
        root_loop, 'handle_abort_at_round_start', abort_at_round_start)
    monkeypatch.setattr(
        root_loop, 'run_tool_dispatch',
        lambda *a, **k: events.append(('tools', k['round_num']))
        or tool_timeout)

    def project_timeout(
        task,
        state,
        *,
        round_num,
        tid,
        tool_timed_out,
        max_consecutive_tool_timeouts,
        chassis_consecutive_count,
    ):
        events.append((
            'timeout', round_num, tool_timed_out,
            chassis_consecutive_count))
        state.consecutive_tool_timeouts = chassis_consecutive_count
        if chassis_consecutive_count >= max_consecutive_tool_timeouts:
            state.exit_reason = (
                f'consecutive_tool_timeouts_{chassis_consecutive_count}')
            return True
        return False

    monkeypatch.setattr(
        root_loop, 'handle_tool_timeout_circuit_breaker', project_timeout)
    monkeypatch.setattr(
        root_loop, 'handle_tool_loop_circuit_breaker',
        lambda *a, **k: events.append(('progress', k['round_num'])) or False)
    monkeypatch.setattr(
        root_loop, 'run_round_checkpoint_and_close',
        lambda *a, **k: events.append(('checkpoint', k['round_num'])))


def _install_script(monkeypatch, state, script, events, *, abort_on_round=None):
    turns = iter(script)

    def llm_call(task, current_state, *args, round_num, **kwargs):
        message, finish, usage = next(turns)
        current_state.assistant_msg = message
        current_state.last_finish_reason = finish
        current_state.last_usage = usage
        current_state.api_rounds.append({'roundNum': round_num})
        events.append(('llm', round_num))
        if abort_on_round == round_num:
            task['aborted'] = True
        return 'proceed'

    monkeypatch.setattr(root_loop, 'run_llm_call_with_fallback', llm_call)


def _tool_message(round_num=0):
    return {
        'role': 'assistant',
        'content': '',
        'tool_calls': [{
            'id': f't{round_num}',
            'function': {'name': 'read_files', 'arguments': '{}'},
        }],
    }


def test_tool_round_then_verified_natural_completion(monkeypatch):
    task = {'aborted': False, 'error': None}
    state = _state()
    events = []
    _install_common(monkeypatch, events)
    _install_script(monkeypatch, state, [
        (_tool_message(0), 'tool_calls', {'total_tokens': 10}),
        ({'role': 'assistant', 'content': 'done'}, 'stop',
         {'total_tokens': 5}),
    ], events)

    def decide(task, current_state, *, round_num, **kwargs):
        if current_state.assistant_msg.get('tool_calls'):
            return 'proceed', kwargs['premature_retry_count']
        current_state.exit_reason = f'no_tool_calls_round_{round_num}'
        return 'break', kwargs['premature_retry_count']

    monkeypatch.setattr(root_loop, 'apply_stream_decision', decide)
    result = root_loop.run_root_agent_loop(_request(task, state))

    assert not result.outcome.completed and not result.outcome.aborted
    assert result.last_round_num == 1
    assert state.exit_reason == 'no_tool_calls_round_1'
    assert state.tool_call_happened is True
    assert [e for e in events if e[0] == 'tools'] == [('tools', 0)]
    assert [e for e in events if e[0] == 'checkpoint'] == [
        ('checkpoint', 0)]


def test_hygiene_receives_exact_remaining_api_round_budget(monkeypatch):
    task = {'aborted': False, 'error': None}
    state = _state()
    state.api_rounds.extend([{'round': 1}, {'round': 2}])
    events = []
    _install_common(monkeypatch, events)
    remaining = []
    monkeypatch.setattr(
        root_loop,
        'run_round_message_hygiene',
        lambda *args, **kwargs: remaining.append(
            kwargs['remaining_api_rounds']),
    )
    _install_script(monkeypatch, state, [
        ({'role': 'assistant', 'content': 'done'}, 'stop', {}),
    ], events)

    def decide(task, current_state, *, round_num, **kwargs):
        current_state.exit_reason = f'no_tool_calls_round_{round_num}'
        return 'break', kwargs['premature_retry_count']

    monkeypatch.setattr(root_loop, 'apply_stream_decision', decide)
    request = _request(task, state)
    request.cfg = {'maxApiRounds': 5}

    root_loop.run_root_agent_loop(request)

    assert remaining == [3]


def test_program_continuation_gets_next_round_without_tool_execution(
    monkeypatch,
):
    task = {'aborted': False, 'error': None}
    state = _state()
    events = []
    _install_common(monkeypatch, events)
    _install_script(monkeypatch, state, [
        ({'role': 'assistant', 'content': ''}, 'stop', {'_program_pending': 1}),
        ({'role': 'assistant', 'content': 'final'}, 'stop', {}),
    ], events)
    actions = iter(['program_continue', 'break'])

    def decide(task, current_state, *, round_num, **kwargs):
        action = next(actions)
        if action == 'break':
            current_state.exit_reason = f'no_tool_calls_round_{round_num}'
        return action, kwargs['premature_retry_count']

    monkeypatch.setattr(root_loop, 'apply_stream_decision', decide)
    result = root_loop.run_root_agent_loop(_request(task, state))

    assert not result.outcome.completed and result.last_round_num == 1
    assert [e for e in events if e[0] == 'llm'] == [('llm', 0), ('llm', 1)]
    assert [e for e in events if e[0] == 'gate'] == [('gate', 0)]
    assert not [e for e in events if e[0] == 'tools']


def test_post_stream_abort_runs_root_cleanup_before_any_tool(monkeypatch):
    task = {'aborted': False, 'error': None}
    state = _state()
    events = []
    _install_common(monkeypatch, events)
    _install_script(
        monkeypatch, state,
        [(_tool_message(), 'tool_calls', {})], events,
        abort_on_round=0)
    monkeypatch.setattr(
        root_loop, 'apply_stream_decision',
        lambda *a, **k: ('proceed', k['premature_retry_count']))

    result = root_loop.run_root_agent_loop(_request(task, state))

    assert result.outcome.aborted
    assert result.outcome.exit_reason == 'aborted_before_tools_round_0'
    assert state.exit_reason == 'aborted_before_tools_round_0'
    assert ('tool_open', 0) in events
    assert ('abort_before_tools', 0) in events
    assert not [e for e in events if e[0] == 'tools']
    assert not [e for e in events if e[0] == 'checkpoint']


def test_chassis_timeout_counter_halts_before_third_checkpoint(monkeypatch):
    task = {'aborted': False, 'error': None}
    state = _state()
    events = []
    _install_common(monkeypatch, events, tool_timeout=True)
    _install_script(monkeypatch, state, [
        (_tool_message(i), 'tool_calls', {}) for i in range(3)
    ], events)
    monkeypatch.setattr(
        root_loop, 'apply_stream_decision',
        lambda *a, **k: ('proceed', k['premature_retry_count']))

    result = root_loop.run_root_agent_loop(_request(task, state))

    assert result.outcome.halted
    assert result.outcome.exit_reason == 'tool_timeout'
    assert result.outcome.consecutive_tool_timeouts == 3
    assert result.last_round_num == 2
    assert state.exit_reason == 'consecutive_tool_timeouts_3'
    assert [e for e in events if e[0] == 'checkpoint'] == [
        ('checkpoint', 0), ('checkpoint', 1)]
    assert [e for e in events if e[0] == 'progress'] == [
        ('progress', 0), ('progress', 1)]


def test_abort_during_batch_tools_stops_before_another_provider_call(
    monkeypatch,
):
    """The batch pipeline settles its round; chassis catches Stop at top."""
    task = {'aborted': False, 'error': None}
    state = _state()
    events = []
    _install_common(monkeypatch, events)
    _install_script(
        monkeypatch, state,
        [(_tool_message(), 'tool_calls', {})], events)
    monkeypatch.setattr(
        root_loop, 'apply_stream_decision',
        lambda *a, **k: ('proceed', k['premature_retry_count']))

    def aborting_tool_batch(*args, **kwargs):
        events.append(('tools', kwargs['round_num']))
        task['aborted'] = True
        return False

    monkeypatch.setattr(root_loop, 'run_tool_dispatch', aborting_tool_batch)
    result = root_loop.run_root_agent_loop(_request(task, state))

    assert result.outcome.aborted
    assert result.outcome.exit_reason == 'aborted_at_round_1'
    assert result.last_round_num == 1
    assert [e for e in events if e[0] == 'llm'] == [('llm', 0)]
    assert [e for e in events if e[0] == 'checkpoint'] == [
        ('checkpoint', 0)]
    assert ('abort_at_start', 1) in events


def test_resource_gate_stops_before_provider_dispatch(monkeypatch):
    task = {'aborted': False, 'error': None}
    state = _state()
    events = []
    _install_common(monkeypatch, events)

    def resource_gate(task, current_state, *, round_num, cfg, messages):
        assert messages == []
        current_state.exit_reason = f'budget_exceeded_round_{round_num}'
        return True

    monkeypatch.setattr(root_loop, 'check_task_resource_budget', resource_gate)
    monkeypatch.setattr(
        root_loop, 'run_llm_call_with_fallback',
        lambda *a, **k: pytest.fail('provider call crossed resource gate'))
    result = root_loop.run_root_agent_loop(_request(task, state))

    assert result.outcome.halted
    assert result.outcome.exit_reason == 'budget_exceeded_round_0'
    assert result.last_round_num == 0
    assert not events
