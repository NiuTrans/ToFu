"""The root/child adapter enforces evidence-based recovery through its real loop."""

from contextlib import nullcontext
import json
import threading

import pytest

from lib.tasks_pkg.child_invocation import create_child_invocation
from lib.tasks_pkg.manager import discard_task
from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop
from lib.tasks_pkg.orchestrator._tool_loop_breaker import handle_tool_loop_circuit_breaker
from tests._registered_chat_task import registered_chat_task
from tests.tasks_pkg.test_root_orchestrator_agent_loop_adapter import (
    _install_common, _install_script, _request, _state,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('child_lane', [False, True], ids=['root', 'child'])
@pytest.mark.parametrize('changing', [False, True], ids=['cycle', 'progress'])
def test_shared_loop_stops_only_after_visible_evidence_repeats_and_a_warning(
        monkeypatch, child_lane, changing):
    tools = [{'type': 'function', 'function': {'name': name}}
             for name in ('read_files', 'search_files')]
    task = {
        'id': 'observation-root', 'convId': 'observation-conv', '_userId': 71,
        '_transientRuntime': True,
        'config': {}, 'toolRounds': [], 'aborted': False, 'error': None,
        'abort_event': threading.Event(), '_tool_schema': tools,
    }
    if child_lane:
        task = create_child_invocation(
            task, model='kimi-k3', tools=tools, objective='Review and report findings.',
            event_sink=lambda event: None, abort_check=lambda: False)

    state, events = _state(), []
    request = _request(task, state)
    request.tool_list = tools
    request.messages.append({'role': 'user', 'content': 'Review and report findings.'})
    _install_common(monkeypatch, events)
    monkeypatch.setattr(root_loop, 'handle_tool_loop_circuit_breaker',
                        handle_tool_loop_circuit_breaker)
    monkeypatch.setattr(
        root_loop, 'apply_stream_decision',
        lambda *a, **k: ('proceed', k['premature_retry_count']))

    script = []
    for rn in range(8):
        name = 'search_files' if rn % 2 else 'read_files'
        args = ({'searches': [{'path': 'src', 'pattern': 'deadline'}]}
                if rn % 2 else {'reads': [{
                    'path': 'src/worker.py', 'start_line': 1, 'end_line': 5}]})
        script.append(({
            'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': f'observation-{rn}', 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)},
            }],
        }, 'tool_calls', {}))
    script.append(({'role': 'assistant', 'content': 'Review complete.'}, 'stop', {}))
    _install_script(monkeypatch, state, script, events)

    def execute(*args, round_num, **kwargs):
        message = script[round_num][0]
        call = message['tool_calls'][0]
        content = f'evidence-{round_num if changing else round_num % 2}'
        request.messages.extend([
            message,
            {'role': 'tool', 'tool_call_id': call['id'], 'content': content},
        ])
        task.setdefault('toolRounds', []).append({
            'toolName': call['function']['name'],
            'toolArgs': call['function']['arguments'],
            'toolContent': content, 'toolCallId': call['id'],
            'status': 'done', 'llmRound': round_num, 'roundNum': round_num,
        })
        return False

    monkeypatch.setattr(root_loop, 'run_tool_dispatch', execute)
    try:
        with nullcontext(task) if child_lane else registered_chat_task(task, user_id=71):
            result = root_loop.run_root_agent_loop(request)
        if changing:
            assert not result.outcome.halted
            assert not task.get('error')
            assert not task.get('_toolLoopNudges')
            assert len([event for event in events if event[0] == 'llm']) == 9
        else:
            assert result.outcome.halted
            assert result.outcome.exit_reason == 'semantic_redundant_read_loop'
            assert task['error']['kind'] == 'tool_loop'
            assert result.last_round_num == 5
            nudges = task['_toolLoopNudges']
            assert len(nudges) == 1 and nudges[0]['reason'] == 'redundant_reads'
            assert 'finish now and report your findings' in nudges[0]['prompt']
            assert 'edit_file' not in nudges[0]['prompt']
    finally:
        if child_lane:
            discard_task(task['id'])
