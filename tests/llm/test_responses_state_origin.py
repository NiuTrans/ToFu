"""Encrypted Responses state names its minting Deployment without reaching the wire.

Ingress and checked egress stay real; no provider, network or executable tools.
2026-10-31 (task d9a651dc round 28): 27 rounds of ``gpt-6.1-sol`` reasoning
minted on the Kunlun Deployment were replayed to the public Deployment of the
same model after a failover, which rejected them ``invalid_encrypted_content``.
The marker specified here is the provenance dispatch binds on.
"""

from copy import deepcopy
import json
import time

import pytest

from lib.conversation_sync.usage_recovery import fresh_recovery_model_messages
from lib.llm._sse_core import SSEAccumulator
from lib.llm.conversion import conversion_report
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.responses_outbound import (
    ResponsesSSETranslator,
    openai_body_to_responses_checked,
    responses_response_to_openai,
)
from lib.llm.responses_state_origin import (
    STATE_ORIGIN_TYPE,
    responses_state_origins,
    stamp_state_origin,
)

pytestmark = pytest.mark.unit

KUNLUN = 'deployment-kunlun'
PUBLIC = 'deployment-public'


def _reasoning(value, identifier='rs-synthetic'):
    return {'type': 'reasoning', 'id': identifier, 'summary': [],
            'encrypted_content': value}


def _call(identifier, arguments='{}'):
    return {'type': 'function_call', 'id': 'fc-' + identifier,
            'call_id': identifier, 'name': 'synthetic_read',
            'arguments': arguments, 'status': 'completed'}


def _message(text):
    return {'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text}]}


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
        body, 'origin-synthetic',
        RawSSEDumper('gpt-6-astra', 'origin-synthetic', body),
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


def _replay_input(message):
    messages = [{'role': 'user', 'content': 'Synthetic task'}, message]
    messages.extend({'role': 'tool', 'tool_call_id': call['id'],
                     'content': 'Already completed'}
                    for call in message.get('tool_calls') or [])
    return openai_body_to_responses_checked(
        {'model': 'gpt-6-astra', 'messages': messages},
        profile='codex').require_payload()['input']


def _identity(item):
    return (item.get('type'), item.get('call_id') or item.get('id'),
            item.get('encrypted_content'))


def _markers(message):
    return [item for item in message.get('_responses_items') or ()
            if item.get('type') == STATE_ORIGIN_TYPE]


def test_only_encrypted_state_is_stamped_and_a_restamp_replaces_the_marker():
    message = {'role': 'assistant', 'content': 'Done',
               '_responses_items': [_reasoning('cipher')]}
    alias = message['_responses_items']
    assert stamp_state_origin(message, KUNLUN) is True
    assert _markers(message) == [
        {'type': STATE_ORIGIN_TYPE, 'version': 1, 'deployment_id': KUNLUN}]
    assert alias == [_reasoning('cipher')], 'the sidecar list is rebuilt'
    assert stamp_state_origin(message, PUBLIC) is True
    assert [marker['deployment_id'] for marker in _markers(message)] == [PUBLIC]
    assert responses_state_origins([message]) == {PUBLIC}

    plain = {'role': 'assistant', 'content': 'Done', '_responses_items': [
        {'type': 'reasoning', 'id': 'rs-summary-only', 'summary': []}]}
    before = deepcopy(plain)
    assert stamp_state_origin(plain, KUNLUN) is False
    assert stamp_state_origin({'role': 'assistant', 'content': 'x'}, KUNLUN) is False
    unattributed = {'role': 'assistant', '_responses_items': [_reasoning('cipher')]}
    assert stamp_state_origin(unattributed, '') is False
    assert plain == before and not _markers(unattributed)


def test_origins_count_only_marked_state_that_is_still_replayed():
    marked = {'role': 'assistant', 'content': 'a',
              '_responses_items': [_reasoning('cipher')]}
    stamp_state_origin(marked, KUNLUN)
    legacy = {'role': 'assistant', 'content': 'b',
              '_responses_items': [_reasoning('older-cipher')]}
    retired = {'role': 'assistant', 'content': 'c', '_responses_items': [
        {'type': STATE_ORIGIN_TYPE, 'version': 1, 'deployment_id': PUBLIC}]}
    foreign_version = {'role': 'assistant', '_responses_items': [
        _reasoning('cipher'),
        {'type': STATE_ORIGIN_TYPE, 'version': 2, 'deployment_id': PUBLIC}]}
    history = [{'role': 'user', 'content': 'task'}, marked, legacy, retired,
               foreign_version]
    assert responses_state_origins(history) == {KUNLUN}
    assert responses_state_origins({'model': 'm', 'messages': history}) == {KUNLUN}
    assert responses_state_origins([legacy, retired]) == frozenset()
    assert responses_state_origins(None) == frozenset()


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('output', [
    [_reasoning('cipher-only'), _message('Visible work')],
    [_reasoning('cipher-one'), _call('one'), _reasoning('cipher-two'),
     _message('Visible work'), _call('two', '{"n": 2}')],
], ids=['canonical-order', 'order-witness'])
def test_marker_never_reaches_the_wire_and_keeps_native_order(streaming, output):
    message = _receive(output, streaming)
    assert stamp_state_origin(message, KUNLUN) is True
    replayed = _replay_input(message)
    assert all(item.get('type') != STATE_ORIGIN_TYPE for item in replayed)
    assert KUNLUN not in json.dumps(replayed)
    native = [item for item in replayed
              if item.get('type') in {'reasoning', 'function_call', 'message'}
              and item.get('role') != 'user']
    assert [_identity(item) for item in native] == [
        _identity(item) for item in output]


@pytest.mark.parametrize('protocol, options', [
    ('anthropic', {}),
    ('openai', {}),
    ('responses', {'replay_reasoning': True, 'replay_types': frozenset({'reasoning'})}),
])
def test_checked_conversion_reports_no_loss_for_the_marker(protocol, options):
    unmarked = {'role': 'assistant', 'content': 'Visible work',
                '_responses_items': [_reasoning('cipher')]}
    marked = deepcopy(unmarked)
    stamp_state_origin(marked, KUNLUN)

    def report(message):
        return conversion_report(
            {'messages': [{'role': 'user', 'content': 'task'}, message]},
            protocol, **options)

    assert report(marked) == report(unmarked)


def test_usage_recovery_retires_the_marker_with_the_state_it_describes():
    message = {'role': 'assistant', 'content': '',
               '_responses_items': [_reasoning('cipher')]}
    stamp_state_origin(message, KUNLUN)
    program = {'type': 'program', 'id': 'prog-1', 'call_id': 'prog-1'}
    with_program = deepcopy(message)
    with_program['_responses_items'].insert(0, program)

    retired, kept = fresh_recovery_model_messages([message, with_program])

    assert '_responses_items' not in retired, 'a lone marker must not keep the row'
    assert kept['_responses_items'] == [program]
    assert responses_state_origins([retired, kept]) == frozenset()
