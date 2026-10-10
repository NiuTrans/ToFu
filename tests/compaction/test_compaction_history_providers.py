"""Retained-history meaning and complete execution batches at real wire seams.

Only request shaping runs: no transport, storage, or model invocation.
"""
from copy import deepcopy
import json

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm.body._build import build_body
from lib.llm.responses_outbound import openai_body_to_responses
from lib.tasks_pkg.compaction import _history as history
from lib.tasks_pkg.conv_message_builder._transform import _transform_messages

pytestmark = pytest.mark.unit

OPENING = 'Has tofu-db launched, and is it easy for users to install?'
EARLIER = 'Work on release readiness within the existing repository.'
LIVE_REQUESTS = [
    'Continue the release-readiness work.',
    'Keep pushing forward; do not ask me for approval again.',
    'Stop implementation now; report only and make no more changes.',
]
PROVIDERS = ['chat', 'responses-default', 'responses-codex', 'anthropic']
RESULTS = {
    'compact_receipt': 'Historical working state: release checks remain.',
    'real_read': 'README documents the supported installation command.',
    'real_check': 'Release readiness checks: 2 passed.',
}


def _call(call_id, name, arguments):
    return {'id': call_id, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments)}}


def _messages(live):
    return [
        history.make_history_carrier([
            {'kind': 'opening_request', 'content': OPENING},
            {'kind': 'earlier_request', 'content': EARLIER},
        ]),
        {'role': 'assistant', 'content': None, '_isMeta': True,
         'tool_calls': [_call('compact_receipt', 'context_compact', {})]},
        {'role': 'tool', 'name': 'context_compact',
         'tool_call_id': 'compact_receipt', 'content': RESULTS['compact_receipt'],
         '_isCompactionSummary': True},
        {'role': 'assistant', 'content': 'Checking the release evidence.',
         'tool_calls': [_call('real_read', 'read_files', {'path': 'README.md'}),
                        _call('real_check', 'run_command', {'command': 'pytest'})]},
        {'role': 'tool', 'name': 'read_files', 'tool_call_id': 'real_read',
         'content': RESULTS['real_read']},
        {'role': 'tool', 'name': 'run_command', 'tool_call_id': 'real_check',
         'content': RESULTS['real_check']},
        {'role': 'user', 'content': live},
    ]


def _wire(messages, provider):
    body = build_body('gpt-4o', deepcopy(messages), max_tokens=1000,
                      provider_id='openai')
    _assert_no_private_fields(body['messages'])
    if provider.startswith('responses-'):
        return openai_body_to_responses(
            body, profile=provider.removeprefix('responses-'))[0]['input']
    if provider == 'anthropic':
        return openai_body_to_anthropic(body)['messages']
    return body['messages']


def _assert_no_private_fields(value):
    if isinstance(value, dict):
        assert all(not key.startswith('_') for key in value)
        for nested in value.values():
            _assert_no_private_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_private_fields(nested)


def _text(message):
    content = message.get('content') or ''
    if isinstance(content, str):
        return content
    return ''.join(block.get('text', '') for block in content)


def _assert_history_and_live(items, live):
    _assert_no_private_fields(items)
    historical = [item for item in items if OPENING in _text(item)]
    assert len(historical) == 1
    text = _text(historical[0])
    assert 'Historical user evidence' in text
    assert 'historical opening_request' in text
    assert OPENING in text
    assert 'not a new request' in text
    assert 'take precedence' in text
    assert items[-1]['role'] == 'user'
    assert _text(items[-1]).endswith(live)
    assert sum(_text(item).count(live) for item in items) == 1
    # A provider may merge user messages, but never place live text inside
    # the quoted history envelope or attach historical authority to it.
    closing = text.index('</retained_user_messages>')
    assert live not in text[:closing]
    if live in text:
        assert text.index(live) > closing


def _assert_pairs(items, provider):
    calls, results = {}, {}
    if provider == 'anthropic':
        assert all(item['role'] in {'user', 'assistant'} for item in items)
        # Messages accepts consecutive same-role inputs and combines them
        # server-side (SDK MessageCreateParams.messages contract). Do not
        # demand local coalescing or rewrite cache/native message boundaries.
        for index, item in enumerate(items):
            assert isinstance(item['content'], list) and item['content']
            for block in item['content']:
                if block['type'] == 'text':
                    assert block['text'].strip()
                elif block['type'] == 'tool_use':
                    assert item['role'] == 'assistant'
                    assert isinstance(block['input'], dict)
                    assert block['id'] not in calls
                    calls[block['id']] = index
                elif block['type'] == 'tool_result':
                    assert item['role'] == 'user'
                    call_id = block['tool_use_id']
                    assert calls[call_id] == index - 1
                    assert call_id not in results
                    results[call_id] = block['content']
        # Whether locally merged or split on the wire, the final user run
        # keeps the complete result batch BEFORE the latest instruction.
        tail_start = len(items) - 1
        while tail_start > 0 and items[tail_start - 1]['role'] == 'user':
            tail_start -= 1
        assert [b['type'] for item in items[tail_start:]
                for b in item['content']] == ['tool_result', 'tool_result', 'text']
    elif provider.startswith('responses-'):
        for index, item in enumerate(items):
            if item['type'] == 'function_call':
                assert item['call_id'] not in calls
                assert isinstance(json.loads(item['arguments']), dict)
                calls[item['call_id']] = index
            elif item['type'] == 'function_call_output':
                call_id = item['call_id']
                assert calls[call_id] < index
                assert call_id not in results
                results[call_id] = item['output']
            else:
                assert item['type'] == 'message'
                assert all(block['type'] == (
                    'output_text' if item['role'] == 'assistant' else 'input_text')
                    for block in item['content'])
    else:
        pending = set()
        for index, item in enumerate(items):
            if item['role'] == 'tool':
                call_id = item['tool_call_id']
                assert call_id in pending
                pending.remove(call_id)
                assert call_id not in results
                results[call_id] = item['content']
            else:
                assert not pending
                for call in item.get('tool_calls', []):
                    assert item['role'] == 'assistant'
                    assert call['id'] not in calls
                    assert isinstance(json.loads(call['function']['arguments']), dict)
                    calls[call['id']] = index
                    pending.add(call['id'])
        assert not pending
    assert set(calls) == set(RESULTS)
    assert results == RESULTS


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('live', LIVE_REQUESTS)
def test_retained_history_and_complete_batch_reach_provider(provider, live):
    messages = _messages(live)
    before = deepcopy(messages)
    items = _wire(messages, provider)
    _assert_history_and_live(items, live)
    assert 'historical earlier_request' in _text(items[0])
    assert EARLIER in _text(items[0])
    _assert_pairs(items, provider)
    assert messages == before


@pytest.mark.parametrize('provider', PROVIDERS)
def test_manual_compaction_projected_reload_keeps_live_stop_outside_history(provider):
    live = LIVE_REQUESTS[-1]
    raw = [
        {'role': 'user', 'content': OPENING},
        {'role': 'assistant', '_isCompactionSummary': True,
         'content': 'Historical working state: release checks remain.'},
        {'role': 'user', 'content': live},
    ]
    before = deepcopy(raw)
    projected = _transform_messages(raw, {})
    assert history.history_entries(projected[0])[0]['content'] == OPENING
    _assert_history_and_live(_wire(projected, provider), live)
    assert raw == before


def test_anthropic_same_role_history_merge_preserves_both_labels_and_live_boundary():
    live = LIVE_REQUESTS[-1]
    messages = [
        history.make_history_carrier([
            {'kind': 'opening_request', 'content': OPENING}]),
        history.make_history_carrier([
            {'kind': 'earlier_request', 'content': EARLIER}]),
        {'role': 'user', 'content': live},
    ]
    items = _wire(messages, 'anthropic')
    assert len(items) == 1
    _assert_history_and_live(items, live)
    text = _text(items[0])
    assert 'historical earlier_request' in text
    assert EARLIER in text
    assert text.rindex('</retained_user_messages>') < text.index(live)
    assert text.count('Historical user evidence') == 2


def test_anthropic_same_role_assistant_input_keeps_receipt_and_real_batch():
    live = LIVE_REQUESTS[0]
    messages = _messages(live)
    messages.insert(3, {'role': 'assistant', 'content': 'Continuing from history.'})
    items = _wire(messages, 'anthropic')
    _assert_history_and_live(items, live)
    _assert_pairs(items, 'anthropic')
    assert any('Continuing from history.' in _text(item) for item in items)
    assert any('Checking the release evidence.' in _text(item) for item in items)
