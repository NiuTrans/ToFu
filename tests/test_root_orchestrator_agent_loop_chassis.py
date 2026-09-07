"""Behavioral adoption contract for the root orchestrator ReAct chassis."""

from __future__ import annotations

import dis
import threading
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


def _request(adapter):
    state = SimpleNamespace(exit_reason='running')
    request = adapter.RootLoopRequest(
        task={'id': 'root-chassis-task', 'aborted': False},
        state=state,
        messages=[],
        tool_list=[],
        all_search_results_text=[],
        cfg={},
        tid='root-cha',
        thinking_depth=None,
        temperature=None,
        max_tokens=128,
        response_format=None,
        project_path=None,
        project_enabled=False,
        search_enabled=False,
    )
    return request, state


@pytest.fixture
def chassis_delegation(monkeypatch):
    import lib.tasks_pkg.orchestrator._root_agent_loop as adapter
    from lib.agent_loop import LoopOutcome

    request, state = _request(adapter)
    captured = []
    outcome = LoopOutcome(exit_reason='stubbed-chassis-stop')

    def fake_runner(**kwargs):
        captured.append(kwargs)
        return outcome

    monkeypatch.setattr(adapter, 'run_agent_loop', fake_runner)
    result = adapter.run_root_agent_loop(request)
    return adapter, request, state, captured, outcome, result


def test_root_delegates_once_without_a_private_round_loop(chassis_delegation):
    adapter, _request_value, state, captured, outcome, result = (
        chassis_delegation)
    backward_jumps = [
        instruction.opname
        for instruction in dis.get_instructions(adapter.run_root_agent_loop)
        if 'JUMP_BACKWARD' in instruction.opname
    ]
    assert backward_jumps == []
    assert len(captured) == 1, 'root must invoke the shared chassis once'
    assert result.outcome is outcome
    assert result.last_round_num == -1
    assert state.exit_reason == 'stubbed-chassis-stop'


def test_root_wires_every_control_boundary_to_the_chassis(chassis_delegation):
    adapter, request, _state, captured, _outcome, _result = chassis_delegation
    call = captured[0]
    required = {
        'abort', 'dispatch', 'decide_round',
        'on_tool_round', 'before_tools', 'execute_tools',
        'max_consecutive_tool_timeouts', 'on_tool_timeout_state',
        'after_tools', 'on_round_end', 'on_abort',
    }
    assert required <= call.keys(), sorted(required - call.keys())
    assert call['round_tools'] is request.tool_list
    assert call['max_consecutive_tool_timeouts'] == (
        adapter._MAX_CONSECUTIVE_TOOL_TIMEOUTS)
    for name in required - {'abort', 'max_consecutive_tool_timeouts'}:
        assert callable(call[name]), name
    assert call['abort'].aborted is False


def test_root_timeout_projection_consumes_the_chassis_count(monkeypatch):
    import lib.tasks_pkg.orchestrator._root_agent_loop as adapter

    request, state = _request(adapter)
    captured = {}

    def observe(task, observed_state, **kwargs):
        captured.update(kwargs)
        captured['task'] = task
        captured['state'] = observed_state

    monkeypatch.setattr(
        adapter, 'handle_tool_timeout_circuit_breaker', observe)
    hooks = adapter._RootLoopHooks(request)
    hooks.observe_timeout_state(
        round_num=4,
        tool_timed_out=True,
        consecutive_count=2,
        timeout_limit=3,
    )

    assert captured == {
        'task': request.task,
        'state': state,
        'round_num': 4,
        'tid': request.tid,
        'tool_timed_out': True,
        'max_consecutive_tool_timeouts': 3,
        'chassis_consecutive_count': 2,
    }


def test_flow_work_turn_delegates_to_root_run_task(monkeypatch):
    import lib.tasks_pkg.orchestrator._turn as turn_module

    calls = []

    def fake_run_task(task):
        calls.append(task)
        assert task['_flow_managed'] is True
        task['content'] = 'completed through root'
        task['thinking'] = 'trace'
        task['usage'] = {'total_tokens': 7}
        task['finishReason'] = 'stop'

    monkeypatch.setattr(turn_module, 'run_task', fake_run_task)
    messages = [{'role': 'user', 'content': 'work'}]
    task = {
        'id': 'flow-root-task',
        'content_lock': threading.Lock(),
        'messages': [],
        'content': 'stale',
        'thinking': 'stale',
    }

    result = turn_module._run_single_turn(task, messages)

    assert calls == [task]
    assert result == {
        'content': 'completed through root',
        'thinking': 'trace',
        'usage': {'total_tokens': 7},
        'finishReason': 'stop',
        'messages': messages,
        'error': None,
    }
    assert '_flow_managed' not in task


def test_flow_work_turn_preserves_outer_goal_flow_ownership(monkeypatch):
    import lib.tasks_pkg.orchestrator._turn as turn_module

    def fake_run_task(task):
        assert task['_flow_managed'] is True
        task['content'] = 'one inner turn'
        task['finishReason'] = 'stop'

    monkeypatch.setattr(turn_module, 'run_task', fake_run_task)
    task = {
        'id': 'goal-flow-root-task',
        'content_lock': threading.Lock(),
        'messages': [],
        '_flow_managed': True,
    }

    turn_module._run_single_turn(
        task, [{'role': 'user', 'content': 'continue goal'}])

    assert task['_flow_managed'] is True
