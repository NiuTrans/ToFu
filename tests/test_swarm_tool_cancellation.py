"""Regression coverage for Swarm tools that outlive their LLM round."""

from __future__ import annotations

import threading
import time

import pytest

from lib.swarm.agent import SubAgent
from lib.swarm.liveness import ProgressBeacon
from lib.swarm.master import MasterOrchestrator
from lib.swarm.types import SubTaskSpec


pytestmark = pytest.mark.unit


def _agent(*, abort_check=None, timeout_seconds=0, tools=None,
           dispatch_stream_fn=None):
    return SubAgent(
        SubTaskSpec(
            id='guarded', role='browser', objective='inspect the page',
            timeout_seconds=timeout_seconds),
        parent_task={'id': 'parent', 'convId': 'conv', 'config': {}},
        all_tools=tools or [],
        model='test-model',
        thinking_enabled=False,
        abort_check=abort_check,
        dispatch_stream_fn=dispatch_stream_fn,
    )


def test_effective_tool_prompt_never_promises_absent_browser_js():
    agent = _agent(tools=[{
        'type': 'function',
        'function': {'name': 'browser_read_page', 'parameters': {}},
    }])
    prompt = agent.messages[0]['content']

    assert 'Available tools: browser_read_page.' in prompt
    assert 'browser_execute_js' not in prompt
    assert 'Never emulate a missing browser or privileged tool' in prompt


def test_rehydration_refreshes_stale_system_authority():
    tools = [
        {'type': 'function',
         'function': {'name': 'browser_preview_page', 'parameters': {}}},
        {'type': 'function',
         'function': {'name': 'run_command', 'parameters': {}}},
    ]
    spec = SubTaskSpec(id='visual', role='browser', objective='inspect page')
    master = MasterOrchestrator(
        task_id='parent', conv_id='conv', specs=[spec], all_tools=tools,
        model='test-model', thinking_enabled=False)
    master._resume_messages['visual'] = [
        {'role': 'system',
         'content': 'Use browser_execute_js; emulate it with run_command.'},
        {'role': 'user', 'content': 'inspect page'},
        {'role': 'assistant', 'content': 'prior progress'},
    ]

    agent = master._make_agent(spec)

    assert len(agent.messages) == 3
    prompt = agent.messages[0]['content']
    assert 'browser_execute_js' not in prompt
    assert 'Never emulate a missing browser or privileged tool' in prompt
    assert agent.messages[-1]['content'] == 'prior progress'
    names = {t['function']['name'] for t in agent.tools}
    assert 'browser_preview_page' in names
    assert 'run_command' not in names
    assert 'write_file' not in names


def test_dispatch_proxy_is_isolated_unattended_and_keeps_unbounded_default(
        monkeypatch):
    captured = {}

    def fake_execute(task, tc, name, tc_id, args, rn, round_entry, *rest):
        captured.update(task=task, args=args, round_entry=round_entry)
        return tc_id, 'ok', False

    monkeypatch.setattr('lib.tasks_pkg.executor._execute_tool_one', fake_execute)
    agent = _agent()
    parent_events = agent.parent_task.setdefault('events', ['parent-event'])
    parent_rounds = agent.parent_task.setdefault('toolRounds', ['parent-round'])

    result = agent._dispatch_tool(
        {'id': 'tc1', 'function': {'name': 'run_command'}},
        'run_command', {'command': 'echo ok'}, 1)

    assert result == 'ok'
    assert 'timeout' not in captured['args']
    assert captured['task']['_unattended'] is True
    assert captured['task']['_suppressCheckpoint'] is True
    assert captured['task']['events'] == []
    assert captured['task']['toolRounds'] == []
    assert captured['task']['events'] is not parent_events
    assert captured['task']['toolRounds'] is not parent_rounds
    assert hasattr(captured['task']['content_lock'], 'acquire')


def test_explicit_agent_deadline_reaches_blocked_run_command(monkeypatch):
    captured = {}

    def fake_execute(task, tc, name, tc_id, args, rn, round_entry, *rest):
        captured['timeout'] = args.get('timeout')
        return tc_id, 'ok', False

    monkeypatch.setattr('lib.tasks_pkg.executor._execute_tool_one', fake_execute)
    agent = _agent(timeout_seconds=30)
    agent._run_deadline_monotonic = time.monotonic() + 0.4

    agent._dispatch_tool(
        {'id': 'tc1', 'function': {'name': 'run_command'}},
        'run_command', {'command': 'sleep 10', 'timeout': 0}, 1)

    assert 0 < captured['timeout'] <= 0.4


def test_parent_abort_is_mirrored_while_executor_is_blocked(monkeypatch):
    abort = threading.Event()
    entered = threading.Event()

    def fake_execute(task, tc, name, tc_id, args, rn, round_entry, *rest):
        entered.set()
        deadline = time.monotonic() + 2
        while not task.get('aborted') and time.monotonic() < deadline:
            time.sleep(0.01)
        return tc_id, 'aborted' if task.get('aborted') else 'missed', False

    monkeypatch.setattr('lib.tasks_pkg.executor._execute_tool_one', fake_execute)
    agent = _agent(abort_check=abort.is_set)
    timer = threading.Timer(0.1, abort.set)
    timer.start()
    try:
        result = agent._dispatch_tool(
            {'id': 'tc1', 'function': {'name': 'run_command'}},
            'run_command', {'command': 'sleep 10'}, 1)
    finally:
        timer.cancel()

    assert entered.is_set()
    assert result == 'aborted'


def test_repeated_unavailable_tool_calls_trip_authority_breaker():
    calls = {'n': 0}

    def dispatch(_body, **_kwargs):
        calls['n'] += 1
        return ({
            'role': 'assistant', 'content': '',
            'tool_calls': [{
                'id': f'bad-{calls["n"]}',
                'function': {'name': 'run_command',
                             'arguments': '{"command":"echo bypass"}'},
            }],
        }, 'tool_calls', {})

    agent = _agent(
        tools=[{'type': 'function', 'function': {
            'name': 'browser_preview_page', 'parameters': {}}}],
        dispatch_stream_fn=dispatch)
    agent._finalize_with_wrapup = lambda reason, **_kw: setattr(
        agent.result, 'final_answer', reason)

    agent._run_loop(time.time())

    assert calls['n'] == 2
    assert agent._tool_authority_rejections == 2
    assert agent.result.rounds_used == 2
    assert agent.result.status == 'completed'
    assert 'unavailable tool calls' in agent.result.final_answer


def test_unattended_code_exec_does_not_install_stdin_waiter(monkeypatch):
    from lib.tasks_pkg.handlers import code_exec

    captured = {}

    def fake_run(name, args, **kwargs):
        captured.update(kwargs)
        return '$ echo ok\nok\n[exit code: 0]'

    monkeypatch.setattr('lib.project_mod.execute_standalone_command', fake_run)
    monkeypatch.setattr(code_exec, '_finalize_tool_round', lambda *a, **k: None)
    task = {'id': 'parent', '_unattended': True, '_suppressEvents': True}

    code_exec._handle_code_exec(
        task, {}, 'run_command', 'tc1', {'command': 'echo ok'}, 1,
        {'toolName': 'run_command'}, {}, '', False)

    assert captured['stdin_callback'] is None


def test_stall_verdict_is_propagated_before_scheduler_shutdown(monkeypatch):
    spec = SubTaskSpec(id='stuck', role='browser', objective='x')
    master = MasterOrchestrator('task', 'conv', [spec])
    master._persist_agent_snapshot = lambda **kwargs: None
    master._beacon = ProgressBeacon(stall_timeout=0.01)
    master._beacon.touch('stuck', 'tool_start')
    time.sleep(0.02)
    seen = {}

    class FakeScheduler:
        running_count = 1

        def iter_completions(self):
            return iter(())

        def shutdown(self):
            seen['stop_at_shutdown'] = master._stop_stalled

    master._scheduler = FakeScheduler()
    monkeypatch.setattr('lib.swarm.persistence.mark_session_terminated',
                        lambda key: None)

    master._start_driver()

    assert master._completion_event.wait(2)
    assert seen['stop_at_shutdown'] is True
    assert master._stop_stalled is True
    assert master._aborted is False
