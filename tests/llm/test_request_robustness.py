"""Request preparation preserves replay state and fails before provider I/O.

Exercise real builders, protocol adapters and both stream shells with synthetic
state. Only counting, egress and HTTP are replaced; no live credentials needed.
"""
from copy import deepcopy
import asyncio
import importlib
import json

import pytest

from lib.llm._sse_core import prepare_request
from lib.llm.body import build_body
from lib.llm.llm_errors import LocalRequestPreparationError, PromptTooLongError
from lib.llm_sanitize import _merge_consecutive_same_role
from tests.llm.test_inference_local_dispatch_errors import dispatch_fixture as dispatch_fixture
from tests.sse.test_sse_core_parity import (
    NORMAL, _FakeAsyncStreamCtx, _FakeRequestsResp,
)

pytestmark = pytest.mark.unit

URL = 'https://example.invalid/v1'
PROTOCOLS = ('openai', 'anthropic', 'responses')


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
                        lambda *a, **k: 200000)


def _capture_openai(monkeypatch, entry, messages, *, model='MiniMax-M2', extra=None,
                    knowledge_model=''):
    wires = []

    def capture(kwargs):
        wires.append(json.loads(kwargs.get('content', kwargs.get('data'))))

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {'choices': [{'message': {'content': 'Hello world'},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}

    def post(url, **kwargs):
        capture(kwargs)
        return Response()

    class SyncSession:
        def post(self, url, **kwargs):
            capture(kwargs)
            return _FakeRequestsResp(NORMAL)

    class AsyncClient:
        def stream(self, method, url, **kwargs):
            capture(kwargs)
            return _FakeAsyncStreamCtx(NORMAL)

    kwargs = dict(base_url=URL, api_key='synthetic', api_protocol='openai')
    if entry == 'nonstream':
        module = importlib.import_module('lib.llm.chat')
        monkeypatch.setattr(module, 'http_post', post)
        text, _ = module.chat(messages, model, knowledge_model=knowledge_model,
                              extra=extra, max_tokens=1024, max_retries=0, **kwargs)
        assert text == 'Hello world'
    else:
        body = build_body(model, messages, knowledge_model=knowledge_model,
                          extra=extra, max_tokens=1024)
        if entry == 'async':
            module = importlib.import_module('lib.llm.astream')
            monkeypatch.setattr(module, 'get_async_client', lambda *a, **k: AsyncClient())
            result = asyncio.run(module._async_stream_chat_once(body, **kwargs))
        else:
            module = importlib.import_module('lib.llm.stream')
            monkeypatch.setattr(module, 'get_sync_session', lambda *a, **k: SyncSession())
            result = module._stream_chat_once(body, **kwargs)
        assert result.message['content'] == 'Hello world'
    assert len(wires) == 1
    return wires[0]


@pytest.mark.parametrize('state', [
    {'reasoning_content': '  Exact reasoning\n原文  '},
    {'reasoning_content': ''},
    {'thinking_signature': 'opaque-signature'},
    {'reasoning_details': [{'type': 'thinking', 'signature': 'opaque-signature'}]},
    {'_responses_items': [{'type': 'reasoning', 'encrypted_content': 'opaque'}]},
    {'_anthropic_content_blocks': [{'type': 'text', 'text': 'original'}]},
    {'_reasoning_continuity': 'active_turn'},
])
@pytest.mark.parametrize('side', [0, 1])
def test_same_role_merge_never_rebinds_assistant_state(state, side):
    messages = [{'role': 'assistant', 'content': 'first'},
                {'role': 'assistant', 'content': 'second'}]
    messages[side].update(deepcopy(state))
    original = deepcopy(messages)
    assert _merge_consecutive_same_role(messages) == original
    assert messages == original


@pytest.mark.parametrize('role', ['user', 'assistant'])
def test_named_participants_are_not_merged(role):
    messages = [{'role': role, 'name': 'alice', 'content': 'first'},
                {'role': role, 'name': 'bob', 'content': 'second'}]
    assert _merge_consecutive_same_role(messages) == messages


def test_plain_adjacent_text_still_merges():
    assert _merge_consecutive_same_role([
        {'role': 'assistant', 'content': 'first'},
        {'role': 'assistant', 'content': 'second'},
    ]) == [{'role': 'assistant', 'content': 'first\n\nsecond'}]


@pytest.mark.parametrize('protocol,model', [
    ('openai', 'deepseek-v4.1-flash'), ('anthropic', 'deepseek-v4.1-flash'),
    ('openai', 'claude-sonnet-4-6'), ('anthropic', 'claude-sonnet-4-6'),
])
def test_adjacent_reasoning_survives_builder_and_final_wire(protocol, model):
    messages = [{'role': 'user', 'content': 'start'}]
    for index in range(2):
        msg = {'role': 'assistant', 'content': f'answer-{index}',
               'reasoning_content': f'  reasoning-{index}\n'}
        if model.startswith('claude'):
            msg['thinking_signature'] = f'signature-{index}'
        messages.append(msg)
    messages.append({'role': 'user', 'content': 'continue'})
    original = deepcopy(messages)
    body = build_body('opaque-deployment', messages, knowledge_model=model)
    plan = prepare_request(body, base_url=URL, api_protocol=protocol)
    assistants = [msg for msg in plan.body['messages'] if msg['role'] == 'assistant']
    if protocol == 'anthropic':
        thinking = [block['thinking'] for msg in assistants for block in msg['content']
                    if block['type'] == 'thinking']
    else:
        thinking = [msg['reasoning_content'] for msg in assistants]
    assert thinking == ['  reasoning-0\n', '  reasoning-1\n']
    assert messages == original


@pytest.mark.parametrize('entry', ['nonstream', 'sync', 'async'])
def test_chat_projection_removes_private_carriers_and_preserves_public_state(monkeypatch, entry):
    messages = [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]},
        {'role': 'assistant', 'content': 'prior answer',
         'reasoning_content': '  retained reasoning\n',
         '_responses_items': [{'type': 'message', 'role': 'assistant', 'content': [
             {'type': 'output_text', 'text': 'prior answer'}]}],
         '_anthropic_content_blocks': [{'type': 'text', 'text': 'prior answer'}],
         '_reasoning_continuity': 'completed_turn'},
        {'role': 'user', 'content': 'continue'},
    ]
    extra = {'metadata': {'_user_key': 'user data'}, '_request_private': 'not sent'}
    original = deepcopy(messages)
    wire = _capture_openai(monkeypatch, entry, messages, extra=extra)
    assert wire['messages'][0]['content'] == 'hello'
    assert wire['messages'][1] == {
        'role': 'assistant', 'content': 'prior answer',
        'reasoning_content': '  retained reasoning\n'}
    assert not any(key.startswith('_') for key in wire)
    assert wire['metadata'] == {'_user_key': 'user data'}
    assert messages == original


@pytest.mark.parametrize('entry', ['nonstream', 'sync', 'async'])
def test_chat_projection_preserves_model_specific_signed_details(monkeypatch, entry):
    messages = [
        {'role': 'user', 'content': 'start'},
        {'role': 'assistant', 'content': 'answer', 'reasoning_content': 'signed thought',
         'thinking_signature': 'synthetic-signature', '_reasoning_continuity': 'active_turn'},
        {'role': 'user', 'content': 'continue'},
    ]
    wire = _capture_openai(monkeypatch, entry, messages, model='opaque-deployment',
                           knowledge_model='claude-sonnet-4-6')
    msg = wire['messages'][1]
    assert msg['reasoning_content'] == 'signed thought'
    assert msg['thinking_signature'] == 'synthetic-signature'
    assert msg['reasoning_details'][0]['signature'] == 'synthetic-signature'
    assert '_reasoning_continuity' not in msg


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('entry', ['nonstream', 'sync', 'async'])
@pytest.mark.parametrize('bad_value', [float('nan'), {'invalid-json-carrier'}, '\ud800'])
def test_invalid_json_never_reaches_provider_or_leaks_content(monkeypatch, protocol, entry, bad_value):
    messages = [{'role': 'user', 'content': 'private prompt'}]
    # A tool schema extension is retained by all three protocols; validate JSON
    # at the common count/seal boundary without inventing a provider allowlist.
    extra = {'tools': [{'type': 'function', 'function': {
        'name': 'inspect', 'parameters': {'type': 'object', 'properties': {},
                                         'x-private-extension': bad_value}}}]}
    kwargs = dict(base_url=URL, api_key='synthetic', api_protocol=protocol)
    sends = []

    def forbidden(*a, **k):
        sends.append(True)
        pytest.fail('invalid local request reached provider transport')

    chat_module = importlib.import_module('lib.llm.chat')
    sync = importlib.import_module('lib.llm.stream')
    asynchronous = importlib.import_module('lib.llm.astream')
    monkeypatch.setattr(chat_module, 'http_post', forbidden)
    monkeypatch.setattr(sync, 'get_sync_session', forbidden)
    monkeypatch.setattr(asynchronous, 'get_async_client', forbidden)
    with pytest.raises(LocalRequestPreparationError) as raised:
        if entry == 'nonstream':
            chat_module.chat(messages, 'test-model', extra=extra, max_retries=0, **kwargs)
        else:
            body = {'model': 'test-model', 'messages': messages, **extra}
            if entry == 'sync':
                sync._stream_chat_once(body, **kwargs)
            else:
                asyncio.run(asynchronous._async_stream_chat_once(body, **kwargs))
    assert raised.value.provider_reached is False
    assert not sends
    assert 'private' not in str(raised.value)
    assert 'invalid-json-carrier' not in str(raised.value)


@pytest.mark.parametrize('protocol', ['response', 'messages', 'unknown', False, 0, []])
def test_unknown_wire_protocol_is_rejected_locally(protocol):
    body = {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'hello'}]}
    with pytest.raises(LocalRequestPreparationError) as raised:
        prepare_request(body, base_url=URL, api_protocol=protocol)
    assert raised.value.provider_reached is False


def _invoke_preparation(entry, protocol):
    messages = [{'role': 'user', 'content': 'synthetic input'}]
    kwargs = dict(base_url=URL, api_key='synthetic', api_protocol=protocol)
    if entry == 'nonstream':
        module = importlib.import_module('lib.llm.chat')
        return module.chat(messages, 'test-model', max_retries=0, **kwargs)
    body = {'model': 'test-model', 'messages': messages}
    if entry == 'sync':
        module = importlib.import_module('lib.llm.stream')
        return module._stream_chat_once(body, **kwargs)
    module = importlib.import_module('lib.llm.astream')
    return asyncio.run(module._async_stream_chat_once(body, **kwargs))


@pytest.mark.parametrize('entry', ['nonstream', 'sync', 'async'])
@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_local_context_overflow_keeps_recoverable_type_and_evidence(monkeypatch, entry, protocol):
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 200001})
    with pytest.raises(PromptTooLongError) as raised:
        _invoke_preparation(entry, protocol)
    assert raised.value.origin == 'local'
    assert raised.value.provider_reached is False
    assert raised.value.total_tokens == 200001
    assert raised.value.context_window_tokens == 200000


@pytest.mark.parametrize('entry', ['nonstream', 'sync', 'async'])
@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_unexpected_preparation_failure_is_neutral_and_secret_free(monkeypatch, entry, protocol):
    failure = RuntimeError('private request fragment: synthetic secret')

    def fail_before_network(body):
        raise failure

    monkeypatch.setattr('lib.llm.request.detach_input', fail_before_network)
    with pytest.raises(LocalRequestPreparationError) as raised:
        _invoke_preparation(entry, protocol)
    assert raised.value.provider_reached is False
    assert raised.value.__cause__ is failure
    assert 'private' not in str(raised.value)
    assert 'synthetic secret' not in str(raised.value)


@pytest.mark.parametrize('entry', ['nonstream', 'sync', 'async'])
@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('failure', ['json', 'context'])
def test_real_dispatch_preparation_releases_once_without_retry_or_health_penalty(
        monkeypatch, dispatch_fixture, entry, protocol, failure):
    api, dispatcher, sleep = dispatch_fixture
    for slot in dispatcher.slots:
        slot.protocol = protocol
        slot.inference_profile = None
    snapshots = [slot.health() for slot in dispatcher.slots]
    kwargs = dict(max_retries=3, owner_user_id=987654)
    expected = LocalRequestPreparationError
    if failure == 'json':
        kwargs['tools'] = [{'type': 'function', 'function': {
            'name': 'inspect', 'parameters': {'type': 'object', 'properties': {},
                                             'x-invalid': float('nan')}}}]
    else:
        monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 200001})
        expected = PromptTooLongError
    messages = [{'role': 'user', 'content': 'offline specification'}]
    with pytest.raises(expected) as raised:
        if entry == 'nonstream':
            api.dispatch_chat(messages, **kwargs)
        elif entry == 'sync':
            api.dispatch_stream(messages, **kwargs)
        else:
            asyncio.run(api.async_dispatch_stream(messages, **kwargs))
    assert raised.value.provider_reached is False
    if failure == 'json':
        assert raised.value.stage == 'request_serialization'
    else:
        assert raised.value.origin == 'local'
    assert len(dispatcher.picks) == 1
    assert [slot.release_calls for slot in dispatcher.slots] == [1, 0]
    assert [slot.inflight for slot in dispatcher.slots] == [0, 0]
    assert [slot.health() for slot in dispatcher.slots] == snapshots
    for slot in dispatcher.slots:
        slot.record_error.assert_not_called()
        slot.record_success.assert_not_called()
        slot.record_truncation.assert_not_called()
    sleep.assert_not_called()


@pytest.mark.parametrize('entry', ['nonstream', 'sync', 'async'])
def test_unknown_protocol_never_defaults_to_chat_transport(monkeypatch, entry):
    with pytest.raises(LocalRequestPreparationError, match='unsupported_wire_protocol'):
        _invoke_preparation(entry, 'response')


@pytest.mark.parametrize('profile', ['codxe', '', None, []])
def test_unknown_responses_profile_is_not_silently_reinterpreted(profile):
    from lib.llm.responses_outbound import openai_body_to_responses_checked
    body = {'model': 'gpt-5.6', 'messages': [{'role': 'user', 'content': 'hello'}]}
    checked = openai_body_to_responses_checked(body, profile=profile)
    assert checked.payload is None
    assert checked.validation_issues == ('unsupported_responses_profile',)


@pytest.mark.parametrize('state', [
    {'_responses_items': [{'type': 'compaction', 'encrypted_content': 'opaque'}]},
    {'_responses_items': [{'type': 'program', 'call_id': 'p1', 'code': 'opaque'}]},
    {'_anthropic_content_blocks': [{'type': 'redacted_thinking', 'data': 'opaque'}]},
    {'_anthropic_content_blocks': [{'type': 'thinking', 'thinking': 'native thought',
                                    'signature': 'opaque-signature'}]},
])
@pytest.mark.parametrize('entry', ['prepare', 'nonstream'])
def test_chat_conversion_refuses_unrepresentable_mandatory_state(monkeypatch, state, entry):
    messages = [
        {'role': 'user', 'content': 'start'},
        {'role': 'assistant', 'content': 'work', '_reasoning_continuity': 'active_turn', **state},
        {'role': 'user', 'content': 'continue'},
    ]
    with pytest.raises(LocalRequestPreparationError, match='mandatory_continuity_unrepresentable'):
        if entry == 'prepare':
            prepare_request({'model': 'test-model', 'messages': messages}, base_url=URL)
        else:
            _capture_openai(monkeypatch, entry, messages)


def test_chat_conversion_reports_optional_foreign_reasoning_without_losing_work():
    from lib.llm.openai_outbound import openai_body_to_chat
    body = {'model': 'deepseek-v4.1-flash', 'messages': [
        {'role': 'assistant', 'content': 'portable work', 'reasoning_content': 'portable reasoning',
         '_responses_items': [{'type': 'reasoning', 'encrypted_content': 'foreign-ciphertext'}]},
    ]}
    original = deepcopy(body)
    projected = openai_body_to_chat(body)
    assert projected['messages'] == [{
        'role': 'assistant', 'content': 'portable work', 'reasoning_content': 'portable reasoning'}]
    assert projected['_conversion_report'] == {'cross_protocol_reasoning_omitted': 1}
    assert body == original


def test_chat_conversion_preserves_exact_flat_signed_representation():
    from lib.llm.openai_outbound import openai_body_to_chat
    msg = {'role': 'assistant', 'content': 'work', 'reasoning_content': 'native thought',
           'thinking_signature': 'opaque-signature', '_reasoning_continuity': 'active_turn',
           '_anthropic_content_blocks': [
               {'type': 'thinking', 'thinking': 'native thought', 'signature': 'opaque-signature'},
               {'type': 'text', 'text': 'work'},
           ]}
    projected = openai_body_to_chat({'model': 'claude-sonnet-4-6', 'messages': [msg]})
    assert projected['messages'][0] == {
        'role': 'assistant', 'content': 'work', 'reasoning_content': 'native thought',
        'thinking_signature': 'opaque-signature'}
    assert not projected.get('_conversion_report')


@pytest.mark.parametrize('continuity', ['active_turn', 'completed_turn'])
def test_flat_signed_replay_cannot_collapse_duplicate_native_blocks(continuity):
    from lib.llm.openai_outbound import openai_body_to_chat
    block = {'type': 'thinking', 'thinking': 'same thought', 'signature': 'opaque'}
    msg = {'role': 'assistant', 'content': 'work', 'reasoning_content': 'same thought',
           'thinking_signature': 'opaque', '_reasoning_continuity': continuity,
           '_anthropic_content_blocks': [deepcopy(block), deepcopy(block)]}
    with pytest.raises(LocalRequestPreparationError, match='mandatory_continuity_unrepresentable'):
        openai_body_to_chat({'messages': [msg]})


@pytest.mark.parametrize('entry', ['nonstream', 'sync', 'async'])
def test_complete_signed_sequence_survives_all_chat_entry_points(monkeypatch, entry):
    blocks = [
        {'type': 'thinking', 'thinking': 'first', 'signature': 'sig-1'},
        {'type': 'thinking', 'thinking': 'second', 'signature': 'sig-2'},
    ]
    messages = [
        {'role': 'user', 'content': 'start'},
        {'role': 'assistant', 'content': 'work', 'reasoning_content': 'first\nsecond',
         'thinking_signature': 'sig-2', 'reasoning_details': deepcopy(blocks),
         '_anthropic_content_blocks': [*deepcopy(blocks), {'type': 'text', 'text': 'work'}],
         '_reasoning_continuity': 'active_turn'},
        {'role': 'user', 'content': 'continue'},
    ]
    original = deepcopy(messages)
    wire = _capture_openai(monkeypatch, entry, messages, model='claude-sonnet-4-6')
    assert wire['messages'][1]['reasoning_details'] == blocks
    assert wire['messages'][1]['reasoning_content'] == 'first\nsecond'
    assert '_anthropic_content_blocks' not in wire['messages'][1]
    assert messages == original
