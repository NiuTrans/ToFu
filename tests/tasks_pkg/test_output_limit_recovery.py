"""Output-limit stops keep the cut response and resume from its own analysis."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.stream_handler import api
from lib.tasks_pkg.stream_handler import _terminal_recovery as recovery

pytestmark = pytest.mark.unit


@pytest.fixture
def phases(monkeypatch):
    events = []
    monkeypatch.setattr(recovery, 'emit_phase', lambda *args, **kw: events.append(kw))
    return events


def analyse(task, messages, message, finish, round_num):
    return api.analyse_stream_result(
        message, finish, task, 'limit-test', 'glm-5.3-m17', round_num, 0, messages,
        usage={})


def test_cut_reasoning_is_replayed_with_its_provider_sidecars(phases):
    """The next request must carry the cut response, not a retry of the old request."""
    task = {'id': 'limit-test', 'aborted': False, 'config': {'thinkingDepth': 'max'}}
    config = deepcopy(task['config'])
    messages = [{'role': 'user', 'content': 'Build the circuit generator.'}]
    cut = {'content': '', 'reasoning_content': 'Gate layout: isqrt, then Fibonacci...',
           '_responses_items': [{'type': 'reasoning', 'id': 'rs_1'}],
           '_anthropic_content_blocks': [{'type': 'thinking', 'thinking': 'Gate layout'}]}
    result = analyse(task, messages, cut, 'length', 2)
    assert result['action'] == 'continue'
    row = messages[1]
    assert row['role'] == 'assistant'
    assert row['reasoning_content'] == cut['reasoning_content']
    assert row['_responses_items'] == cut['_responses_items']
    assert row['_anthropic_content_blocks'] == cut['_anthropic_content_blocks']
    assert not row.get('tool_calls')
    assert messages[2]['role'] == 'user'
    assert task['config'] == config


def test_cut_tool_call_arguments_are_never_repaired_into_an_execution(phases):
    """A response cut mid-arguments cannot run its tool: repair would invent the rest."""
    task = {'id': 'limit-test', 'aborted': False}
    messages = [{'role': 'user', 'content': 'Write the file.'}]
    call = {'id': 'call-1', 'type': 'function',
            'function': {'name': 'write_file',
                         'arguments': '{"path": "/app/ars.R", "content": "# first half'}}
    result = analyse(task, messages,
                     {'content': 'Writing it now', 'reasoning_content': 'design',
                      'tool_calls': [call]}, 'length', 4)
    assert result['action'] == 'continue'
    assert not task.get('error')
    assert not messages[1].get('_responses_items')
    assert not messages[1].get('_anthropic_content_blocks')
    row = messages[1]
    assert row['role'] == 'assistant' and not row.get('tool_calls')
    assert row['reasoning_content'] == 'design'
    notice = messages[2]['content']
    assert 'write_file' in notice and 'no tool call from it ran' in notice
    assert phases[-1]['bucket'] == 'output_limit'


def test_sidecar_only_tool_items_are_never_replayed(phases):
    task = {'id': 'limit-test', 'aborted': False}
    messages = []
    result = analyse(task, messages, {
        'content': '', 'reasoning_content': 'design',
        '_responses_items': [{'type': 'reasoning', 'id': 'r1'},
                             {'type': 'function_call', 'id': 'cut'}],
        '_anthropic_content_blocks': [{'type': 'thinking', 'thinking': 'design'},
                                      {'type': 'tool_use', 'id': 'cut'}],
    }, 'length', 1)
    assert result['action'] == 'continue'
    assert messages[0]['_responses_items'] == [{'type': 'reasoning', 'id': 'r1'}]
    assert messages[0]['_anthropic_content_blocks'] == [
        {'type': 'thinking', 'thinking': 'design'}]
    assert 'not executed or replayed' in messages[1]['content']


def test_responses_replay_retains_state_and_retires_cut_call_order(phases):
    from lib.llm.responses_outbound import openai_body_to_responses
    from lib.llm.responses_replay import capture_replay_order
    from lib.llm.responses_state_origin import (
        responses_state_origins, stamp_state_origin)

    reasoning = {'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'opaque'}
    commentary = {'type': 'message', 'role': 'assistant', 'phase': 'commentary',
                  'content': [{'type': 'output_text', 'text': 'Working.'}]}
    call = {'id': 'call-1', 'type': 'function',
            'function': {'name': 'write_file', 'arguments': '{}'}}
    message = {'role': 'assistant', 'content': 'Working.', 'tool_calls': [call]}
    items = capture_replay_order(
        [reasoning, dict(call, type='function_call', name='write_file'), commentary],
        message, [reasoning, commentary])
    message['_responses_items'] = items
    assert stamp_state_origin(message, 'deployment-1')

    task = {'id': 'limit-test', 'aborted': False}
    messages = []
    assert analyse(task, messages, message, 'length', 1)['action'] == 'continue'
    assert not messages[0].get('tool_calls')
    assert {item['type'] for item in messages[0]['_responses_items']} == {
        'reasoning', 'message', 'tofu_state_origin'}
    assert responses_state_origins(messages) == {'deployment-1'}
    body, _ = openai_body_to_responses(
        {'model': 'gpt-5.6', 'messages': messages, 'tools': []}, profile='codex')
    assert [item['type'] for item in body['input']] == [
        'reasoning', 'message', 'message']
    assert not any(item['type'] == 'function_call' for item in body['input'])


def test_cut_question_does_not_become_a_human_wait(phases):
    task = {'id': 'limit-test', 'aborted': False}
    messages = []
    result = analyse(task, messages,
                     {'content': 'Which of these unfinished options?', 'reasoning_content': 'design'},
                     'length', 1)
    assert result['action'] == 'continue'
    assert messages[-1]['content'].startswith('[SYSTEM: OUTPUT LIMIT RECOVERY]')


def test_cut_tool_call_budget_exhaustion_never_dispatches(phases):
    task = {'id': 'limit-test', 'aborted': False}
    call = {'id': 'call-1', 'type': 'function',
            'function': {'name': 'write_file', 'arguments': '{"path": "a.py"'}}
    for round_num in (1, 2):
        result = analyse(task, [], {'tool_calls': [call], 'reasoning_content': 'design'},
                         'length', round_num)
        assert result['action'] == 'continue'
    result = analyse(task, [], {'tool_calls': [call], 'reasoning_content': 'design'},
                     'length', 3)
    assert result['action'] == 'break'
    assert result['last_finish_reason'] == 'abnormal_stop'
    assert task['error']['kind'] == 'abnormal_stop'
    assert task['_suppress_whole_turn_retry_to_preserve_partial'] is True


def test_complete_tool_batch_is_not_disturbed_by_the_limit(phases):
    task = {'id': 'limit-test', 'aborted': False}
    call = {'id': 'call-1', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': '{"path": "sim.c"}'}}
    result = analyse(task, [], {'content': '', 'tool_calls': [call]}, 'tool_calls', 3)
    assert result['action'] == 'proceed'
    assert not phases


def test_recovery_does_not_turn_thinking_off_for_the_next_round(phases):
    """GLM-5.3 still spent 65k tokens at reasoning_effort=low; changing the turn's
    reasoning settings is not the recovery and drops product behavior."""
    task = {'id': 'limit-test', 'aborted': False}
    analyse(task, [], {'content': '', 'reasoning_content': 'analysis'}, 'length', 1)
    assert not task.get('_reasoning_blowout_degrade_next')
