"""Foreign reasoning is private replay state, not portable executable history."""
from copy import deepcopy
import importlib
import json

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic_checked
from lib.llm.responses_outbound import (
    openai_body_to_responses_checked, responses_response_to_openai,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
        lambda *a, **k: 200000)
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')


def _history():
    """Use the real Responses ingress so tool/ciphertext carriers match runtime."""
    response = responses_response_to_openai({
        'id': 'response-synthetic', 'status': 'completed', 'output': [
            {'type': 'reasoning', 'id': 'rs-1',
             'encrypted_content': 'synthetic-ciphertext-1',
             'summary': [{'type': 'summary_text', 'text': 'Private summary'}]},
            {'type': 'reasoning', 'id': 'rs-2',
             'encrypted_content': 'synthetic-ciphertext-2'},
            {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'Existing visible work'}]},
            {'type': 'function_call', 'call_id': 'call-read',
             'name': 'read_file', 'arguments': '{"path":"synthetic.txt"}'},
            {'type': 'function_call', 'call_id': 'call-write',
             'name': 'write_file', 'arguments': '{"path":"done.txt","content":"saved"}'},
        ]})
    assert response['choices'][0]['finish_reason'] == 'tool_calls'
    return [
        {'role': 'system', 'content': 'Preserve the existing task constraints'},
        {'role': 'user', 'content': 'Continue the existing work'},
        response['choices'][0]['message'],
        {'role': 'tool', 'tool_call_id': 'call-read', 'content': 'Exact read result\n  indented'},
        {'role': 'tool', 'tool_call_id': 'call-write', 'content': 'Already saved; do not write again'},
    ]


def _assert_portable_history(wire):
    assert wire['system'] == 'Preserve the existing task constraints'
    assert wire['messages'] == [
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'Continue the existing work'}]},
        {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'Existing visible work'},
            {'type': 'tool_use', 'id': 'call-read', 'name': 'read_file',
             'input': {'path': 'synthetic.txt'}},
            {'type': 'tool_use', 'id': 'call-write', 'name': 'write_file',
             'input': {'path': 'done.txt', 'content': 'saved'}},
        ]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'call-read',
             'content': 'Exact read result\n  indented'},
            {'type': 'tool_result', 'tool_use_id': 'call-write',
             'content': 'Already saved; do not write again'},
        ]},
    ]
    encoded = json.dumps(wire)
    assert 'synthetic-ciphertext' not in encoded
    assert 'Private summary' not in encoded
    assert '_responses_items' not in encoded
    assert '"thinking"' not in encoded


@pytest.mark.parametrize('model', ['kimi-k3', 'compatible-messages-model'])
@pytest.mark.parametrize('continuity', [None, 'completed_turn', 'active_turn'])
def test_messages_projects_responses_history_without_foreign_reasoning(model, continuity):
    body = {'model': model, 'messages': _history()}
    if continuity is not None:
        body['messages'][2]['_reasoning_continuity'] = continuity
    original = deepcopy(body)
    result = openai_body_to_anthropic_checked(body)
    _assert_portable_history(result.require_payload())
    assert result.permitted_conversion_losses == {
        'optional_reasoning_omitted': 1, 'cross_protocol_reasoning_omitted': 2}
    assert body == original


@pytest.mark.parametrize('stream', [False, True])
def test_final_messages_request_preserves_history_and_seals_conversion_report(stream):
    from lib.llm._sse_core import prepare_request
    from lib.llm.body import build_body

    messages = _history()
    original = deepcopy(messages)
    body = build_body('kimi-k3', messages, stream=stream, thinking_enabled=False)
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol='anthropic')
    wire = json.loads(plan.body.payload)
    _assert_portable_history(wire)
    assert '_conversion_report' not in wire
    assert messages == original


class TransportCaptured(BaseException):
    """Stop at the actual HTTP boundary, with no network or provider response."""


def test_nonstream_messages_http_uses_the_same_conversion(monkeypatch):
    chat_module = importlib.import_module('lib.llm.chat')
    captured = []

    def post(url, **kwargs):
        captured.append(json.loads(kwargs['data']))
        raise TransportCaptured()

    monkeypatch.setattr(chat_module, 'http_post', post)
    messages = _history()
    original = deepcopy(messages)
    with pytest.raises(TransportCaptured):
        chat_module.chat(messages, 'kimi-k3', api_protocol='anthropic',
                         api_key='synthetic', base_url='https://example.invalid/v1',
                         thinking_enabled=False, max_retries=0)
    assert len(captured) == 1
    _assert_portable_history(captured[0])
    assert '_conversion_report' not in captured[0]
    assert messages == original


@pytest.mark.parametrize('profile', ['default', 'codex'])
def test_messages_projection_does_not_destroy_later_responses_replay(profile):
    body = {'model': 'gpt-6-astra', 'messages': _history(),
            '_route_inference_profile': {
                'features': {'responses_reasoning_replay': 'supported'}}}
    original = deepcopy(body)
    openai_body_to_anthropic_checked(body).require_payload()
    result = openai_body_to_responses_checked(body, profile=profile)
    replayed = [item for item in result.require_payload()['input']
                if item.get('type') == 'reasoning']
    assert replayed == original['messages'][2]['_responses_items']
    assert 'cross_protocol_reasoning_omitted' not in result.permitted_conversion_losses
    assert body == original


@pytest.mark.parametrize('profile', ['default', 'codex'])
@pytest.mark.parametrize('declaration', ['unsupported', 'unknown', None, False])
def test_responses_replay_denial_is_not_a_cross_protocol_omission(profile, declaration):
    body = {'model': 'gpt-6-astra', 'messages': _history(),
            '_route_inference_profile': {
                'features': {'responses_reasoning_replay': declaration}}}
    original = deepcopy(body)
    result = openai_body_to_responses_checked(body, profile=profile)
    assert result.validation_issues == ('mandatory_continuity_unrepresentable',)
    assert result.payload is None
    assert body == original


@pytest.mark.parametrize('kind', [
    'compaction', 'program', 'program_output', 'shell_call', 'shell_call_output',
    'tool_search_call', 'tool_search_output', 'future_call', 'function_call_output',
])
def test_foreign_reasoning_does_not_make_unrepresentable_execution_portable(kind):
    body = {'model': 'kimi-k3', 'messages': _history()}
    body['messages'][2]['_responses_items'].append(
        {'type': kind, 'id': 'owned', 'call_id': 'native-call',
         'encrypted_content': 'synthetic-opaque-execution'})
    original = deepcopy(body)
    result = openai_body_to_anthropic_checked(body)
    assert result.validation_issues == ('mandatory_continuity_unrepresentable',)
    assert result.payload is None
    assert body == original


@pytest.mark.parametrize('invalid', [
    True, False, 0, 1, [], {}, ['synthetic-secret'], {'secret': 'synthetic'},
])
def test_messages_does_not_treat_malformed_ciphertext_as_optional(invalid):
    body = {'messages': [{'role': 'assistant', 'content': 'Visible work',
                         '_responses_items': [
                             {'type': 'reasoning', 'encrypted_content': invalid}]}]}
    original = deepcopy(body)
    result = openai_body_to_anthropic_checked(body)
    assert result.validation_issues == ('native_content_invalid',)
    assert result.payload is None
    assert body == original


@pytest.mark.parametrize('fields', [{}, {'encrypted_content': None}, {'encrypted_content': ''}])
def test_messages_keeps_absent_ciphertext_metadata_compatibility(fields):
    body = {'messages': [{'role': 'assistant', 'content': 'Visible work',
                         '_responses_items': [{'type': 'reasoning', **fields}]}]}
    original = deepcopy(body)
    result = openai_body_to_anthropic_checked(body)
    assert result.require_payload()['messages'][0]['content'] == [
        {'type': 'text', 'text': 'Visible work'}]
    assert result.permitted_conversion_losses == {'native_metadata_omitted': 1}
    assert body == original


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('with_id', [False, True])
def test_ciphertext_only_occurrences_survive_real_ingress_and_messages_projection(
        streaming, with_id):
    from time import time

    from lib.llm._sse_core import SSEAccumulator
    from lib.llm.diagnostics import RawSSEDumper
    from lib.llm.responses_outbound import ResponsesSSETranslator

    reasoning = {'type': 'reasoning', 'encrypted_content': 'synthetic-ciphertext'}
    if with_id:
        reasoning['id'] = 'recycled-reasoning-id'
    messages = _history()
    expected_assistant = messages[2]
    output = [deepcopy(reasoning), deepcopy(reasoning), {
        'type': 'message', 'role': 'assistant', 'content': [{
            'type': 'output_text', 'text': expected_assistant['content']}]}]
    for call in expected_assistant['tool_calls']:
        output.append({'type': 'function_call', 'call_id': call['id'],
                       'name': call['function']['name'],
                       'arguments': call['function']['arguments']})
    response = {'id': 'response-synthetic', 'status': 'completed',
                'output': output, 'usage': {'input_tokens': 10, 'output_tokens': 5}}
    if streaming:
        body = {'model': 'gpt-6-astra', 'messages': [], 'tools': [
            {'type': 'function', 'function': {
                'name': call['function']['name'], 'parameters': {'type': 'object'}}}
            for call in expected_assistant['tool_calls']]}
        acc = SSEAccumulator(
            body, 'synthetic-trace', RawSSEDumper('gpt-6-astra', 'synthetic-trace', body),
            ResponsesSSETranslator(model='gpt-6-astra'), time())
        # Provisional entries and terminal output share positions, not unique IDs.
        for index, item in enumerate(output[:2]):
            acc.feed_line('data: ' + json.dumps({
                'type': 'response.output_item.done', 'output_index': index, 'item': item}))
        for index, item in enumerate(output[3:], start=3):
            acc.feed_line('data: ' + json.dumps({
                'type': 'response.output_item.added', 'output_index': index, 'item': item}))
        acc.feed_line('data: ' + json.dumps({
            'type': 'response.completed', 'response': response}))
        assistant, finish, _usage = acc.finalize()
        assert finish == 'tool_calls'
    else:
        assistant = responses_response_to_openai(response)['choices'][0]['message']
    assert assistant['_responses_items'] == [reasoning, reasoning]
    assert not assistant.get('reasoning_content')
    messages[2] = assistant
    original = deepcopy(messages)
    result = openai_body_to_anthropic_checked({'messages': messages})
    _assert_portable_history(result.require_payload())
    assert result.permitted_conversion_losses == {'cross_protocol_reasoning_omitted': 2}
    replay = openai_body_to_responses_checked({
        'model': 'gpt-6-astra', 'messages': messages}, profile='codex').require_payload()
    assert [item for item in replay['input'] if item.get('type') == 'reasoning'] == [
        reasoning, reasoning]
    assert messages == original
