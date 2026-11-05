"""Synthetic native Responses output retains occurrence order through replay.

Ingress and checked egress stay real; no provider, network, or executable tools.
Canonical prose/calls remain authoritative after hooks or history projection.
"""

from copy import deepcopy
import json
import time

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.responses_outbound import (
    ResponsesSSETranslator,
    openai_body_to_responses_checked,
    responses_response_to_openai,
)

pytestmark = pytest.mark.unit


def _reasoning(value, identifier='recycled'):
    return {'type': 'reasoning', 'id': identifier, 'summary': [],
            'encrypted_content': value}


def _call(identifier, arguments='{}', **extra):
    return {'type': 'function_call', 'id': 'fc-' + identifier,
            'call_id': identifier, 'name': 'synthetic_read',
            'arguments': arguments, 'status': 'completed', **extra}


def _message(text, phase=None):
    item = {'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text}]}
    if phase is not None:
        item['phase'] = phase
    return item


def _receive(output, streaming):
    response = {'id': 'resp-synthetic', 'status': 'completed',
                'output': deepcopy(output),
                'usage': {'input_tokens': 1, 'output_tokens': 1}}
    if not streaming:
        return responses_response_to_openai(response)['choices'][0]['message']
    body = {'model': 'gpt-6-astra', 'messages': [], 'tools': [
        {'type': 'function', 'function': {'name': 'synthetic_read',
                                        'parameters': {'type': 'object'}}}]}
    accumulator = SSEAccumulator(
        body, 'replay-synthetic',
        RawSSEDumper('gpt-6-astra', 'replay-synthetic', body),
        ResponsesSSETranslator('gpt-6-astra'), time.time())
    for index, item in enumerate(output):
        accumulator.feed_line('data: ' + json.dumps({
            'type': 'response.output_item.added', 'output_index': index,
            'item': item}))
        accumulator.feed_line('data: ' + json.dumps({
            'type': 'response.output_item.done', 'output_index': index,
            'item': item}))
    accumulator.feed_line('data: ' + json.dumps({
        'type': 'response.completed', 'response': response}))
    return accumulator.finalize()[0]


def _replay(message, profile='codex'):
    messages = [{'role': 'user', 'content': 'Synthetic task'}, message]
    messages.extend({'role': 'tool', 'tool_call_id': call['id'],
                     'content': 'Already completed'}
                    for call in message.get('tool_calls') or [])
    return openai_body_to_responses_checked(
        {'model': 'gpt-6-astra', 'messages': messages}, profile=profile)


def _identity(item):
    return (item.get('type'), item.get('call_id') or item.get('id'),
            item.get('encrypted_content'))


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('profile', ['default', 'codex'])
@pytest.mark.parametrize('phase', [None, 'commentary'])
def test_native_cross_type_order_survives_real_ingress_and_checked_egress(
        streaming, profile, phase):
    output = [_reasoning('cipher-one'), _call('one'),
              _reasoning('cipher-two'), _message('Visible work', phase),
              _call('two', '{"n": 2}')]
    message = _receive(output, streaming)
    original = deepcopy(message)
    wire = _replay(message, profile).require_payload()
    replayed = [item for item in wire['input']
                if item.get('type') != 'function_call_output'
                and item.get('role') != 'user']
    assert [_identity(item) for item in replayed] == [
        _identity(item) for item in output]
    if phase is not None:
        assert next(item for item in replayed if item['type'] == 'message')['phase'] == phase
    assert message == original
    next(item for item in replayed if item['type'] == 'reasoning')['summary'].append(
        {'type': 'summary_text', 'text': 'wire mutation'})
    assert message == original


@pytest.mark.parametrize('streaming', [False, True])
def test_equal_opaque_occurrences_are_not_deduplicated_or_moved_across_calls(streaming):
    opaque = _reasoning('equal-cipher')
    output = [deepcopy(opaque), _call('one'), deepcopy(opaque), _call('two')]
    replayed = _replay(_receive(output, streaming)).require_payload()['input']
    replayed = [item for item in replayed
                if item.get('type') in {'reasoning', 'function_call'}]
    assert [_identity(item) for item in replayed] == [_identity(item) for item in output]


@pytest.mark.parametrize('streaming', [False, True])
def test_captured_call_can_never_resurrect_a_removed_canonical_call(streaming):
    message = _receive([_reasoning('cipher-one'), _call('removed'),
                        _reasoning('cipher-two'), _call('kept')], streaming)
    message['tool_calls'] = message['tool_calls'][1:]
    result = _replay(message)
    # A changed canonical sequence cannot certify the old native interleaving.
    assert result.payload is None
    assert result.validation_issues == ('native_replay_projection_mismatch',)
    assert 'cipher-' not in str(result.validation_issues)


@pytest.mark.parametrize('streaming', [False, True])
def test_captured_call_cannot_override_changed_canonical_arguments(streaming):
    message = _receive([_reasoning('cipher-one'), _call('one'),
                        _reasoning('cipher-two'), _call('two')], streaming)
    message['tool_calls'][0]['function']['arguments'] = '{"changed": true}'
    result = _replay(message)
    assert result.payload is None
    assert result.validation_issues == ('native_replay_projection_mismatch',)


@pytest.mark.parametrize('streaming', [False, True])
def test_prose_edit_cannot_resurrect_original_native_text(streaming):
    message = _receive([_reasoning('cipher-one'), _call('one'),
                        _reasoning('cipher-two'), _message('Text to redact', 'commentary')],
                       streaming)
    message['content'] = 'Redacted text'
    result = _replay(message)
    # Unlike display-only phases, mandatory encrypted interleaving cannot be
    # rebuilt from changed prose without an explicit completed-turn projection.
    assert result.payload is None
    assert result.validation_issues == ('native_replay_projection_mismatch',)


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('witnessed', [False, True])
def test_native_text_override_cannot_resurrect_original_responses_prose(streaming, witnessed):
    output = [_reasoning('cipher-one')]
    if witnessed:
        output.extend([_call('one'), _reasoning('cipher-two')])
    output.append(_message('Original text to redact', 'commentary'))
    message = _receive(output, streaming)
    message['_anthropic_content_blocks'] = [{'type': 'text', 'text': 'Redacted text'}]
    before = deepcopy(message)
    result = _replay(message)
    if witnessed:
        # An override changes the effective canonical projection. The old
        # mandatory native interleaving must fail, never restore stale text.
        assert result.payload is None
        assert result.validation_issues == ('native_replay_projection_mismatch',)
    else:
        # Optional phase-only metadata can fall back to the live text override.
        payload = result.require_payload()
        text = [part['text'] for item in payload['input']
                if item.get('role') == 'assistant'
                for part in item.get('content') or [] if part.get('type') == 'output_text']
        assert text == ['Redacted text']
        assert 'Original text to redact' not in json.dumps(payload)
    assert message == before


def test_legacy_opaque_only_sidecars_keep_the_existing_canonical_call_path():
    message = {'role': 'assistant', '_responses_items': [_reasoning('legacy')],
               'tool_calls': [{'id': 'one', 'type': 'function',
                               'function': {'name': 'synthetic_read', 'arguments': '{}'}}]}
    replayed = _replay(message).require_payload()['input']
    assert [item['type'] for item in replayed] == [
        'message', 'reasoning', 'function_call', 'function_call_output']


@pytest.mark.parametrize('streaming', [False, True])
def test_call_attribution_and_argument_bytes_are_preserved(streaming):
    arguments = ' { "n" : 2, "text": "字" } '
    output = [_reasoning('cipher-one'), _call('one', arguments,
              caller={'type': 'program', 'caller_id': 'owned-program'}),
              _reasoning('cipher-two'), _call('two')]
    replayed = _replay(_receive(output, streaming)).require_payload()['input']
    calls = [item for item in replayed if item['type'] == 'function_call']
    assert calls[0]['arguments'] == arguments
    assert calls[0]['caller'] == {'type': 'program', 'caller_id': 'owned-program'}
    assert [item['type'] for item in replayed[1:-2]] == [
        'reasoning', 'function_call', 'reasoning', 'function_call']


@pytest.mark.parametrize('mutation', ['cipher', 'order', 'duplicate', 'negative', 'bool',
                                      'removed', 'version', 'role'])
def test_changed_or_malformed_replay_witness_fails_locally_without_payload(mutation):
    from lib.llm.responses_replay import REPLAY_ORDER_TYPE

    message = _receive([_reasoning('cipher-one'), _call('one'),
                        _reasoning('cipher-two'), _call('two')], False)
    marker = next(item for item in message['_responses_items']
                  if item['type'] == REPLAY_ORDER_TYPE)
    if mutation == 'cipher':
        message['_responses_items'][0]['encrypted_content'] += 'changed'
    elif mutation == 'order':
        marker['sequence'][0], marker['sequence'][1] = marker['sequence'][1], marker['sequence'][0]
    elif mutation == 'duplicate':
        marker['sequence'][1] = marker['sequence'][0]
    elif mutation == 'negative':
        marker['sequence'][0][1] = -1
    elif mutation == 'bool':
        marker['sequence'][0][1] = False
    elif mutation == 'removed':
        marker['sequence'].pop()
    elif mutation == 'version':
        marker['version'] = 99
    else:
        message['_responses_items'][0]['role'] = 'user'
    result = _replay(message)
    assert result.payload is None
    assert result.validation_issues == ('native_replay_projection_mismatch',)


@pytest.mark.parametrize('streaming', [False, True])
def test_interleaving_survives_body_and_segment_cold_rebuild(streaming):
    from lib.llm.body import build_body
    from lib.tasks_pkg.segments import (
        assemble_segments, segments_to_json, rehydrate_segments,
        reconstruct_tool_messages_from_segments,
    )

    output = [_reasoning('cipher-one'), _call('one'),
              _reasoning('cipher-two'), _message('Visible work', 'commentary'), _call('two')]
    message = _receive(output, streaming)
    rounds = [{'roundNum': index + 1, 'llmRound': 0, 'attemptId': 'attempt-owning',
               'taskId': 'task-owning', 'toolCallId': call['id'],
               'toolName': call['function']['name'], 'toolArgs': call['function']['arguments'],
               'toolContent': 'Already completed', 'status': 'done'}
              for index, call in enumerate(message['tool_calls'])]
    rounds[0].update(assistantContent=message.get('content', ''),
                     _responsesItems=message['_responses_items'])
    original = deepcopy(rounds)
    segments = assemble_segments({'content': '', 'thinking': '', 'toolRounds': rounds})
    cold = reconstruct_tool_messages_from_segments(
        rehydrate_segments(json.loads(json.dumps(segments_to_json(segments))), rounds))
    body = build_body('gpt-6-astra', [{'role': 'user', 'content': 'Synthetic task'}, *cold])
    replayed = openai_body_to_responses_checked(body, profile='codex').require_payload()['input']
    replayed = [item for item in replayed
                if item.get('type') != 'function_call_output' and item.get('role') != 'user']
    assert [_identity(item) for item in replayed] == [_identity(item) for item in output]
    assert rounds == original


@pytest.mark.parametrize('overflow', ['count', 'text'])
def test_mandatory_order_overflow_is_refusal_not_truncated_state(monkeypatch, overflow):
    from lib.llm import responses_replay

    output = [_reasoning('cipher-one'), _call('one'),
              _reasoning('cipher-two'), _message('Too large', 'commentary')]
    if overflow == 'count':
        monkeypatch.setattr(responses_replay, 'MAX_REPLAY_ORDER_ENTRIES', 3)
    else:
        monkeypatch.setattr(responses_replay, 'MAX_REPLAY_MESSAGE_TEXT_BYTES', 2)
    message = _receive(output, False)
    result = _replay(message)
    assert result.payload is None
    assert result.validation_issues == ('native_replay_unrepresentable',)
    assert [item['encrypted_content'] for item in message['_responses_items']
            if item['type'] == 'reasoning'] == ['cipher-one', 'cipher-two']


def test_order_witness_does_not_bypass_destination_replay_capability():
    message = _receive([_reasoning('cipher-one'), _call('one'),
                        _reasoning('cipher-two'), _call('two')], False)
    body = {'model': 'gpt-6-astra', 'messages': [message],
            '_inference_profile': {'features': {'responses_reasoning_replay': 'unsupported'}}}
    result = openai_body_to_responses_checked(body, profile='codex')
    assert result.payload is None
    assert result.validation_issues == ('mandatory_continuity_unrepresentable',)
