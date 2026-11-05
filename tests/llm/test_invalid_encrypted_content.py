"""Exact encrypted Responses state rejection is terminal and content-free."""

import copy
import importlib
import json

import pytest

from lib.llm import llm_errors as errors
from tests.llm.test_responses_websocket import _ScriptedConnection, _install_connection
from tests.sse.test_sse_core_parity import _run_async, _run_sync

pytestmark = pytest.mark.unit

CODE = 'invalid_encrypted_content'
PARAM = 'input[1].encrypted_content'
CIPHERTEXT = 'synthetic-ciphertext-must-not-be-retained'


def _error(*, message=None, param=PARAM):
    error = {'code': CODE, 'param': param}
    if message is not None:
        error['message'] = message
    return error


def _assert_typed(error, *, param=PARAM, stage='provider_response'):
    assert type(error).__name__ == 'InvalidEncryptedContentError'
    assert isinstance(error, errors.RequestScopedError)
    assert not isinstance(error, errors.RetryableAPIError)
    assert error.retryable is False
    assert error.code == CODE
    assert error.param == param
    assert error.status_code == 400
    assert error.failure_stage == stage
    assert CIPHERTEXT not in str(error)
    assert CIPHERTEXT not in repr(vars(error))


@pytest.mark.parametrize('status', [400, 503])
@pytest.mark.parametrize('message', [None, CIPHERTEXT, 'server overload: ' + CIPHERTEXT])
def test_exact_structured_code_wins_before_http_text_or_status(status, message, caplog):
    with pytest.raises(errors.RequestScopedError) as caught:
        errors._classify_http_error(
            status, 'API HTTP %s: %s' % (status, json.dumps({'error': _error(message=message)})),
            'unit-model', '[encrypted-state]')
    _assert_typed(caught.value)
    assert CIPHERTEXT not in caplog.text


@pytest.mark.parametrize('error', [
    {'code': CODE + '_other', 'message': 'ordinary rejection'},
    {'code': CODE.upper(), 'message': 'ordinary rejection'},
    {'type': CODE, 'message': 'ordinary rejection'},
    {'message': CODE},
])
def test_only_exact_code_selects_the_terminal_type(error):
    with pytest.raises(errors.BadRequestError) as caught:
        errors._classify_http_error(400, json.dumps({'error': error}), 'unit-model', '')
    assert type(caught.value) is errors.BadRequestError


@pytest.mark.parametrize('param,expected', [
    (PARAM, PARAM),
    ('input.1.encrypted_content', 'input[1].encrypted_content'),
    ('input[1]', 'input[1]'),
    ('input.' + 'x' * 1000, None),
    ({'encrypted_content': CIPHERTEXT}, None),
    (CIPHERTEXT, None),
    ('input[1].' + CIPHERTEXT, None),
    ('input[1].encrypted_content.' + CIPHERTEXT, None),
    ('input[' + '9' * 257 + '].encrypted_content', None),
    ('input[1].encrypted_content\n' + CIPHERTEXT, None),
])
def test_param_retains_only_bounded_structural_input_labels(param, expected):
    payload = json.dumps({'error': _error(param=param)})
    with pytest.raises(errors.RequestScopedError) as caught:
        errors._classify_http_error(400, payload, 'unit-model', '')
    _assert_typed(caught.value, param=expected)
    response = type('Response', (), {'content': payload.encode(), 'encoding': 'utf-8'})()
    decoded = errors.decode_error_body(response)
    assert CIPHERTEXT not in decoded
    fields = {'code': CODE}
    if expected is not None:
        fields['param'] = expected
    assert json.loads(decoded)['error'] == fields


def test_http_decoder_keeps_code_before_diagnostic_clipping_or_limit_retry():
    # Code follows a long opaque message and misleading token-limit text.
    payload = {'error': {'message': CIPHERTEXT * 300 + ' max_tokens exceeds limit: 8192',
                         'code': CODE, 'param': PARAM}}
    response = type('Response', (), {'content': json.dumps(payload).encode(),
                                    'encoding': 'utf-8'})()
    decoded = errors.decode_error_body(response)
    assert len(decoded) < errors._ERR_BODY_LIMIT
    assert CIPHERTEXT not in decoded
    assert json.loads(decoded)['error'] == _error()


@pytest.mark.parametrize('run', [_run_sync, _run_async], ids=['sync', 'async'])
@pytest.mark.parametrize('shape', ['top', 'nested', 'failed'])
def test_sse_code_only_responses_errors_use_the_terminal_type(run, shape):
    error = _error()
    if shape == 'top':
        event = {'type': 'error', **error}
    elif shape == 'nested':
        event = {'type': 'error', 'error': error}
    else:
        event = {'type': 'response.failed', 'response': {'status': 'failed', 'error': error}}
    wire = b'data: ' + json.dumps(event).encode() + b'\n\n'
    with pytest.raises(errors.RequestScopedError) as caught:
        run([], raw_chunks=[wire], protocol='responses',
            base_url='https://api.example.test/v1', api_key='synthetic-key')
    _assert_typed(caught.value, stage='provider_stream')


@pytest.mark.parametrize('status', [400, 200])
@pytest.mark.parametrize('message', [None, CIPHERTEXT * 300 + ' max_tokens exceeds limit: 8192'],
                         ids=['code-only', 'opaque-message'])
@pytest.mark.parametrize('param', [PARAM, 'max_tokens must be at most 8192'],
                         ids=['field-path', 'misleading-limit'])
def test_nonstream_does_not_retry_or_retain_invalid_state(monkeypatch, status, message, param, caplog):
    chat = importlib.import_module('lib.llm.chat')
    payload = {'error': _error(message=message, param=param), 'status': 'failed'}
    calls, learned = [], []

    class Response:
        status_code = status
        headers = {}
        encoding = 'utf-8'
        content = json.dumps(payload).encode()

        def json(self):
            return payload

    def post(*args, **kwargs):
        calls.append(kwargs['data'])
        return Response()

    monkeypatch.setattr(chat, 'http_post', post)
    monkeypatch.setattr('lib.model_info._learn_model_limit', lambda *a, **k: learned.append((a, k)))
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')
    messages = [{'role': 'user', 'content': 'Continue'}]
    before = copy.deepcopy(messages)
    with pytest.raises(errors.RequestScopedError) as caught:
        chat.chat(messages, 'unit-model', api_key='synthetic-key',
                  base_url='https://api.example.test/v1', api_protocol='responses',
                  max_tokens=10000, max_retries=2)
    _assert_typed(caught.value, param=PARAM if param == PARAM else None)
    assert len(calls) == 1
    assert learned == []
    assert messages == before
    assert CIPHERTEXT not in caplog.text


@pytest.mark.parametrize('nested', [False, True])
def test_websocket_code_only_failure_closes_without_replay_or_checkpoint(monkeypatch, nested):
    from lib.llm import responses_websocket as ws
    from lib.llm._sse_core import prepare_request

    class Connection(_ScriptedConnection):
        def send(self, payload):
            self.sent.append(json.loads(payload))
            error = _error()
            self.events = [{'type': 'error', **({'error': error} if nested else error)}]

    connection = Connection()
    _install_connection(ws, monkeypatch, connection)
    source = {'model': 'unit-model', 'messages': [{'role': 'user', 'content': 'Continue'}]}
    before = copy.deepcopy(source)
    plan = prepare_request(source, api_key='synthetic-key',
                           base_url='https://api.example.test/v1', api_protocol='responses')
    try:
        with pytest.raises(errors.RequestScopedError) as caught:
            ws.stream_responses_websocket(plan)
        _assert_typed(caught.value)
        assert len(connection.sent) == 1
        assert connection.closed
        assert not ws._sessions()
        assert source == before
    finally:
        ws._sessions().clear()
