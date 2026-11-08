"""Every tool-call ID on the wire satisfies the destination provider's contract.

Production 2026-11-05/06: three conversations died on Anthropic HTTP 400
``messages.N.content.K.tool_use.id: String should match pattern
'^[a-zA-Z0-9_-]+$'`` and every later request of each failed the same way.

* worker-2717df5aee9a627fa19b8204 (lane 29f784e8) and
  worker-ff4248a52bf27798bb699072 (lane b6c30125) resumed after a restart and
  replayed execute_tools children whose recycled ``call_id`` the dispatcher had
  reminted as ``c1#r104#44079377`` / ``a#r55#f464f4d3``.
* coordinator-17715d532c339073d864765b334def15 replayed kimi-k3's own
  ``watchdog_snapshot:90`` after a fallback from gpt-6.1-sol to Claude.

Persisted history keeps the IDs it was given; each outbound adapter spells
them for its provider. Valid IDs stay byte-identical (prompt-cache prefix),
the spelling is deterministic, a call and its result always agree, and no two
occurrences of one request share a wire ID.
"""
import copy
import json
import re

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm.openai_outbound import openai_body_to_chat
from lib.llm.responses_outbound import openai_body_to_responses

pytestmark = pytest.mark.unit

ANTHROPIC_ID = re.compile(r'^[a-zA-Z0-9_-]+$')
PORTABLE_ID = re.compile(r'^[a-zA-Z0-9_-]{1,40}$')
MISTRAL_ID = re.compile(r'^[a-zA-Z0-9]{9}$')

# Exact shapes retained by the poisoned conversations.
TOFU_REMINT = 'c1#r104#44079377'
TOFU_REMINT_SIBLING = 'c2#r104#d9824666'
KIMI_NATIVE = 'watchdog_snapshot:90'
CLAUDE_NATIVE = 'toolu_01Uo5gFMinCG2PzxRtXBzH17'
# The Codex backend returns call_ids longer than its own 64-character input
# limit (69 observed); positional ids built on long tool names exceed 40.
LONG_NATIVE = 'call_' + 'Ab9' * 21 + 'xyz1'


def _call(call_id, name='consider_project_message', arguments='{}'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}


def _result(call_id, content='ok'):
    return {'role': 'tool', 'tool_call_id': call_id, 'content': content}


def _history(*, native_sidecar=True):
    """29f784e8 R104 as replayed: provider blocks + expanded gateway children."""
    gateway_turn = {
        'role': 'assistant', 'content': '',
        'tool_calls': [
            _call(CLAUDE_NATIVE, 'execute_tools', '{"calls":[]}'),
            _call(TOFU_REMINT), _call(TOFU_REMINT_SIBLING),
        ],
    }
    if native_sidecar:
        gateway_turn['_anthropic_content_blocks'] = [
            {'type': 'thinking', 'thinking': 'plan', 'signature': 'SIG'},
            {'type': 'tool_use', 'id': CLAUDE_NATIVE, 'name': 'execute_tools',
             'input': {'calls': []}},
        ]
    return [
        {'role': 'system', 'content': 'worker'},
        {'role': 'user', 'content': 'go'},
        gateway_turn,
        _result(CLAUDE_NATIVE, '{"status":"ok"}'),
        _result(TOFU_REMINT, 'first child'),
        _result(TOFU_REMINT_SIBLING, 'second child'),
        {'role': 'assistant', 'content': 'snapshot',
         'tool_calls': [_call(KIMI_NATIVE, 'watchdog_snapshot')]},
        _result(KIMI_NATIVE, 'snapshot body'),
        {'role': 'assistant', 'content': '',
         'tool_calls': [_call(LONG_NATIVE, 'read_files')]},
        _result(LONG_NATIVE, 'file body'),
        {'role': 'user', 'content': 'continue'},
    ]


def _anthropic_pairs(wire):
    uses, results = [], []
    for message in wire['messages']:
        for block in message['content'] if isinstance(message['content'], list) else ():
            if block.get('type') == 'tool_use':
                uses.append(block['id'])
            elif block.get('type') == 'tool_result':
                results.append((block['tool_use_id'], block['content']))
    return uses, results


def _anthropic(history, model='claude-opus-5.5'):
    return openai_body_to_anthropic(
        {'model': model, 'max_tokens': 64, 'messages': history})


def test_anthropic_wire_satisfies_the_tool_use_id_pattern_and_keeps_pairs():
    wire = _anthropic(_history())
    uses, results = _anthropic_pairs(wire)

    assert uses and all(ANTHROPIC_ID.match(value) for value in uses), uses
    assert [result_id for result_id, _ in results] == uses
    assert len(set(uses)) == len(uses)
    # Already-valid provider IDs are byte-identical (prompt-cache prefix).
    assert uses[0] == CLAUDE_NATIVE
    # Each result still answers the call it answered in the source history.
    by_content = dict((content, result_id) for result_id, content in results)
    assert by_content['first child'] == uses[1]
    assert by_content['second child'] == uses[2]
    assert by_content['snapshot body'] == uses[3]


def test_anthropic_maps_invalid_ids_inside_native_provider_blocks():
    """Kimi's Messages face returns colon IDs inside its own content array."""
    history = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': '',
         '_anthropic_content_blocks': [
             {'type': 'thinking', 'thinking': 't', 'signature': 'SIG'},
             {'type': 'tool_use', 'id': KIMI_NATIVE,
              'name': 'watchdog_snapshot', 'input': {}}],
         'tool_calls': [_call(KIMI_NATIVE, 'watchdog_snapshot')]},
        _result(KIMI_NATIVE, 'snapshot body'),
        {'role': 'user', 'content': 'continue'},
    ]
    uses, results = _anthropic_pairs(_anthropic(history))

    assert len(uses) == 1 and ANTHROPIC_ID.match(uses[0])
    assert results == [(uses[0], 'snapshot body')]


def test_wire_spelling_is_deterministic_and_prefix_stable():
    first = _anthropic(_history())
    assert _anthropic(copy.deepcopy(_history())) == first

    longer = _history()[:-1] + [
        {'role': 'assistant', 'content': '',
         'tool_calls': [_call('m1#r109#ad5a1c6d', 'get_project_messages')]},
        _result('m1#r109#ad5a1c6d', 'mail'),
        {'role': 'user', 'content': 'continue'},
    ]
    grown = _anthropic(longer)
    prefix = len(first['messages']) - 1
    assert grown['messages'][:prefix] == first['messages'][:prefix]


def test_wire_ids_never_collide_within_one_request():
    """Distinct IDs that sanitize alike stay distinct, even beside a real one."""
    ids = ['a:1', 'a.1', 'a_1', 'a#1']
    history = [{'role': 'user', 'content': 'go'},
               {'role': 'assistant', 'content': '',
                'tool_calls': [_call(value) for value in ids]},
               *[_result(value, f'result {value}') for value in ids],
               {'role': 'user', 'content': 'continue'}]
    uses, results = _anthropic_pairs(_anthropic(history))

    assert len(set(uses)) == len(ids)
    assert all(ANTHROPIC_ID.match(value) for value in uses)
    assert 'a_1' in uses
    assert [content for _, content in results] == [f'result {v}' for v in ids]
    assert [result_id for result_id, _ in results] == uses


def test_valid_histories_reach_every_wire_unchanged():
    history = [{'role': 'user', 'content': 'go'},
               {'role': 'assistant', 'content': '',
                'tool_calls': [_call(CLAUDE_NATIVE), _call('call_abc123')]},
               _result(CLAUDE_NATIVE), _result('call_abc123'),
               {'role': 'user', 'content': 'continue'}]
    uses, _results = _anthropic_pairs(_anthropic(copy.deepcopy(history)))
    assert uses == [CLAUDE_NATIVE, 'call_abc123']

    chat = openai_body_to_chat({'model': 'kimi-k2.6', 'messages': copy.deepcopy(history)})
    assert chat['messages'] == history


def test_responses_call_ids_fit_the_64_character_limit():
    body = {'model': 'gpt-6.1-sol', 'max_tokens': 64,
            'messages': _history(native_sidecar=False)}
    wire, _reverse = openai_body_to_responses(body, stream=False)
    calls = [item['call_id'] for item in wire['input']
             if item.get('type') == 'function_call']
    outputs = [item['call_id'] for item in wire['input']
               if item.get('type') == 'function_call_output']

    assert outputs == calls
    assert all(0 < len(value) <= 64 for value in calls), calls
    assert len(set(calls)) == len(calls)
    # Responses constrains length only: shorter IDs keep their spelling.
    assert TOFU_REMINT in calls and KIMI_NATIVE in calls
    assert LONG_NATIVE not in calls


def test_chat_ids_fit_40_characters_and_keep_native_spellings():
    wire = openai_body_to_chat({'model': 'kimi-k2.6', 'messages': _history(native_sidecar=False)})
    calls = [tc['id'] for m in wire['messages'] for tc in m.get('tool_calls') or ()]
    results = [m['tool_call_id'] for m in wire['messages'] if m['role'] == 'tool']

    assert results == calls
    assert all(0 < len(value) <= 40 for value in calls), calls
    assert KIMI_NATIVE in calls and TOFU_REMINT in calls
    assert LONG_NATIVE not in calls


def test_claude_behind_a_chat_gateway_gets_the_anthropic_pattern():
    wire = openai_body_to_chat({'model': 'claude-opus-5.5', 'messages': _history(native_sidecar=False)})
    calls = [tc['id'] for m in wire['messages'] for tc in m.get('tool_calls') or ()]
    results = [m['tool_call_id'] for m in wire['messages'] if m['role'] == 'tool']

    assert results == calls
    assert all(PORTABLE_ID.match(value) for value in calls), calls
    assert calls[0] == CLAUDE_NATIVE


def test_mistral_ids_are_nine_alphanumerics():
    history = _history(native_sidecar=False)
    history[-1:-1] = [
        {'role': 'assistant', 'content': '',
         'tool_calls': [_call('D681PevKs', 'read_files')]},
        _result('D681PevKs', 'native mistral'),
    ]
    wire = openai_body_to_chat({'model': 'mistral-large-4-0', 'messages': history})
    calls = [tc['id'] for m in wire['messages'] for tc in m.get('tool_calls') or ()]
    results = [m['tool_call_id'] for m in wire['messages'] if m['role'] == 'tool']

    assert results == calls
    assert all(MISTRAL_ID.match(value) for value in calls), calls
    assert len(set(calls)) == len(calls)
    assert 'D681PevKs' in calls


@pytest.mark.parametrize('convert', [
    lambda body: openai_body_to_anthropic(body),
    lambda body: openai_body_to_chat({**body, 'model': 'mistral-large-4-0'}),
    lambda body: openai_body_to_responses({**body, 'model': 'gpt-6.1-sol'}, stream=False),
])
def test_adapters_leave_the_canonical_history_untouched(convert):
    body = {'model': 'claude-opus-5.5', 'max_tokens': 64,
            'messages': _history(native_sidecar=False)}
    before = copy.deepcopy(body)
    convert(body)
    assert body == before


def test_reminted_call_ids_are_portable_at_the_source():
    from lib.tasks_pkg.tool_dispatch._pipeline import _mint_fresh_call_id

    task = {'id': 'task', 'toolRounds': []}
    for preferred in ('c1', 'functions.read_files:3', KIMI_NATIVE, 'x' * 200, ''):
        minted = _mint_fresh_call_id(task, [], preferred, 104, used_ids=set())
        assert PORTABLE_ID.match(minted), minted
    assert _mint_fresh_call_id(task, [], 'c1', 104, used_ids=set()).startswith('c1_r104_')


def test_resumed_29f784e8_rounds_replay_valid_ids_through_the_real_builder():
    """Checkpoint replay of the exact stored row shapes, built for Claude."""
    from lib.llm.body import build_body
    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages,
    )

    scope = {'attemptId': 'fe6e6f23-cac9-4717-9541-5b54f81c6024',
             'taskId': 'project-worker:29f784e8', 'llmRound': 104}
    container = {
        **scope, 'roundNum': 110, 'source': 'native_direct', 'status': 'done',
        '_hiddenToolAdapter': True, 'toolName': 'execute_tools',
        'toolCallId': 'toolu_01Uo5gFMinCG2PzxRtXBzH17',
        'toolArgs': json.dumps({'calls': [{'call_id': 'c1'}, {'call_id': 'c2'}]}),
        # Text projection: the replay scanner cannot prove child coverage.
        'toolContent': '{"status":"ok","warnings":[]}  ## results[0] '
                       '{"call_id":"c1#r104#44079377"} output',
        'thinking': 'plan', 'thinkingSignature': 'SIG',
        '_anthropicContentBlocks': [
            {'type': 'thinking', 'thinking': 'plan', 'signature': 'SIG'},
            {'type': 'tool_use', 'id': 'toolu_01Uo5gFMinCG2PzxRtXBzH17',
             'name': 'execute_tools', 'input': {'calls': []}}],
    }
    children = [
        {**scope, 'roundNum': 8700007 + index, 'source': 'execute_calls',
         'status': 'done', 'toolName': 'consider_project_message',
         'toolCallId': call_id, 'toolArgs': '{"decision":"accepted"}',
         'toolContent': f'{{"message":"child {index}"}}'}
        for index, call_id in enumerate(
            ('c1#r104#44079377', 'c2#r104#d9824666', 'c3#r104#a7c716db'))
    ]
    replay = _reconstruct_tool_call_messages([container, *children])
    body = build_body(
        'claude-opus-5.5',
        [{'role': 'system', 'content': 'worker'},
         {'role': 'user', 'content': 'go'}, *replay,
         {'role': 'user', 'content': 'continue'}],
        max_tokens=64, stream=False, thinking_enabled=False)
    uses, results = _anthropic_pairs(openai_body_to_anthropic(body))

    assert len(uses) == 4
    assert all(ANTHROPIC_ID.match(value) for value in uses), uses
    assert [result_id for result_id, _ in results] == uses
