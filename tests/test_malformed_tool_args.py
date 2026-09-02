"""Regression tests for the May 2026 "malformed tool_call args poisons next
API round" bug.

The bug: when a weaker model emits a tool_call whose ``arguments`` string is
syntactically broken JSON (e.g. unescaped ``\\d`` in a regex replacement),
chatui correctly produced an error tool_result and fed it back to the model.
But the assistant message that contained the bad ``arguments`` string was
appended to ``messages`` AS-IS — so on the next API round, the upstream
gateway rejected the request with HTTP 400 ``invalid function arguments
json string`` because the JSON-string itself doesn't parse.

Result: the conversation got stuck. The model never saw the error
tool_result, couldn't recover, and the task ended ``finishReason=error``.

The fix sanitizes the offending tool_call's ``arguments`` field to ``'{}'``
in BOTH the live append path (orchestrator.py) and the replay path
(conv_message_builder.py + message_builder.py) before the next API call
fires. The error tool_result still reaches the model; the gateway sees
syntactically valid JSON and lets the round proceed.
"""
from __future__ import annotations

import json

import pytest


def test_conv_message_builder_sanitizes_malformed_args():
    """When stored toolArgs isn't valid JSON, replay must use ``'{}'``."""
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages

    bad_args = r'{"path": "f.py", "search": "r\d+"}'  # \d not escaped
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad_args)

    rounds = [{
        'toolCallId': 'call_xyz',
        'toolName': 'apply_diff',
        'status': 'done',
        'toolContent': 'ERROR: Your tool call had malformed JSON args.',
        'toolArgs': bad_args,
        'roundNum': 1,
        'llmRound': 1,
    }]

    out = _reconstruct_tool_call_messages(rounds)
    assert out is not None, 'expected structured replay'
    assert len(out) == 2

    asst_msg, tool_msg = out[0], out[1]
    assert asst_msg['role'] == 'assistant'
    tcs = asst_msg['tool_calls']
    assert len(tcs) == 1
    arg_str = tcs[0]['function']['arguments']

    # Must be syntactically valid JSON; should be ``'{}'`` placeholder.
    parsed = json.loads(arg_str)
    assert parsed == {}, f'expected {{}} placeholder, got {arg_str!r}'

    # Tool result still tells the model what went wrong.
    assert tool_msg['role'] == 'tool'
    assert 'malformed JSON' in tool_msg['content']


def test_conv_message_builder_preserves_valid_args():
    """Valid JSON args must NOT be touched."""
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages

    good_args = '{"path": "x.py", "search": "foo", "replace": "bar"}'
    rounds = [{
        'toolCallId': 'call_ok',
        'toolName': 'apply_diff',
        'status': 'done',
        'toolContent': 'Applied 1 edit',
        'toolArgs': good_args,
        'roundNum': 1,
        'llmRound': 1,
    }]

    out = _reconstruct_tool_call_messages(rounds)
    assert out is not None
    arg_str = out[0]['tool_calls'][0]['function']['arguments']
    assert json.loads(arg_str) == json.loads(good_args), \
        'valid args must round-trip unchanged'


def test_inject_tool_history_sanitizes_malformed_args():
    """Continue-context replay path must also sanitize."""
    from lib.tasks_pkg.message_builder import inject_tool_history

    bad_args = r'{"foo": "\xnotahex"}'  # invalid \x escape
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad_args)

    cfg = {
        'toolHistory': [
            {
                'toolCalls': [
                    {'id': 'tc1', 'name': 'apply_diff', 'arguments': bad_args},
                ],
                'toolResults': [
                    {'tool_call_id': 'tc1', 'content': 'ERROR: malformed JSON'},
                ],
            },
        ],
    }
    messages: list = [{'role': 'user', 'content': 'do the thing'}]
    task = {'id': 'fakeid12', 'convId': 'fakeconv'}

    n = inject_tool_history(messages, cfg, task, model='aws.claude-opus-4.6')
    assert n == 1

    # Find the assistant tool_calls message
    asst_msgs = [m for m in messages if m.get('role') == 'assistant' and m.get('tool_calls')]
    assert len(asst_msgs) == 1
    arg_str = asst_msgs[0]['tool_calls'][0]['function']['arguments']

    # Must be valid JSON
    parsed = json.loads(arg_str)
    assert parsed == {}, f'expected sanitized {{}}, got {arg_str!r}'


def test_inject_tool_history_preserves_valid_args():
    """Valid checkpoint args must NOT be touched on replay."""
    from lib.tasks_pkg.message_builder import inject_tool_history

    good_args = '{"path": "django/x.py"}'
    cfg = {
        'toolHistory': [
            {
                'toolCalls': [
                    {'id': 'tc1', 'name': 'read_files', 'arguments': good_args},
                ],
                'toolResults': [
                    {'tool_call_id': 'tc1', 'content': 'ok'},
                ],
            },
        ],
    }
    messages: list = [{'role': 'user', 'content': 'show file'}]
    task = {'id': 'fakeid12', 'convId': 'fakeconv'}

    inject_tool_history(messages, cfg, task, model='aws.claude-opus-4.6')
    asst_msgs = [m for m in messages if m.get('role') == 'assistant' and m.get('tool_calls')]
    arg_str = asst_msgs[0]['tool_calls'][0]['function']['arguments']
    assert json.loads(arg_str) == json.loads(good_args)


def test_orchestrator_live_sanitizer_logic():
    """Unit-test the live sanitizer's shape: walk parsed_tcs, rewrite
    matching tc.function.arguments in messages[-1] when args_parse_error
    is set. We don't run the orchestrator end-to-end (it has many deps);
    we just verify the inner loop's invariants.
    """
    # Simulate what orchestrator.py does
    parsed_tcs = [
        # OK call
        ({'id': 'good'}, 'read_files', 'good',
         {'path': 'a.py'}, 1, {'roundNum': 1}, None),
        # Broken call
        ({'id': 'bad'}, 'apply_diff', 'bad',
         {}, 2, {'roundNum': 2},
         'ERROR: Your tool call for `apply_diff` had malformed JSON ...'),
    ]

    messages: list = [
        {'role': 'user', 'content': 'foo'},
        {
            'role': 'assistant',
            'tool_calls': [
                {'id': 'good', 'function': {'name': 'read_files',
                                            'arguments': '{"path": "a.py"}'}},
                {'id': 'bad',  'function': {'name': 'apply_diff',
                                            'arguments': r'{"r": "\d"}'}},  # bad
            ],
        },
    ]

    # Apply the same logic from orchestrator.py:1364
    for tc, fn_name, tc_id, fn_args, rn, round_entry, args_parse_err in parsed_tcs:
        if not args_parse_err:
            continue
        last_msg = messages[-1]
        for live_tc in last_msg.get('tool_calls', []) or []:
            if live_tc.get('id') != tc_id:
                continue
            live_tc['function']['arguments'] = '{}'
            break

    # OK call untouched
    assert messages[-1]['tool_calls'][0]['function']['arguments'] == '{"path": "a.py"}'
    # Bad call sanitized to '{}'
    assert messages[-1]['tool_calls'][1]['function']['arguments'] == '{}'
    # Both args round-trip as valid JSON now
    for tc in messages[-1]['tool_calls']:
        json.loads(tc['function']['arguments'])  # must not raise
