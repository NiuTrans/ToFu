"""Typed non-retryable tool failures stop argument-changing agent loops."""

from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.unit


def _tool_turn(round_num: int):
    return (
        {
            'role': 'assistant',
            'content': '',
            'tool_calls': [{
                'id': f't{round_num}',
                'function': {
                    'name': 'browser_execute_js',
                    'arguments': json.dumps({
                        'tab_id': round_num,
                        'code': 'document.title',
                    }),
                },
            }],
        },
        'tool_calls',
        {'total_tokens': 2},
    )


def _terminal_error(code='browser_write_authorization_required') -> str:
    from lib.tools.result_envelope import typed_tool_error

    return typed_tool_error(
        code,
        retryable=False,
        next_action='Ask the user to grant browser access.',
        message='Browser write authorization is required.',
    ).to_model_text()


def test_typed_error_parser_is_strict_and_legacy_fails_open():
    from lib.tools.result_envelope import (
        nonretryable_tool_error_code,
        tool_result_error,
        typed_tool_error,
    )

    terminal = _terminal_error('permission_required')
    parsed = tool_result_error(terminal)
    assert parsed is not None
    assert parsed.code == 'permission_required'
    assert parsed.retryable is False
    assert nonretryable_tool_error_code(terminal) == 'permission_required'

    retryable = typed_tool_error(
        'temporary', retryable=True, next_action='Retry once.').to_model_text()
    assert tool_result_error(retryable).retryable is True
    assert nonretryable_tool_error_code(retryable) is None
    assert tool_result_error('Error: permission required') is None
    assert tool_result_error({
        'contractVersion': 'tofu.tool-result/v2',
        'status': 'error',
        'error': {'code': 'bad', 'retryable': 'false'},
    }) is None


def test_progress_ledger_uses_codes_not_changed_tool_arguments():
    from lib.agent_core.progress_ledger import ProgressLedgerV2

    ledger = ProgressLedgerV2()
    assert ledger.observe_nonretryable_failures(['permission_required'])[
        'nonretryableFailureStreak'] == 1
    assert ledger.observe_nonretryable_failures(['permission_required'])[
        'nonretryableFailureStreak'] == 2
    assert ledger.observe_nonretryable_failures(['access_denied'])[
        'nonretryableFailureStreak'] == 1
    assert ledger.observe_nonretryable_failures([])[
        'nonretryableFailureStreak'] == 0


def test_chassis_halts_three_varying_calls_with_same_terminal_result():
    from lib.agent_loop import AbortSignal, run_agent_loop

    dispatches = {'count': 0}

    def dispatch(rnd, _tools):
        dispatches['count'] += 1
        return _tool_turn(rnd)

    outcome = run_agent_loop(
        abort=AbortSignal.never(),
        round_tools=None,
        dispatch=dispatch,
        execute_tools=lambda _rnd, _calls: {
            'nonretryable_failure_signatures': [
                'browser_write_authorization_required'],
        },
        max_consecutive_no_progress_rounds=10,
        max_consecutive_nonretryable_failure_rounds=3,
    )

    assert outcome.halted is True
    assert outcome.exit_reason == 'nonretryable_tool_failure'
    assert outcome.consecutive_nonretryable_failure_rounds == 3
    assert dispatches['count'] == 3


def test_clean_or_unknown_result_resets_terminal_failure_streak():
    from lib.agent_loop import AbortSignal, run_agent_loop

    notes = iter([
        {'nonretryable_failure_signatures': ['permission_required']},
        {},
        {'nonretryable_failure_signatures': ['permission_required']},
        {'nonretryable_failure_signatures': ['permission_required']},
    ])
    turns = iter([
        _tool_turn(1),
        _tool_turn(2),
        _tool_turn(3),
        _tool_turn(4),
        ({'role': 'assistant', 'content': 'finished normally'},
         'stop', {'total_tokens': 2}),
    ])
    outcome = run_agent_loop(
        abort=AbortSignal.never(),
        round_tools=None,
        dispatch=lambda _rnd, _tools: next(turns),
        execute_tools=lambda _rnd, _calls: next(notes),
        max_consecutive_nonretryable_failure_rounds=3,
    )

    assert outcome.completed is True
    assert outcome.exit_reason == 'completed'
    assert outcome.consecutive_nonretryable_failure_rounds == 2


def test_browser_authority_failure_is_canonical_nonretryable(monkeypatch):
    from lib.browser import access
    from lib.browser import dispatch
    from lib.browser import queue
    from lib.tools.result_envelope import tool_result_error

    class _Runtime:
        def __init__(self, *, owner_user_id, client_id):
            self.owner_user_id = owner_user_id
            self.client_id = client_id

    monkeypatch.setattr(dispatch, 'BrowserToolRuntime', _Runtime)
    monkeypatch.setattr(
        queue,
        'get_connected_clients',
        lambda **_kwargs: [{'client_id': 'browser-1', 'last_poll': 1}],
    )

    def reject(*_args, **_kwargs):
        raise access.BrowserWriteAuthorizationRequired(
            'example.com', client_id='browser-1')

    monkeypatch.setattr(access, 'browser_tool_access', reject)
    result = dispatch.execute_browser_tool(
        'browser_execute_js',
        {'tab_id': 17, 'code': 'document.title'},
        owner_user_id='7',
        client_id='browser-1',
    )

    error = tool_result_error(result)
    assert error is not None
    assert error.code == 'browser_write_authorization_required'
    assert error.retryable is False
    assert 'grant browser access' in error.next_action
    assert 'tab #17 on example.com' in error.message
    assert 'arbitrary JavaScript can mutate the page' in error.message
    assert 'browser_list_tabs' in error.next_action
    assert 'browser_research_page' in error.next_action


def test_origin_relative_browser_js_requires_explicit_tab_before_resolution():
    from lib.browser import dispatch
    from lib.tools.result_envelope import tool_result_error

    result = dispatch.execute_browser_tool(
        'browser_execute_js',
        {'code': "fetch('/api/items').then(response => response.json())"},
        owner_user_id='7',
    )

    error = tool_result_error(result)
    assert error is not None
    assert error.code == 'browser_explicit_tab_required'
    assert error.retryable is True
    assert 'will not guess which page origin' in error.message
    assert 'browser_list_tabs' in error.next_action
    assert 'explicit tab_id' in error.next_action


@pytest.mark.parametrize('fn_name', [
    'browser_execute_js',
    'browser_read_page',
    'browser_devtools',
    'browser_get_cookies',
    'browser_list_tabs',
])
def test_browser_handler_marks_typed_error_as_failed(monkeypatch, fn_name):
    from lib.browser import dispatch
    from lib.tasks_pkg.handlers import browser

    terminal = _terminal_error()
    captured = {}
    monkeypatch.setattr(
        dispatch, 'execute_browser_tool', lambda *_args, **_kwargs: terminal)

    def finalize(_task, _rn, round_entry, results, **kwargs):
        captured['status'] = kwargs.get('status')
        captured['badge'] = results[0]['badge']
        round_entry['status'] = captured['status']

    monkeypatch.setattr(browser, '_finalize_tool_round', finalize)
    round_entry = {'query': fn_name}
    _tc_id, content, is_read = browser._handle_browser_tool(
        {'_userId': '7', 'lastUserQuery': ''},
        {},
        fn_name,
        't1',
        {'tab_id': 1, 'code': 'document.title'},
        1,
        round_entry,
        {},
        '',
        False,
    )

    assert content == terminal
    assert is_read is False
    assert captured == {'status': 'error', 'badge': 'error'}


def test_swarm_production_wiring_stops_after_three_failure_rounds():
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubAgentStatus, SubTaskSpec

    dispatches = {'count': 0}

    def dispatch(_body, **_kwargs):
        dispatches['count'] += 1
        if dispatches['count'] <= 3:
            return _tool_turn(dispatches['count'])
        return (
            {'role': 'assistant', 'content': (
                'Browser access is unavailable until the user grants it.')},
            'stop',
            {'total_tokens': 2},
        )

    agent = SubAgent(
        SubTaskSpec(role='researcher', objective='inspect the active page'),
        parent_task={},
        all_tools=[],
        model='terminal-breaker-test',
        thinking_enabled=False,
        build_body_fn=lambda **kwargs: dict(kwargs),
        dispatch_stream_fn=dispatch,
    )
    terminal = _terminal_error()

    def execute(tool_calls, _round_num):
        for call in tool_calls:
            agent.messages.append({
                'role': 'tool',
                'tool_call_id': call['id'],
                'content': terminal,
            })
        return {'nonretryable_failure_signatures': [
            'browser_write_authorization_required']}

    agent._execute_tool_calls = execute
    agent._run_loop(time.time())

    assert dispatches['count'] == 4  # three failed rounds + one tool-less wrap-up
    assert agent.result.rounds_used == 3
    assert agent.result.status == SubAgentStatus.COMPLETED.value
    assert 'Browser access is unavailable' in agent.result.final_answer


def test_swarm_round_note_requires_every_tool_to_be_terminal():
    from lib.swarm.agent import SubAgent, _nonretryable_failure_signatures

    assert _nonretryable_failure_signatures([
        _terminal_error('access_denied'),
        _terminal_error('permission_required'),
    ]) == ['access_denied', 'permission_required']
    assert _nonretryable_failure_signatures([
        _terminal_error('access_denied'),
        'successful legacy result',
    ]) == []

    agent = SubAgent.__new__(SubAgent)
    agent.messages = []
    agent._execute_single_tool = lambda _call, _round: _terminal_error(
        'permission_required')
    note = SubAgent._execute_tool_calls(
        agent,
        [{'id': 't1', 'function': {'name': 'browser_execute_js'}}],
        round_num=1,
    )
    assert note == {
        'nonretryable_failure_signatures': ['permission_required']}
    assert agent.messages[-1]['role'] == 'tool'


def test_swarm_truncation_preserves_bounded_typed_error_contract():
    from lib.swarm.agent import SubAgent
    from lib.tools.result_envelope import tool_result_error

    agent = SubAgent.__new__(SubAgent)
    agent.tool_result_max_chars = 16
    terminal = _terminal_error()
    preserved = SubAgent._truncate_tool_result(agent, terminal)

    assert preserved == terminal
    assert tool_result_error(preserved) is not None


def test_swarm_tool_timeline_renders_typed_error_as_failed():
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubTaskSpec

    tool = {
        'type': 'function',
        'function': {
            'name': 'web_search',
            'description': 'Search',
            'parameters': {
                'type': 'object',
                'properties': {'query': {'type': 'string'}},
            },
        },
    }
    agent = SubAgent(
        SubTaskSpec(role='researcher', objective='search'),
        parent_task={},
        all_tools=[tool],
        model='timeline-test',
        thinking_enabled=False,
    )
    events = []
    agent._touch_progress = lambda _note='': None
    agent._emit_event = lambda event_type, message, **fields: events.append(
        (event_type, message, fields))
    agent._dispatch_tool = lambda *_args, **_kwargs: _terminal_error(
        'permission_required')

    result = agent._execute_single_tool({
        'id': 't1',
        'function': {
            'name': 'web_search',
            'arguments': '{"query":"x"}',
        },
    }, round_num=1)

    assert result == _terminal_error('permission_required')
    assert events[-1][2]['callStatus'] == 'failed'
    assert events[-1][2]['error'] == 'Browser write authorization is required.'
