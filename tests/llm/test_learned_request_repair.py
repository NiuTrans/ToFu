"""Learned request repair: typed repairable 400s, same-slot retry, learned facts.

Executable specification for ``lib/llm/request_repair.py`` and its
classification in ``lib/llm/llm_errors.py`` (docs/modules/llm_io.md,
"Learned request repair"). No network: transports are deterministic fakes.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import logging
import time

import pytest

from lib.llm import request_repair
from lib.llm.llm_errors import (
    REPAIRABLE_PARAMETER_WIRE_NAMES,
    BadRequestError,
    RequestScopedError,
    ThinkingBlockBindingError,
    UnsupportedRequestParameterError,
    _classify_http_error,
)

pytestmark = pytest.mark.unit

ROUTE_A = 'route:v1:' + 'a' * 64
ROUTE_B = 'route:v1:' + 'b' * 64
NOW = 1_800_000_000.0
TTL = request_repair.OMISSION_TTL_S
BINDING_MESSAGE = (
    'messages.3.content.0: Invalid `signature` in `thinking` block. The block '
    'is bound to a different conversation. Remove thinking blocks that were '
    'produced for another conversation.')
TAMPERED_MESSAGE = 'messages.1.content.0: Invalid `signature` in `thinking` block'


@pytest.fixture(autouse=True)
def isolated_repair_state(monkeypatch, tmp_path):
    import lib.config_dir as config_dir

    path = tmp_path / 'server_config.json'
    real = config_dir.config_path
    monkeypatch.setattr(
        config_dir, 'config_path',
        lambda *parts: str(path) if parts == ('server_config.json',)
        else real(*parts))
    monkeypatch.setattr(request_repair, '_ROUTE_OMISSIONS',
                        request_repair._RouteOmissionStore())
    monkeypatch.setattr(request_repair, '_WITHDRAWN_THINKING',
                        request_repair._WithdrawnThinkingMemory())
    monkeypatch.setattr('lib.desktop.egress.route_request',
                        lambda *a, **k: 'direct')
    monkeypatch.setattr('lib.token_counter.count_tokens',
                        lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
        lambda *a, **k: 200000)
    return path


def _openai_error(message, *, param=None, code=None):
    return json.dumps({'error': {'message': message, 'type': 'invalid_request_error',
                                 'param': param, 'code': code}})


def _anthropic_error(message):
    return json.dumps({'type': 'error', 'request_id': 'req_1', 'error': {
        'type': 'invalid_request_error', 'message': message}})


# ── Classification tables ────────────────────────────────────────────────

UNSUPPORTED_POSITIVE = [
    ('openai-unsupported-code', _openai_error(
        "Unsupported parameter: 'temperature' is not supported with this model.",
        param='temperature', code='unsupported_parameter'),
     'temperature', 'unsupported_parameter'),
    ('openai-http-prefix', 'API HTTP 400: ' + _openai_error(
        "Unsupported parameter: 'top_p' is not supported with this model.",
        param='top_p', code='unsupported_parameter'),
     'top_p', 'unsupported_parameter'),
    ('openai-unknown-parameter', _openai_error(
        "Unknown parameter: 'seed'.", param='seed', code='unknown_parameter'),
     'seed', 'unknown_parameter'),
    ('json-escaped-double-quotes', _openai_error(
        'Unsupported parameter: "frequency_penalty" is not supported with this model.'),
     'frequency_penalty', 'invalid_request_error'),
    ('structured-param-only', _openai_error(
        'This model does not accept that field.',
        param='presence_penalty', code='unsupported_parameter'),
     'presence_penalty', 'unsupported_parameter'),
    ('not-supported-with-this-model', _openai_error(
        "'parallel_tool_calls' is not supported with this model."),
     'parallel_tool_calls', 'invalid_request_error'),
    ('reasoning-effort-named-explicitly', _openai_error(
        "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
        param='reasoning_effort', code='unsupported_parameter'),
     'reasoning_effort', 'unsupported_parameter'),
    ('logprobs', _openai_error(
        "Unsupported parameter: 'logprobs' is not supported with this model.",
        param='logprobs', code='unsupported_parameter'),
     'logprobs', 'unsupported_parameter'),
    ('anthropic-extra-input', _anthropic_error(
        'top_k: Extra inputs are not permitted'), 'top_k', 'invalid_request_error'),
    ('anthropic-stop-sequences', _anthropic_error(
        'stop_sequences: Extra inputs are not permitted'), 'stop', 'invalid_request_error'),
    ('anthropic-thinking-display', _anthropic_error(
        'thinking.display: Extra inputs are not permitted'),
     'thinking.display', 'invalid_request_error'),
    ('anthropic-tagged-thinking-display', _anthropic_error(
        'thinking.adaptive.display: Extra inputs are not permitted'),
     'thinking.display', 'invalid_request_error'),
    ('plain-text-body', 'temperature: Extra inputs are not permitted',
     'temperature', ''),
]

NOT_REPAIRABLE = [
    ('required-max-tokens', _openai_error(
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead.",
        param='max_tokens', code='unsupported_parameter')),
    ('required-messages', _anthropic_error('messages: Extra inputs are not permitted')),
    ('required-model', _anthropic_error('model: Extra inputs are not permitted')),
    ('required-tools', _anthropic_error('tools: Extra inputs are not permitted')),
    ('required-input', _openai_error(
        "Unknown parameter: 'input'.", param='input', code='unknown_parameter')),
    ('nested-path', _anthropic_error(
        'messages.0.content.0.temperature: Extra inputs are not permitted')),
    ('two-parameters', _anthropic_error(
        'temperature: Extra inputs are not permitted; '
        'top_p: Extra inputs are not permitted')),
    ('structured-param-disagrees', _openai_error(
        "Unsupported parameter: 'top_p' is not supported with this model.",
        param='temperature', code='unsupported_parameter')),
    ('unsupported-value', _openai_error(
        "Unsupported value: 'temperature' does not support 0.5 with this model. "
        'Only the default (1) value is supported.',
        param='temperature', code='unsupported_value')),
    ('outside-allowlist', _openai_error(
        "Unsupported parameter: 'top_logprobs' is not supported with this model.",
        param='top_logprobs', code='unsupported_parameter')),
    ('reasoning-effort-implied', _openai_error(
        "Unsupported parameter: 'reasoning.effort' is not supported with this model.",
        param='reasoning.effort', code='unsupported_parameter')),
    ('anthropic-output-effort', _anthropic_error(
        'output_config.effort: Extra inputs are not permitted')),
    ('whole-thinking-object', _anthropic_error(
        'thinking: Extra inputs are not permitted')),
    ('value-range', _openai_error(
        'temperature must be at most 1 for this model',
        param='temperature', code='invalid_value')),
    ('tampered-signature', _anthropic_error(TAMPERED_MESSAGE)),
]


@pytest.mark.parametrize(
    'body,param,code', [case[1:] for case in UNSUPPORTED_POSITIVE],
    ids=[case[0] for case in UNSUPPORTED_POSITIVE])
def test_unsupported_optional_parameter_is_typed(body, param, code):
    with pytest.raises(UnsupportedRequestParameterError) as raised:
        _classify_http_error(400, body, 'model-x', '[t]')
    assert raised.value.param == param
    assert raised.value.error_code == code
    assert isinstance(raised.value, BadRequestError)


@pytest.mark.parametrize(
    'body', [case[1] for case in NOT_REPAIRABLE],
    ids=[case[0] for case in NOT_REPAIRABLE])
def test_other_400s_keep_generic_bad_request(body):
    with pytest.raises(BadRequestError) as raised:
        _classify_http_error(400, body, 'model-x', '[t]')
    assert type(raised.value) is BadRequestError


def test_repairable_shapes_are_400_only():
    with pytest.raises(RequestScopedError):
        _classify_http_error(
            422, _anthropic_error('temperature: Extra inputs are not permitted'),
            'model-x', '[t]')


@pytest.mark.parametrize('body', [
    _anthropic_error(BINDING_MESSAGE),
    'API HTTP 400: ' + _anthropic_error(BINDING_MESSAGE),
    json.dumps({'error': {'type': 'invalid_request_error', 'message': BINDING_MESSAGE}}),
], ids=['http-json', 'http-prefixed', 'sse-embedded'])
def test_thinking_binding_mismatch_is_typed(body):
    with pytest.raises(ThinkingBlockBindingError) as raised:
        _classify_http_error(400, body, 'claude-x', '[t]')
    assert isinstance(raised.value, BadRequestError)


def test_vocabulary_is_closed_and_excludes_required_fields():
    assert (set(request_repair._PARAMETER_BODY_KEYS)
            | {request_repair.THINKING_DISPLAY}
            == request_repair.REPAIRABLE_REQUEST_PARAMETERS)
    required = {'model', 'messages', 'input', 'tools', 'max_tokens'}
    assert not required & set(REPAIRABLE_PARAMETER_WIRE_NAMES)
    assert not required & request_repair.REPAIRABLE_REQUEST_PARAMETERS


# ── Body transforms ──────────────────────────────────────────────────────

def test_omission_removes_only_the_named_optional_state():
    thinking = {'type': 'adaptive', 'display': 'summarized'}
    body = {'model': 'm', 'messages': [], 'temperature': 0.2, 'stop': ['x'],
            'stop_sequences': ['y'], 'logprobs': True, 'top_logprobs': 2,
            'thinking': thinking}
    assert request_repair.omit_request_parameter(body, 'stop')
    assert request_repair.omit_request_parameter(body, 'logprobs')
    assert request_repair.omit_request_parameter(body, 'thinking.display')
    assert not request_repair.omit_request_parameter(body, 'seed')
    assert body == {'model': 'm', 'messages': [], 'temperature': 0.2,
                    'thinking': {'type': 'adaptive'}}
    assert thinking == {'type': 'adaptive', 'display': 'summarized'}


def test_parameter_repair_runs_once_and_only_when_it_changes_the_body():
    state = request_repair.RequestRepairState()
    error = UnsupportedRequestParameterError('x', param='temperature')
    body = {'model': 'm', 'temperature': 1, '_route_output_limit_key': ROUTE_A}
    assert request_repair.repair_rejected_request(body, error, state, now=NOW)
    assert 'temperature' not in body
    body['temperature'] = 1
    assert request_repair.repair_rejected_request(body, error, state, now=NOW) == ''
    absent = UnsupportedRequestParameterError('x', param='seed')
    assert request_repair.repair_rejected_request(body, absent, state, now=NOW) == ''
    assert request_repair._ROUTE_OMISSIONS.omitted_parameters(ROUTE_A, NOW) == (
        'temperature',)


# ── Fake transports ──────────────────────────────────────────────────────

OK_SSE = [
    'data: {"choices":[{"delta":{"role":"assistant","content":"ok"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":3,"completion_tokens":1}}',
    'data: [DONE]',
]
TEMPERATURE_400 = (400, [_openai_error(
    "Unsupported parameter: 'temperature' is not supported with this model.",
    param='temperature', code='unsupported_parameter')])
OK_200 = (200, OK_SSE)


def _payload(kwargs):
    if 'json' in kwargs:
        return json.loads(json.dumps(dict(kwargs['json'])))
    return json.loads(kwargs.get('data') or kwargs.get('content'))


class _SyncResp:
    def __init__(self, status, lines):
        self.status_code = status
        self.headers = {}
        self.encoding = 'utf-8'
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def iter_content(self, chunk_size=64 << 10):
        for line in self._lines:
            yield line.encode('utf-8') + b'\n\n'

    def close(self):
        pass


def _run_sync_stream(monkeypatch, body, responses):
    import lib.llm.stream as smod

    queue, posted, restarts = list(responses), [], []

    def post(url, **kwargs):
        posted.append(_payload(kwargs))
        return _SyncResp(*queue.pop(0))

    class _Session:
        pass

    _Session.post = staticmethod(post)
    monkeypatch.setattr(smod, 'get_sync_session', lambda: _Session())
    monkeypatch.setattr(smod, 'abortable_sleep', lambda *a, **k: None)
    try:
        result = smod.stream_chat(
            body, log_prefix='[t]',
            on_attempt_restart=lambda reason='': restarts.append(reason))
    except Exception as error:
        return error, posted, restarts
    return result, posted, restarts


class _AsyncResp:
    def __init__(self, status, lines):
        self.status_code = status
        self.headers = {}
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aiter_bytes(self):
        for line in self._lines:
            yield line.encode('utf-8') + b'\n\n'

    async def aread(self):
        return b''.join(line.encode('utf-8') for line in self._lines)


def _run_async_stream(monkeypatch, body, responses):
    import lib.llm.astream as amod

    queue, posted, restarts = list(responses), [], []

    class _Client:
        def stream(self, method, url, **kwargs):
            posted.append(_payload(kwargs))
            return _AsyncResp(*queue.pop(0))

    monkeypatch.setattr(amod, 'get_async_client', lambda *a, **k: _Client())

    async def run():
        return await amod.async_stream_chat(
            body, log_prefix='[t]',
            on_attempt_restart=lambda reason='': restarts.append(reason))

    try:
        result = asyncio.run(run())
    except Exception as error:
        return error, posted, restarts
    return result, posted, restarts


RUNNERS = [_run_sync_stream, _run_async_stream]


def _stream_body(route=ROUTE_A, **extra):
    return {'model': 'gpt-test', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 64, 'temperature': 0.3,
            '_route_output_limit_key': route, **extra}


# ── Part A: stream sync + async ──────────────────────────────────────────

@pytest.mark.parametrize('run', RUNNERS, ids=['sync', 'async'])
def test_stream_retries_once_without_rejected_parameter(
        monkeypatch, isolated_repair_state, run):
    result, posted, restarts = run(
        monkeypatch, _stream_body(), [TEMPERATURE_400, OK_200])
    message, finish, _usage = result
    assert (message['content'], finish) == ('ok', 'stop')
    assert len(posted) == 2
    assert posted[0]['temperature'] == 0.3
    assert 'temperature' not in posted[1]
    assert restarts == ['request repair: omit unsupported parameter temperature']
    fact = request_repair._ROUTE_OMISSIONS.facts_for(ROUTE_A, time.time())
    assert fact['temperature']['count'] == 1
    assert fact['temperature']['error_code'] == 'unsupported_parameter'
    persisted = json.loads(isolated_repair_state.read_text(encoding='utf-8'))
    assert set(persisted['route_request_omissions'][ROUTE_A]) == {'temperature'}


LIMIT_400 = (400, [_openai_error('max_tokens must be at most 4096',
                                 param='max_tokens', code='invalid_value')])


@pytest.mark.parametrize('run', RUNNERS, ids=['sync', 'async'])
@pytest.mark.parametrize('rejection', [TEMPERATURE_400, LIMIT_400],
                         ids=['repair', 'clamp'])
def test_status_line_rejection_retry_is_admitted_under_a_finite_budget(
        monkeypatch, run, rejection):
    """A non-200 rejection ran no prompt; its retry must not be fenced."""
    from lib.agent_core.execution_budget import model_budget_admission

    monkeypatch.setattr('lib.model_info._learn_model_limit', lambda *a, **k: None)
    owner = {'id': 'stream-budget', 'config': {'executionLimits': {
        'max_tokens': 100_000, 'max_tool_calls': 0,
        'deadline': time.time() + 60}}}
    body = _stream_body(max_tokens=8192,
                        _model_budget_admit=model_budget_admission(owner))
    result, posted, restarts = run(monkeypatch, body, [rejection, OK_200])
    message, finish, _usage = result
    assert (message['content'], finish) == ('ok', 'stop')
    assert len(posted) == 2 and len(restarts) == 1
    assert owner['_executionBudget'].consumption_snapshot() == (4, 0, True)


STREAMED_OUTPUT = {
    'content': 'data: {"choices":[{"delta":{"role":"assistant",'
               '"content":"partial"}}]}',
    'thinking': 'data: {"choices":[{"delta":{"reasoning_content":"weighing"}}]}',
    'tool': 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"id":"call_1","type":"function","function":'
            '{"name":"lookup","arguments":"{}"}}]}}]}',
}
REJECTION_FRAMES = {
    'unsupported-parameter': 'data: ' + TEMPERATURE_400[1][0],
    'thinking-binding': 'data: ' + json.dumps({'error': {
        'type': 'invalid_request_error', 'message': BINDING_MESSAGE}}),
}


@pytest.mark.parametrize('run', RUNNERS, ids=['sync', 'async'])
@pytest.mark.parametrize('output', sorted(STREAMED_OUTPUT))
@pytest.mark.parametrize('rejection', sorted(REJECTION_FRAMES))
def test_rejection_after_streamed_output_is_never_repaired(
        monkeypatch, run, output, rejection):
    """Generated output means the provider ran: no restrict-only retry."""
    body = _stream_body(_conv_id='conv-out', messages=_thinking_chat_history())
    frames = [STREAMED_OUTPUT[output], REJECTION_FRAMES[rejection]]
    error, posted, restarts = run(monkeypatch, body, [(200, frames), OK_200])
    assert type(error) is BadRequestError
    assert len(posted) == 1 and restarts == []
    assert request_repair._ROUTE_OMISSIONS.facts_for(ROUTE_A, time.time()) == {}
    assert request_repair._WITHDRAWN_THINKING.digests(
        'conv:conv-out', time.time()) == frozenset()


@pytest.mark.parametrize('run', RUNNERS, ids=['sync', 'async'])
def test_rejection_frame_before_any_output_is_still_repaired(monkeypatch, run):
    result, posted, restarts = run(monkeypatch, _stream_body(), [
        (200, [REJECTION_FRAMES['unsupported-parameter']]), OK_200])
    assert result[0]['content'] == 'ok'
    assert len(posted) == 2 and 'temperature' not in posted[1]
    assert restarts == ['request repair: omit unsupported parameter temperature']


@pytest.mark.parametrize('run', RUNNERS, ids=['sync', 'async'])
def test_stream_does_not_retry_a_repeated_rejection(monkeypatch, run):
    error, posted, restarts = run(
        monkeypatch, _stream_body(), [TEMPERATURE_400, TEMPERATURE_400, OK_200])
    assert isinstance(error, UnsupportedRequestParameterError)
    assert len(posted) == 2
    assert len(restarts) == 1


@pytest.mark.parametrize('run', RUNNERS, ids=['sync', 'async'])
def test_learned_omission_shapes_next_request_for_same_deployment_only(
        monkeypatch, run):
    request_repair._ROUTE_OMISSIONS.learn(
        ROUTE_A, 'temperature', 'unsupported_parameter', time.time())

    same_route = _stream_body(ROUTE_A)
    _result, posted, restarts = run(monkeypatch, same_route, [OK_200])
    assert len(posted) == 1 and 'temperature' not in posted[0]
    assert restarts == []
    assert same_route['temperature'] == 0.3

    _result, posted, _restarts = run(monkeypatch, _stream_body(ROUTE_B), [OK_200])
    assert posted[0]['temperature'] == 0.3


@pytest.mark.parametrize('run', RUNNERS, ids=['sync', 'async'])
def test_model_limit_on_the_final_attempt_raises_the_typed_error(monkeypatch, run):
    from lib.llm._transport import MAX_STREAM_RETRIES
    from lib.llm.llm_errors import ModelLimitError

    monkeypatch.setattr('lib.model_info._learn_model_limit', lambda *a, **k: None)
    limit_400 = (400, [_openai_error('max_tokens must be at most 4096',
                                     param='max_tokens', code='invalid_value')])
    attempts = 1 + MAX_STREAM_RETRIES
    error, posted, restarts = run(
        monkeypatch, _stream_body(max_tokens=8192), [limit_400] * attempts)
    assert isinstance(error, ModelLimitError)
    assert len(posted) == attempts
    assert posted[-1].get('max_tokens', posted[-1].get('max_completion_tokens')) == 4096
    assert len(restarts) == MAX_STREAM_RETRIES


@pytest.mark.parametrize('run', RUNNERS, ids=['sync', 'async'])
def test_tampered_signature_is_not_repaired(monkeypatch, run):
    body = _stream_body(_conv_id='conv-t', messages=_thinking_chat_history())
    error, posted, restarts = run(
        monkeypatch, body, [(400, [_anthropic_error(TAMPERED_MESSAGE)]), OK_200])
    assert type(error) is BadRequestError
    assert len(posted) == 1 and restarts == []
    assert request_repair._WITHDRAWN_THINKING.digests(
        'conv:conv-t', time.time()) == frozenset()


# ── Part A: non-streaming chat ───────────────────────────────────────────

class _ChatResp:
    def __init__(self, status, payload):
        self.status_code = status
        self.headers = {}
        self.encoding = 'utf-8'
        self._payload = payload
        self.content = json.dumps(payload).encode('utf-8')

    def json(self):
        return self._payload


def _completion(text='ok'):
    return {'choices': [{'message': {'role': 'assistant', 'content': text},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 1}}


def _run_chat(monkeypatch, responses, **kwargs):
    chat_module = importlib.import_module('lib.llm.chat')
    queue, posted = list(responses), []

    def post(url, **post_kwargs):
        posted.append(_payload(post_kwargs))
        return queue.pop(0)

    monkeypatch.setattr(chat_module, 'http_post', post)
    call = dict(model='gpt-test', temperature=0.4, api_key='synthetic',
                base_url='https://example.invalid/v1', provider_id='provider-x',
                max_retries=0)
    call.update(kwargs)
    messages = call.pop('messages', [{'role': 'user', 'content': 'hi'}])
    try:
        return chat_module.chat(messages, **call), posted
    except Exception as error:
        return error, posted


def test_nonstream_chat_retries_once_without_rejected_parameter(monkeypatch):
    rejection = _ChatResp(400, json.loads(TEMPERATURE_400[1][0]))
    (content, _usage), posted = _run_chat(
        monkeypatch, [rejection, _ChatResp(200, _completion())])
    assert content == 'ok'
    assert len(posted) == 2
    assert posted[0]['temperature'] == 0.4 and 'temperature' not in posted[1]
    fallback_key = request_repair.request_fact_route_key(
        {'model': 'gpt-test'}, provider_id='provider-x')
    assert request_repair._ROUTE_OMISSIONS.omitted_parameters(
        fallback_key, time.time()) == ('temperature',)

    (content, _usage), posted = _run_chat(
        monkeypatch, [_ChatResp(200, _completion())])
    assert content == 'ok' and 'temperature' not in posted[0]


BOUNDED_PARAMETERS = ('temperature', 'top_p', 'presence_penalty',
                      'frequency_penalty', 'seed', 'stop')


def _unsupported(param):
    return [_openai_error(
        f"Unsupported parameter: '{param}' is not supported with this model.",
        param=param, code='unsupported_parameter')]


def test_nonstream_repairs_stop_at_the_stream_retry_bound(monkeypatch):
    """One rejected parameter per send must not buy one send per vocabulary entry."""
    from lib.llm._transport import MAX_STREAM_RETRIES

    assert len(BOUNDED_PARAMETERS) > MAX_STREAM_RETRIES + 1
    optional = {'top_p': 0.9, 'presence_penalty': 0.1, 'frequency_penalty': 0.1,
                'seed': 7, 'stop': ['END']}
    chat_rejections = [_ChatResp(400, json.loads(_unsupported(param)[0]))
                       for param in BOUNDED_PARAMETERS]
    error, posted = _run_chat(
        monkeypatch, chat_rejections + [_ChatResp(200, _completion())],
        extra=optional)
    assert all(param in posted[0] for param in BOUNDED_PARAMETERS)
    assert isinstance(error, UnsupportedRequestParameterError)
    assert error.param == BOUNDED_PARAMETERS[MAX_STREAM_RETRIES]
    assert len(posted) == 1 + MAX_STREAM_RETRIES

    stream_rejections = [(400, _unsupported(param)) for param in BOUNDED_PARAMETERS]
    error, stream_posted, _restarts = _run_sync_stream(
        monkeypatch, _stream_body(**optional), stream_rejections + [OK_200])
    assert isinstance(error, UnsupportedRequestParameterError)
    assert len(stream_posted) == len(posted)


def test_nonstream_chat_does_not_retry_a_repeated_rejection(monkeypatch):
    rejection = json.loads(TEMPERATURE_400[1][0])
    error, posted = _run_chat(monkeypatch, [
        _ChatResp(400, rejection), _ChatResp(400, rejection),
        _ChatResp(200, _completion())])
    assert isinstance(error, UnsupportedRequestParameterError)
    assert len(posted) == 2


# ── One Deployment route key for stream and non-stream dispatch ─────────

class _OneSlotDispatcher:
    slots = []

    def __init__(self, slot):
        self._slot = slot

    def pick_and_reserve(self, **kwargs):
        slot, self._slot = self._slot, None
        if slot is not None:
            slot.record_request()
        return slot

    def has_capable_slots(self, *args, **kwargs):
        return self._slot is not None

    def summarize_slots(self, *args, **kwargs):
        return 'one-slot'


def _deployment_slot():
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name='k0', api_key='synthetic', model='gpt-test',
                capabilities={'text'}, provider_id='provider-x',
                base_url='https://example.invalid/v1',
                route_offering_id='offering-x',
                route_deployment_id='deployment-x')


def _stream_dispatch_body():
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
    return _adapt_stream_body_for_slot(
        _deployment_slot(), [{'role': 'user', 'content': 'hi'}], False,
        tools=None, max_tokens=64, temperature=0.4, thinking_enabled=None,
        preset='low', effort=None)


def _dispatch_chat(monkeypatch, responses):
    from lib.llm_dispatch import api

    queue, posted = list(responses), []

    def post(url, **kwargs):
        posted.append(_payload(kwargs))
        return queue.pop(0)

    monkeypatch.setattr(importlib.import_module('lib.llm.chat'), 'http_post', post)
    monkeypatch.setattr(api, 'get_dispatcher',
                        lambda: _OneSlotDispatcher(_deployment_slot()))
    content, _usage = api.dispatch_chat(
        [{'role': 'user', 'content': 'hi'}], temperature=0.4, log_prefix='[t]')
    return content, posted


def test_nonstream_dispatch_learns_on_the_stream_route_key(monkeypatch):
    rejection = _ChatResp(400, json.loads(TEMPERATURE_400[1][0]))
    content, posted = _dispatch_chat(
        monkeypatch, [rejection, _ChatResp(200, _completion())])
    assert content == 'ok' and len(posted) == 2
    assert posted[0]['temperature'] == 0.4 and 'temperature' not in posted[1]

    stream_body = _stream_dispatch_body()
    route_key = stream_body['_route_output_limit_key']
    assert route_key.startswith('route:v1:')
    assert request_repair._ROUTE_OMISSIONS.omitted_parameters(
        route_key, time.time()) == ('temperature',)
    _result, stream_posted, restarts = _run_sync_stream(
        monkeypatch, stream_body, [OK_200])
    assert 'temperature' not in stream_posted[0] and restarts == []


def test_stream_dispatch_fact_shapes_the_nonstream_request(monkeypatch):
    stream_body = _stream_dispatch_body()
    result, stream_posted, _restarts = _run_sync_stream(
        monkeypatch, stream_body, [TEMPERATURE_400, OK_200])
    assert result[0]['content'] == 'ok' and len(stream_posted) == 2

    content, posted = _dispatch_chat(monkeypatch, [_ChatResp(200, _completion())])
    assert content == 'ok' and len(posted) == 1
    assert 'temperature' not in posted[0]


# ── Durable facts: TTL, bounds, load/sanitize/repair ─────────────────────

def test_learned_omission_expires_and_is_relearned(caplog):
    store = request_repair._ROUTE_OMISSIONS
    with caplog.at_level(logging.INFO):
        assert store.learn(ROUTE_A, 'temperature', 'unsupported_parameter', NOW)
        assert not store.learn(ROUTE_A, 'temperature', 'unsupported_parameter', NOW + 5)
    learned_lines = [record for record in caplog.records
                     if 'Learned request omission' in record.getMessage()]
    assert len(learned_lines) == 1
    assert store.omitted_parameters(ROUTE_A, NOW + 5 + TTL) == ('temperature',)
    assert store.omitted_parameters(ROUTE_A, NOW + 6 + TTL) == ()
    assert store.learn(ROUTE_A, 'temperature', '', NOW + 7 + TTL)
    assert store.facts_for(ROUTE_A, NOW + 7 + TTL)['temperature']['count'] == 1


def test_learn_persists_provenance_and_preserves_other_config(
        isolated_repair_state):
    path = isolated_repair_state
    path.write_text(json.dumps({'unrelated': 7,
                                'route_model_limits': {ROUTE_A: 1000}}))
    store = request_repair._RouteOmissionStore()
    store.learn(ROUTE_A, 'temperature', 'unsupported_parameter', NOW)
    store.learn(ROUTE_A, 'temperature', 'unsupported_parameter', NOW + 5)
    persisted = json.loads(path.read_text(encoding='utf-8'))
    assert persisted['unrelated'] == 7
    assert persisted['route_model_limits'] == {ROUTE_A: 1000}
    assert persisted['route_request_omissions'] == {ROUTE_A: {'temperature': {
        'param': 'temperature', 'first_seen': NOW, 'last_seen': NOW + 5,
        'count': 2, 'error_code': 'unsupported_parameter'}}}
    reloaded = request_repair._RouteOmissionStore()
    assert reloaded.omitted_parameters(ROUTE_A, NOW + 6) == ('temperature',)


def test_load_sanitizes_and_repairs_persisted_facts(isolated_repair_state):
    path = isolated_repair_state
    now = time.time()
    valid = {'param': 'temperature', 'first_seen': now - 60,
             'last_seen': now - 30, 'count': 2,
             'error_code': 'unsupported_parameter'}
    path.write_text(json.dumps({
        'unrelated': {'kept': True},
        'route_request_omissions': {
            ROUTE_A: {
                'temperature': valid,
                'max_tokens': {**valid, 'param': 'max_tokens'},
                'top_p': {**valid, 'param': 'top_p',
                          'first_seen': now - TTL - 20,
                          'last_seen': now - TTL - 10},
                'seed': {**valid, 'param': 'top_k'},
                'stop': {**valid, 'param': 'stop', 'count': True},
                'top_k': {**valid, 'param': 'top_k', 'last_seen': now + 86400},
                'logprobs': {**valid, 'param': 'logprobs',
                             'error_code': 'not a code!'},
            },
            ' padded ': {'temperature': valid},
            'r' * 81: {'temperature': valid},
            ROUTE_B: 'not-a-map',
        },
    }), encoding='utf-8')

    store = request_repair._RouteOmissionStore()
    assert store.omitted_parameters(ROUTE_A, now) == ('logprobs', 'temperature')
    repaired = json.loads(path.read_text(encoding='utf-8'))
    assert repaired['unrelated'] == {'kept': True}
    assert set(repaired['route_request_omissions']) == {ROUTE_A}
    route = repaired['route_request_omissions'][ROUTE_A]
    assert set(route) == {'temperature', 'logprobs'}
    assert route['logprobs']['error_code'] == ''


def test_missing_map_is_not_created_on_load(isolated_repair_state):
    path = isolated_repair_state
    path.write_text(json.dumps({'unrelated': 1}), encoding='utf-8')
    assert request_repair._RouteOmissionStore().omitted_parameters(
        ROUTE_A, NOW) == ()
    assert json.loads(path.read_text(encoding='utf-8')) == {'unrelated': 1}


def test_durable_write_runs_outside_the_lock_and_stays_monotonic(
        monkeypatch, isolated_repair_state):
    """A slow FUSE write must not stall other learners or roll facts back.

    Writer A persists first but returns last with a view that predates B.
    """
    import threading
    import lib.json_store as json_store

    file_lock, document = threading.Lock(), {}
    a_written, release_a = threading.Event(), threading.Event()

    def update_json_atomic(path, mutate, *, default=None, **_kwargs):
        with file_lock:
            document.update(mutate(copy.deepcopy(document) or default))
            stored = copy.deepcopy(document)
        if threading.current_thread().name == 'writer-a':
            a_written.set()
            assert release_a.wait(10)
        return stored

    monkeypatch.setattr(json_store, 'update_json_atomic', update_json_atomic)
    store = request_repair._RouteOmissionStore()
    writer_a = threading.Thread(name='writer-a', target=store.learn, args=(
        ROUTE_A, 'temperature', 'unsupported_parameter', NOW))
    writer_a.start()
    assert a_written.wait(10)
    writer_b = threading.Thread(target=store.learn, args=(
        ROUTE_B, 'top_p', 'unsupported_parameter', NOW + 1))
    writer_b.start()
    writer_b.join(5)
    blocked = writer_b.is_alive()
    release_a.set()
    writer_a.join(10)
    writer_b.join(10)
    assert not blocked, 'learn() held the store lock across a durable write'
    assert store.omitted_parameters(ROUTE_A, NOW + 2) == ('temperature',)
    assert store.omitted_parameters(ROUTE_B, NOW + 2) == ('top_p',)
    assert set(document['route_request_omissions']) == {ROUTE_A, ROUTE_B}


def test_capacity_keeps_routes_with_newest_evidence():
    def fact(seen):
        return {'temperature': {'param': 'temperature', 'first_seen': seen,
                                'last_seen': seen, 'count': 1, 'error_code': ''}}

    cleaned, changed = request_repair._sanitize_route_omissions(
        {ROUTE_A: fact(NOW - 30), ROUTE_B: fact(NOW - 10),
         'route:v1:' + 'c' * 64: fact(NOW - 20)}, now=NOW, max_routes=2)
    assert changed
    assert list(cleaned) == ['route:v1:' + 'c' * 64, ROUTE_B]


# ── Part B: preserved-thinking binding mismatch ──────────────────────────

def _thinking_chat_history():
    """Actual public signed carriers for a Chat-compatible Claude gateway.

    Native Messages fixtures below include redacted state with no Chat binding.
    Such requests now fail before I/O and cannot exercise provider-side repair.
    """
    messages = []
    for index in (1, 2):
        messages.extend([
            {'role': 'user', 'content': f'q{index}'},
            {'role': 'assistant', 'content': f'a{index}',
             'reasoning_content': f'r{index}', 'thinking_signature': f'sig-{index}',
             'reasoning_details': [{'type': 'thinking', 'thinking': f'r{index}',
                                    'signature': f'sig-{index}'}]},
        ])
    return [*messages, {'role': 'user', 'content': 'q3'}]


def _thinking_history():
    return [
        {'role': 'user', 'content': 'q1'},
        {'role': 'assistant', 'content': 'a1', 'reasoning_content': 'r1',
         'thinking_signature': 'sig-1',
         'reasoning_details': [{'type': 'reasoning.text', 'text': 'r1'}],
         '_anthropic_content_blocks': [
             {'type': 'thinking', 'thinking': 'r1', 'signature': 'sig-1'},
             {'type': 'redacted_thinking', 'data': 'opaque-1'},
             {'type': 'text', 'text': 'a1'}]},
        {'role': 'user', 'content': 'q2'},
        {'role': 'assistant', 'content': 'a2', 'reasoning_content': 'r2',
         'thinking_signature': 'sig-2',
         '_anthropic_content_blocks': [
             {'type': 'thinking', 'thinking': 'r2', 'signature': 'sig-2'}]},
        {'role': 'user', 'content': 'q3'},
    ]


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).digest()


def test_binding_repair_remembers_details_without_flat_signature_or_native_blocks():
    signed = {'type': 'thinking', 'thinking': 'thought', 'signature': 'sig-details'}
    redacted = {'type': 'redacted_thinking', 'data': 'redacted-details'}
    original = {'model': 'claude-sonnet-4-5', '_conv_id': 'details-only', 'messages': [
        {'role': 'user', 'content': 'question'},
        {'role': 'assistant', 'content': 'work', 'reasoning_content': 'thought',
         'reasoning_details': [signed, redacted]},
    ]}
    body = copy.deepcopy(original)
    assert request_repair.repair_rejected_request(
        body, ThinkingBlockBindingError(BINDING_MESSAGE),
        request_repair.RequestRepairState(), now=NOW)
    assert request_repair._WITHDRAWN_THINKING.digests('conv:details-only', NOW) == {
        _sha('sig-details'), _sha('redacted-details')}
    later = copy.deepcopy(original)
    later['messages'].extend([
        {'role': 'user', 'content': 'next question'},
        {'role': 'assistant', 'content': 'new work', 'reasoning_content': 'new thought',
         'reasoning_details': [{'type': 'thinking', 'thinking': 'new thought',
                                'signature': 'fresh-sig'}]},
    ])
    fresh = copy.deepcopy(later['messages'][-1])
    request_repair.apply_learned_request_facts(later, now=NOW + 1)
    assert later['messages'][1] == {'role': 'assistant', 'content': 'work'}
    assert later['messages'][-1] == fresh


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('wire_model', ['deepseek-v4.1-flash', 'opaque-deployment'])
def test_learned_signature_withdrawal_keeps_required_reasoning_across_wire_routes(protocol, wire_model):
    from lib.llm._sse_core import prepare_request
    history = _thinking_chat_history()
    rejected = {'model': 'claude-sonnet-4-5', '_conv_id': 'required-reasoning',
                'messages': copy.deepcopy(history)}
    assert request_repair.repair_rejected_request(
        rejected, ThinkingBlockBindingError(BINDING_MESSAGE),
        request_repair.RequestRepairState())
    body = {'model': wire_model, '_admission_logical_model': 'deepseek-v4.1-flash',
            '_conv_id': 'required-reasoning', 'messages': copy.deepcopy(history)}
    original = copy.deepcopy(body)
    plan = prepare_request(body, api_protocol=protocol, base_url='https://example.invalid/v1')
    assistants = [msg for msg in plan.body['messages'] if msg['role'] == 'assistant']
    if protocol == 'anthropic':
        thoughts = [block['thinking'] for msg in assistants for block in msg['content']
                    if block['type'] == 'thinking']
        assert not any('signature' in block for msg in assistants for block in msg['content'])
    else:
        thoughts = [msg['reasoning_content'] for msg in assistants]
        assert not any(_has_signed_thinking(msg) for msg in assistants)
    assert thoughts == ['r1', 'r2']
    assert body == original


def _has_thinking_state(message):
    return (any(key in message for key in (
        'thinking_signature', 'reasoning_content', 'reasoning_details'))
        or any(block.get('type') in ('thinking', 'redacted_thinking')
               for block in message.get('_anthropic_content_blocks') or []))


def _has_signed_thinking(message):
    return ('thinking_signature' in message
            or any(block.get('type') in ('thinking', 'redacted_thinking')
                   for block in message.get('_anthropic_content_blocks') or [])
            or any(detail.get('signature')
                   for detail in message.get('reasoning_details') or []))


def test_binding_repair_strips_exactly_the_thinking_state():
    history = _thinking_history()
    pristine = copy.deepcopy(history)
    body = {'model': 'claude-x', 'messages': history, '_conv_id': 'conv-1'}
    state = request_repair.RequestRepairState()
    error = ThinkingBlockBindingError(BINDING_MESSAGE)

    assert request_repair.repair_rejected_request(body, error, state, now=NOW)
    assert history == pristine
    repaired = body['messages']
    assert repaired[1] == {'role': 'assistant', 'content': 'a1',
                           '_anthropic_content_blocks': [
                               {'type': 'text', 'text': 'a1'}]}
    assert repaired[3] == {'role': 'assistant', 'content': 'a2'}
    assert all(repaired[i] is history[i] for i in (0, 2, 4))
    digests = request_repair._WITHDRAWN_THINKING.digests('conv:conv-1', NOW)
    assert digests == {_sha('sig-1'), _sha('opaque-1'), _sha('sig-2')}
    assert request_repair.repair_rejected_request(body, error, state, now=NOW) == ''


def test_binding_repair_without_thinking_state_is_not_retried():
    body = {'model': 'claude-x', 'messages': [{'role': 'user', 'content': 'q'}]}
    state = request_repair.RequestRepairState()
    assert request_repair.repair_rejected_request(
        body, ThinkingBlockBindingError('x'), state, now=NOW) == ''
    assert not state.thinking_withdrawn


def test_withdrawn_blocks_stay_out_but_later_blocks_stay():
    first = {'model': 'claude-x', 'messages': _thinking_history(),
             '_conv_id': 'conv-1'}
    request_repair.repair_rejected_request(
        first, ThinkingBlockBindingError('x'),
        request_repair.RequestRepairState(), now=NOW)

    fresh = {'role': 'assistant', 'content': 'a3', 'reasoning_content': 'r3',
             'thinking_signature': 'sig-3', '_anthropic_content_blocks': [
                 {'type': 'thinking', 'thinking': 'r3', 'signature': 'sig-3'},
                 {'type': 'text', 'text': 'a3'}]}
    later = {'model': 'claude-x', '_conv_id': 'conv-1',
             'messages': _thinking_history() + [copy.deepcopy(fresh),
                                                {'role': 'user', 'content': 'q4'}]}
    request_repair.apply_learned_request_facts(later, now=NOW + 60)
    assert not _has_signed_thinking(later['messages'][1])
    assert not _has_signed_thinking(later['messages'][3])
    # Plain reasoning is not Anthropic protocol state; other routes replay it.
    assert later['messages'][1]['reasoning_content'] == 'r1'
    assert later['messages'][1]['reasoning_details'] == [
        {'type': 'reasoning.text', 'text': 'r1'}]
    assert later['messages'][3]['reasoning_content'] == 'r2'
    assert later['messages'][5] == fresh

    other = {'model': 'claude-x', '_conv_id': 'conv-2',
             'messages': _thinking_history()}
    request_repair.apply_learned_request_facts(other, now=NOW + 60)
    assert other['messages'] == _thinking_history()

    expired = {'model': 'claude-x', '_conv_id': 'conv-1',
               'messages': _thinking_history()}
    request_repair.apply_learned_request_facts(
        expired, now=NOW + 61 + request_repair.WITHDRAWN_THINKING_TTL_S)
    assert expired['messages'] == _thinking_history()


def test_withdrawn_blocks_never_reach_the_anthropic_wire():
    from lib.llm._sse_core import prepare_request

    request_repair._WITHDRAWN_THINKING.remember(
        'conv:conv-1', [_sha('sig-1'), _sha('opaque-1')], time.time())
    history = _thinking_history()[:3]
    history.append({'role': 'assistant', 'content': 'a3',
                    'reasoning_content': 'r3', 'thinking_signature': 'sig-3'})
    history.append({'role': 'user', 'content': 'q4'})
    body = {'model': 'claude-sonnet-4-5', 'messages': history, 'max_tokens': 64,
            '_conv_id': 'conv-1'}
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol='anthropic')
    wire = json.loads(plan.body.payload)
    assistant = [message['content'] for message in wire['messages']
                 if message['role'] == 'assistant']
    first_types = [block['type'] for block in assistant[0]]
    assert 'thinking' not in first_types and 'redacted_thinking' not in first_types
    assert assistant[1][0] == {'type': 'thinking', 'thinking': 'r3',
                               'signature': 'sig-3'}
    assert body['messages'] is history and history[1]['thinking_signature'] == 'sig-1'


@pytest.mark.parametrize('run', RUNNERS, ids=['sync', 'async'])
def test_withdrawal_keeps_reasoning_for_a_later_non_anthropic_fallback(
        monkeypatch, run):
    claude = {**_stream_body(_conv_id='conv-fb', messages=_thinking_chat_history()),
              'model': 'claude-sonnet-4-5'}
    result, _posted, restarts = run(monkeypatch, claude, [
        (400, [_anthropic_error(BINDING_MESSAGE)]), OK_200])
    assert result[0]['content'] == 'ok'
    assert restarts == ['request repair: withdraw bound thinking blocks']

    fallback = {**_stream_body(_conv_id='conv-fb', messages=_thinking_chat_history()),
                'model': 'deepseek-v4-flash'}
    _result, posted, _restarts = run(monkeypatch, fallback, [OK_200])
    replayed = [message for message in posted[0]['messages']
                if message['role'] == 'assistant']
    assert [message.get('reasoning_content') for message in replayed] == ['r1', 'r2']
    assert not any(_has_signed_thinking(message) for message in replayed)


def test_withdrawal_still_strips_signed_reasoning_on_a_claude_compat_route(
        monkeypatch):
    """build_body projects signed Claude reasoning into reasoning_details."""
    request_repair._WITHDRAWN_THINKING.remember(
        'conv:conv-cc', [_sha('sig-1')], time.time())
    history = [
        {'role': 'user', 'content': 'q1'},
        {'role': 'assistant', 'content': 'a1', 'reasoning_content': 'r1',
         'thinking_signature': 'sig-1'},
        {'role': 'user', 'content': 'q2'},
    ]
    (content, _usage), posted = _run_chat(
        monkeypatch, [_ChatResp(200, _completion())], model='claude-sonnet-4-5',
        messages=history, extra={'_conv_id': 'conv-cc'})
    assert content == 'ok'
    replayed = posted[0]['messages'][1]
    assert replayed['role'] == 'assistant'
    assert not {'reasoning_content', 'reasoning_details',
                'thinking_signature'} & set(replayed)


def test_nonstream_chat_withdraws_thinking_once_then_succeeds(monkeypatch):
    history = [
        {'role': 'user', 'content': 'q1'},
        {'role': 'assistant', 'content': 'a1', 'reasoning_content': 'r1',
         'thinking_signature': 'sig-1'},
        {'role': 'user', 'content': 'q2'},
    ]
    pristine = copy.deepcopy(history)
    reply = {'id': 'msg_1', 'type': 'message', 'role': 'assistant',
             'model': 'claude-sonnet-4-5', 'stop_reason': 'end_turn',
             'content': [{'type': 'text', 'text': 'ok'}],
             'usage': {'input_tokens': 3, 'output_tokens': 1}}
    rejection = json.loads(_anthropic_error(BINDING_MESSAGE))
    (content, _usage), posted = _run_chat(
        monkeypatch, [_ChatResp(400, rejection), _ChatResp(200, reply)],
        model='claude-sonnet-4-5', api_protocol='anthropic', messages=history)
    assert content == 'ok'
    assert len(posted) == 2

    def block_types(payload):
        return [block.get('type') for message in payload['messages']
                if message['role'] == 'assistant'
                for block in (message['content']
                              if isinstance(message['content'], list) else [])]

    assert 'thinking' in block_types(posted[0])
    assert 'thinking' not in block_types(posted[1])
    assert history == pristine


def _fake_stream_once(seen, failures):
    def once(body, **_kwargs):
        seen.append(copy.deepcopy(body))
        if len(seen) <= failures:
            raise ThinkingBlockBindingError(BINDING_MESSAGE)
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', {})
    return once


@pytest.mark.parametrize('failures,expect_error', [(1, False), (5, True)],
                         ids=['repaired', 'repeated'])
def test_sync_stream_withdraws_thinking_once(monkeypatch, failures, expect_error):
    import lib.llm.stream as smod

    seen = []
    monkeypatch.setattr(smod, '_stream_chat_once', _fake_stream_once(seen, failures))
    body = {'model': 'claude-x', 'messages': _thinking_history(), '_conv_id': 'c'}
    if expect_error:
        with pytest.raises(ThinkingBlockBindingError):
            smod.stream_chat(body, log_prefix='[t]')
    else:
        smod.stream_chat(body, log_prefix='[t]')
    assert len(seen) == 2
    assert _has_thinking_state(seen[0]['messages'][1])
    assert not any(_has_thinking_state(m) for m in seen[1]['messages'])


@pytest.mark.parametrize('failures,expect_error', [(1, False), (5, True)],
                         ids=['repaired', 'repeated'])
def test_async_stream_withdraws_thinking_once(monkeypatch, failures, expect_error):
    import lib.llm.astream as amod

    seen = []
    sync_once = _fake_stream_once(seen, failures)

    async def once(body, **kwargs):
        return sync_once(body, **kwargs)

    monkeypatch.setattr(amod, '_async_stream_chat_once', once)
    body = {'model': 'claude-x', 'messages': _thinking_history(), '_conv_id': 'c'}
    if expect_error:
        with pytest.raises(ThinkingBlockBindingError):
            asyncio.run(amod.async_stream_chat(body, log_prefix='[t]'))
    else:
        asyncio.run(amod.async_stream_chat(body, log_prefix='[t]'))
    assert len(seen) == 2
    assert not any(_has_thinking_state(m) for m in seen[1]['messages'])
