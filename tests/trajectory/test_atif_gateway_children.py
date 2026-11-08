"""ATIF exports the tool calls a gateway call ran on the model's behalf.

``execute_program`` and ``execute_tools`` run child tool calls that never
appear as assistant ``tool_calls`` in the working transcript; only the
parent call and its aggregate receipt do. The ATIF export therefore missed
every child call (8 of 89 TB2.1 round-1 Tofu trajectories), so the
contamination audit could not see, e.g., a ``fetch_url`` issued from a
program. The rows below mirror a real headless run (tofu-agent ``run
--trajectory tofu-native`` against a scripted endpoint): the program child
carries ``parentToolCallId``; batch children carry only their ``llmRound``.
"""
import json
from copy import deepcopy

import pytest

from lib.trajectory import flatten

pytestmark = pytest.mark.unit

_PROGRAM_CODE = ("const r = await tools.invoke('run_command', {command: 'echo child-one'});\n"
                 "return r;")
_BATCH_ARGS = ('{"calls": [{"name": "run_command", "arguments": {"command": "echo child-two"}}, '
               '{"name": "run_command", "arguments": {"command": "echo child-three"}}]}')


def _assistant(text, call_id, name, arguments):
    return {'role': 'assistant', 'content': text, 'tool_calls': [{
        'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}]}


def _row(round_num, llm_round, call_id, name, args, content, source, **extra):
    return {'roundNum': round_num, 'llmRound': llm_round, 'toolCallId': call_id,
            'toolName': name, 'toolArgs': args, 'toolContent': content,
            'source': source, 'status': 'done', **extra}


def _gateway_task():
    messages = [
        {'role': 'user', 'content': 'Exercise the tools.'},
        _assistant('Run a child through a program.', 'call_0', 'execute_program',
                   json.dumps({'code': _PROGRAM_CODE})),
        {'role': 'tool', 'tool_call_id': 'call_0', 'content': '"$ echo child-one\\nchild-one"'},
        _assistant('Two children through execute_tools.', 'call_1', 'execute_tools', _BATCH_ARGS),
        {'role': 'tool', 'tool_call_id': 'call_1', 'content': '{"status":"ok"} ## results[0] ...'},
        _assistant('Read a file.', 'call_2', 'read_files', '{"path": "a.txt"}'),
        {'role': 'tool', 'tool_call_id': 'call_2', 'content': 'file a'},
        {'role': 'assistant', 'content': 'Done.'},
    ]
    rounds = [
        _row(1, 0, 'call_0', 'execute_program', '{"code": "..."}', '"child-one"', 'native_direct'),
        # Program presentation row: not a tool call, must never be exported as one.
        {'roundNum': 8800000, 'llmRound': 0, 'status': 'done', '_programSynthetic': True,
         '_programCallId': 'call_0', 'childCallIds': ['gw_one'], 'childToolNames': ['run_command']},
        _row(8700001, 0, 'gw_one', 'run_command', '{"command":"echo child-one"}',
             '$ echo child-one\nchild-one\n\n[exit code: 0]', 'execute_program',
             parentToolCallId='call_0'),
        _row(2, 1, 'call_1', 'execute_tools', _BATCH_ARGS, '{"status":"ok"}', 'native_direct'),
        _row(8700003, 1, 'gw_two', 'run_command', '{"command":"echo child-two"}',
             '$ echo child-two\nchild-two', 'execute_calls'),
        _row(8700004, 1, 'gw_three', 'run_command', '{"command":"echo child-three"}',
             '$ echo child-three\nchild-three', 'execute_calls'),
        _row(3, 2, 'call_2', 'read_files', '{"path": "a.txt"}', 'file a', 'native_direct'),
    ]
    return {'id': 't-gateway', 'status': 'done', 'finishReason': 'stop', 'content': 'Done.',
            'messages': messages, '_messages_include_current_run': True,
            'toolRounds': rounds, 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}


def _calls(steps):
    return [(c['tool_call_id'], c['function_name']) for s in steps for c in (s.get('tool_calls') or [])]


def test_every_executed_call_is_exported_once():
    task = _gateway_task()
    before = deepcopy(task)
    steps = flatten(task, 'atif')['trajectory']['steps']
    executed = [(r['toolCallId'], r['toolName']) for r in task['toolRounds'] if r.get('toolCallId')]
    assert sorted(_calls(steps)) == sorted(executed)
    assert len(_calls(steps)) == len(set(_calls(steps)))
    assert task == before


def test_children_follow_their_parent_with_arguments_and_receipts():
    steps = flatten(_gateway_task(), 'atif')['trajectory']['steps']
    order = [c[0] for c in _calls(steps)]
    assert order == ['call_0', 'gw_one', 'call_1', 'gw_two', 'gw_three', 'call_2']
    by_call = {c['tool_call_id']: s for s in steps for c in (s.get('tool_calls') or [])}
    child = by_call['gw_one']
    assert child['source'] == 'agent'
    assert child['tool_calls'][0]['arguments'] == {'command': 'echo child-one'}
    assert child['observation']['results'] == [{
        'source_call_id': 'gw_one', 'content': '$ echo child-one\nchild-one\n\n[exit code: 0]'}]
    assert child['extra']['parent_call_id'] == 'call_0'
    assert child['extra']['call_source'] == 'execute_program'
    # Batch children have no presentation edge; their llmRound names the
    # one gateway call of that response.
    assert by_call['gw_two']['extra']['parent_call_id'] == 'call_1'
    assert by_call['gw_three']['extra']['parent_call_id'] == 'call_1'
    assert [s['step_id'] for s in steps] == list(range(1, len(steps) + 1))


def test_ambiguous_batch_parent_is_reported_not_guessed():
    task = _gateway_task()
    # A second execute_tools call in the same response: the child's own id in
    # a parent's receipt still identifies its parent; an unidentifiable child
    # is exported with no parent rather than attributed to a guess.
    task['messages'][3]['tool_calls'].append({
        'id': 'call_1b', 'type': 'function',
        'function': {'name': 'execute_tools', 'arguments': '{"calls": []}'}})
    task['messages'].insert(5, {'role': 'tool', 'tool_call_id': 'call_1b', 'content': '{}'})
    task['toolRounds'].insert(6, _row(4, 1, 'call_1b', 'execute_tools', '{"calls": []}',
                                      '{"results":[{"call_id":"gw_four"}]}', 'native_direct'))
    task['toolRounds'].insert(7, _row(8700005, 1, 'gw_four', 'run_command', '{"command":"x"}',
                                      'x', 'execute_calls'))
    task['toolRounds'][3]['toolContent'] = '{"results":[{"call_id":"gw_two"}]}'
    steps = flatten(task, 'atif')['trajectory']['steps']
    by_call = {c['tool_call_id']: s for s in steps for c in (s.get('tool_calls') or [])}
    assert by_call['gw_two']['extra']['parent_call_id'] == 'call_1'
    assert by_call['gw_four']['extra']['parent_call_id'] == 'call_1b'
    assert by_call['gw_three']['extra']['parent_call_id'] is None
    assert by_call['gw_three']['extra']['candidate_parent_call_ids'] == ['call_1', 'call_1b']


def test_run_without_gateway_children_is_unchanged():
    task = _gateway_task()
    task['messages'] = [task['messages'][0]] + task['messages'][5:]
    task['toolRounds'] = [task['toolRounds'][-1]]
    steps = flatten(task, 'atif')['trajectory']['steps']
    assert _calls(steps) == [('call_2', 'read_files')]
    assert all('extra' not in s for s in steps)


def test_a_child_already_in_the_transcript_is_not_duplicated():
    task = _gateway_task()
    # Input-only snapshots rebuild calls from toolRounds; a child the
    # transcript already carries must not be appended a second time.
    task['messages'].insert(3, _assistant('', 'gw_one', 'run_command', '{"command":"echo child-one"}'))
    task['messages'].insert(4, {'role': 'tool', 'tool_call_id': 'gw_one', 'content': 'child-one'})
    steps = flatten(task, 'atif')['trajectory']['steps']
    assert [c for c in _calls(steps) if c[0] == 'gw_one'] == [('gw_one', 'run_command')]
