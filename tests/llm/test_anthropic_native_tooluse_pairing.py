"""Native replay must not orphan locally-synthesized gateway tool results.

The Anthropic adapter replays ``_anthropic_content_blocks`` verbatim, which is
the correct authority for the provider's own turn. The tool gateway expands a
model's ``execute_tools`` call into local sibling ``gw_*`` tool_calls that the
provider never emitted, so those calls exist only in the canonical
``tool_calls`` projection — not in the native sidecar. Replaying the sidecar
alone drops their ``tool_use`` blocks while their ``tool_result`` messages
survive, and a strict Anthropic-shape provider rejects the whole request
(production: conv mvupi0cariefzy, task 42fdc410, 2026-11-01, Kimi
``invalid_request_error: tool_call_id  is not found`` after a Claude→Kimi
fallback).
"""
import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic

pytestmark = pytest.mark.unit


def _gateway_turn():
    """One execute_tools call plus its locally-expanded sibling, as persisted."""
    return [
        {'role': 'user', 'content': 'read the doc'},
        {
            'role': 'assistant',
            'content': '',
            '_anthropic_content_blocks': [
                {'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'},
                {'type': 'tool_use', 'id': 'toolu_1',
                 'name': 'execute_tools',
                 'input': {'calls': [{'name': 'read_doc', 'arguments': {}}]}},
            ],
            'tool_calls': [
                {'id': 'toolu_1', 'type': 'function', 'function': {
                    'name': 'execute_tools',
                    'arguments': '{"calls":[{"name":"read_doc","arguments":{}}]}'}},
                {'id': 'gw_648cee50308a174e1d20', 'type': 'function',
                 'function': {'name': 'read_doc', 'arguments': '{}'}},
            ],
        },
        {'role': 'tool', 'tool_call_id': 'toolu_1',
         'content': '{"status":"ok"}'},
        {'role': 'tool', 'tool_call_id': 'gw_648cee50308a174e1d20',
         'content': '# Document body'},
        {'role': 'user', 'content': 'continue'},
    ]


def _tool_use_ids(wire):
    return [block['id'] for message in wire['messages']
            for block in message.get('content') or []
            if isinstance(block, dict) and block.get('type') == 'tool_use']


def _tool_result_ids(wire):
    return [block['tool_use_id'] for message in wire['messages']
            for block in message.get('content') or []
            if isinstance(block, dict) and block.get('type') == 'tool_result']


def test_native_replay_keeps_the_local_sibling_tool_use():
    wire = openai_body_to_anthropic(
        {'model': 'kimi-k3.1-pro', 'max_tokens': 64,
         'messages': _gateway_turn()})
    used = _tool_use_ids(wire)
    returned = _tool_result_ids(wire)
    # The provider's own turn is preserved verbatim, including its signature.
    native = wire['messages'][1]['content']
    assert native[0] == {'type': 'thinking', 'thinking': '',
                         'signature': 'SIG_A'}
    assert native[1]['name'] == 'execute_tools'
    # Every tool_result resolves to an emitted tool_use in the same request.
    assert set(returned) <= set(used), (
        f'orphaned tool_result ids: {set(returned) - set(used)}')
    assert 'gw_648cee50308a174e1d20' in used
    assert returned == ['toolu_1', 'gw_648cee50308a174e1d20']


def test_native_replay_does_not_duplicate_provider_tool_calls():
    wire = openai_body_to_anthropic(
        {'model': 'kimi-k3.1-pro', 'max_tokens': 64,
         'messages': _gateway_turn()})
    used = _tool_use_ids(wire)
    assert used.count('toolu_1') == 1
    assert used.count('gw_648cee50308a174e1d20') == 1


def test_verbatim_native_turn_without_local_siblings_is_unchanged():
    messages = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': '',
         '_anthropic_content_blocks': [
             {'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'},
             {'type': 'tool_use', 'id': 'toolu_1', 'name': 'read',
              'input': {'path': 'a.py'}}],
         'tool_calls': [{'id': 'toolu_1', 'type': 'function', 'function': {
             'name': 'read', 'arguments': '{"path":"a.py"}'}}]},
        {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'ok'},
    ]
    wire = openai_body_to_anthropic(
        {'model': 'claude-opus-5.5', 'max_tokens': 64, 'messages': messages})
    assert wire['messages'][1]['content'] == [
        {'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'},
        {'type': 'tool_use', 'id': 'toolu_1', 'name': 'read',
         'input': {'path': 'a.py'}},
    ]
