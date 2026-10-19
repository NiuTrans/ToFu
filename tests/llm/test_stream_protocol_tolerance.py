"""Cross-protocol wire semantics, terminal tolerance, and typed error parity."""

import json

import pytest

from lib.llm._sse_framer import SSEFramer
from lib.llm.stream_result import ProviderStreamState
from lib.llm.llm_errors import (
    BadRequestError, ContentFilterError, PermissionError_, PromptTooLongError,
    ProviderStreamError, RateLimitError, RequestScopedError, RetryableAPIError,
)
from tests.sse.test_sse_core_parity import _run_async, _run_sync

pytestmark = pytest.mark.unit


def _wire(events):
    return b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events)


def _completion(protocol, text='Finished.'):
    if protocol == 'openai':
        return [{'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}]}]
    if protocol == 'anthropic':
        return [
            {'type': 'content_block_start', 'index': 0,
             'content_block': {'type': 'text', 'text': text}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}},
        ]
    return [
        {'type': 'response.output_text.delta', 'delta': text},
        {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}},
    ]


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
@pytest.mark.parametrize('tail', [b': heartbeat', b'id: final\n', b'event: ping\nretry: 1000\n'])
def test_control_only_eof_does_not_invalidate_provider_finish(run, protocol, tail):
    result = run([], raw_chunks=[_wire(_completion(protocol)) + tail], protocol=protocol)
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == 'Finished.'
    assert result.evidence.malformed_frame_count == 0


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_finish_without_done_does_not_log_premature_close(run, caplog):
    result = run([], raw_chunks=[_wire(_completion('openai'))])
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert 'PREMATURE STREAM CLOSE' not in caplog.text


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('code, expected, status', [
    ('rate_limit_exceeded', RateLimitError, 429),
    ('insufficient_quota', RateLimitError, 429),
    ('server_error', RetryableAPIError, 500),
    ('invalid_api_key', PermissionError_, 401),
    ('invalid_request_error', BadRequestError, None),
    ('context_length_exceeded', PromptTooLongError, None),
    ('not_found_error', RequestScopedError, 404),
])
@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic'])
def test_structured_error_code_controls_classification(run, code, expected, status, protocol):
    error = {'code': code, 'message': 'Synthetic provider failure'}
    if protocol == 'responses':
        events = [{'type': 'error', **error}]
    elif protocol == 'anthropic':
        events = [{'type': 'error', 'error': error}]
    else:
        events = [{'error': error}]
    with pytest.raises(expected) as caught:
        run([], raw_chunks=[_wire(events)], protocol=protocol)
    if status is not None:
        assert caught.value.status_code == status
    if code == 'insufficient_quota':
        assert caught.value.is_quota is True
    elif code == 'rate_limit_exceeded':
        assert caught.value.is_quota is False


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_explicit_sse_status_preserves_gateway_semantics(run):
    with pytest.raises(RateLimitError) as caught:
        run([], raw_chunks=[_wire([{'error': {
            'code': 'invalid_request_error', 'http_code': 503, 'message': 'Unavailable',
        }}])])
    assert caught.value.is_gateway
    assert caught.value.status_code == 503


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_responses_content_filter_is_typed(run):
    with pytest.raises(ContentFilterError):
        run([], protocol='responses', raw_chunks=[_wire([{
            'type': 'response.incomplete', 'response': {
                'status': 'incomplete', 'output': [],
                'incomplete_details': {'reason': 'content_filter'},
            },
        }])])


@pytest.mark.parametrize('retry', ['²', '9' * 5000], ids=['non-ascii', 'oversized'])
def test_retry_hint_cannot_crash_the_provider_parser(retry):
    framer = SSEFramer()
    events = framer.feed(('retry: ' + retry + '\ndata: ok\n\n').encode())
    assert len(events) == 1 and events[0].data == 'ok'
    assert framer.drain_issues().count == 0


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('prefix', ['', 'Fin', 'Finished.'])
def test_responses_terminal_snapshot_restores_only_missing_suffix(run, prefix):
    events = []
    if prefix:
        events.append({'type': 'response.output_text.delta', 'delta': prefix})
    events.append({'type': 'response.completed', 'response': {
        'status': 'completed', 'output': [{
            'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'Finished.'}],
        }],
    }})
    result = run([], protocol='responses', raw_chunks=[_wire(events)])
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == 'Finished.'


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_truncated_provider_data_remains_malformed(run):
    result = run([], raw_chunks=[_wire(_completion('openai')) + b'data: {"usage":'])
    assert result.state is ProviderStreamState.MALFORMED_STREAM


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_conflicting_terminal_snapshot_is_not_spliced_into_the_answer(run):
    events = [
        {'type': 'response.output_text.delta', 'delta': 'Original'},
        {'type': 'response.completed', 'response': {'status': 'completed', 'output': [{
            'type': 'message', 'content': [{'type': 'output_text', 'text': 'Different'}],
        }]}},
    ]
    with pytest.raises(RetryableAPIError, match='contradicts'):
        run([], protocol='responses', raw_chunks=[_wire(events)])


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('content', [
    None, [], [{'type': 'output_text', 'text': None}],
    [{'type': 'output_text', 'text': 'Fin'}, {'type': 'output_text', 'text': None}],
])
def test_terminal_metadata_without_text_does_not_invalidate_complete_deltas(run, content):
    events = [
        {'type': 'response.output_text.delta', 'delta': 'Finished.'},
        {'type': 'response.completed', 'response': {'status': 'completed', 'output': [{
            'type': 'message', 'content': content,
        }]}},
    ]
    result = run([], protocol='responses', raw_chunks=[_wire(events)])
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == 'Finished.'


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_unicode_terminal_snapshot_excludes_hidden_agent_text(run):
    events = [
        {'type': 'response.output_text.delta', 'delta': '你好'},
        {'type': 'response.completed', 'response': {'status': 'completed', 'output': [
            {'type': 'message', 'agent': {'agent_name': '/worker'},
             'content': [{'type': 'output_text', 'text': 'Private intermediate text'}]},
            {'type': 'message', 'agent': {'agent_name': '/root'}, 'phase': 'final_answer',
             'content': [{'type': 'output_text', 'text': '你好'},
                         {'type': 'output_text', 'text': '，世界🌏'}]},
        ]}},
    ]
    result = run([], protocol='responses', raw_chunks=[_wire(events)])
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == '你好，世界🌏'


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_structured_request_failure_does_not_penalize_network_route(run, monkeypatch):
    outcomes = []
    module = 'lib.llm.stream' if run is _run_sync else 'lib.llm.astream'
    monkeypatch.setattr(module + '._proxy_report_outcome',
                        lambda _url, ok, *_args, **_kwargs: outcomes.append(ok))
    with pytest.raises(BadRequestError):
        run([], base_url='http://fake.local/v1', api_key='synthetic-key', raw_chunks=[_wire([{'error': {
            'code': 'invalid_request_error', 'message': 'Unsupported parameter',
        }}])])
    assert outcomes == [True]


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_sse_missing_credential_claim_preserves_gateway_contradiction(run):
    with pytest.raises(RateLimitError) as caught:
        run([], base_url='http://fake.local/v1', api_key='synthetic-key', raw_chunks=[_wire([{'error': {
            'code': 'invalid_api_key', 'message': 'Missing API key',
        }}])])
    assert caught.value.is_credential_delivery_anomaly
    assert caught.value.is_gateway


def test_response_snapshot_tracker_retains_constant_space():
    import tracemalloc
    from lib.llm.responses_outbound._sse import ResponsesSSETranslator

    translator = ResponsesSSETranslator()
    wire = json.dumps({'type': 'response.output_text.delta', 'delta': 'x' * (64 << 10)})
    translator.translate(wire)  # Warm imports and one-time formatting paths.
    tracemalloc.start()
    try:
        baseline, _ = tracemalloc.get_traced_memory()
        for _ in range(128):  # 8 MiB emitted; no response-sized duplicate is retained.
            translator.translate(wire)
        retained, _ = tracemalloc.get_traced_memory()
        assert retained - baseline < 256 << 10
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize('code, expected', [
    ('insufficient_quota', RateLimitError),
    ('invalid_api_key', PermissionError_),
    ('invalid_request_error', BadRequestError),
])
@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_nonstream_error_uses_same_classification(monkeypatch, code, expected, protocol):
    import importlib
    chat = importlib.import_module('lib.llm.chat')

    class Response:
        status_code = 200
        headers = {}
        text = '{}'

        def json(self):
            return {'error': {
                'code': code, 'message': 'Synthetic provider failure',
            }}

    monkeypatch.setattr(chat, 'http_post', lambda *_args, **_kwargs: Response())
    with pytest.raises(expected) as caught:
        chat.chat([{'role': 'user', 'content': 'Hello'}], 'unit-model',
                  api_key='synthetic-key', base_url='https://api.example.test/v1',
                  api_protocol=protocol)
    if code == 'insufficient_quota':
        assert caught.value.is_quota


@pytest.mark.parametrize('code, expected', [
    ('rate_limit_exceeded', RateLimitError), ('insufficient_quota', RateLimitError),
    ('invalid_api_key', PermissionError_), ('invalid_request_error', BadRequestError),
    ('unknown_provider_failure', ProviderStreamError),
])
@pytest.mark.parametrize('nested', [False, True])
def test_websocket_error_uses_the_shared_responses_translator(monkeypatch, code, expected, nested):
    from lib.llm import responses_websocket as ws
    from lib.llm._sse_core import prepare_request
    from tests.llm.test_responses_websocket import _FakeConnection, _install_connection

    error = {'code': code, 'message': 'Synthetic provider failure'}

    class Connection(_FakeConnection):
        def send(self, payload):
            self.events = [{'type': 'error', **({'error': error} if nested else error)}]

    connection = Connection()
    _install_connection(ws, monkeypatch, connection)
    plan = prepare_request(
        {'model': 'unit-model', 'messages': [{'role': 'user', 'content': 'Hello'}]},
        api_key='synthetic-key', base_url='https://api.example.test/v1', api_protocol='responses')
    try:
        with pytest.raises(expected) as caught:
            ws.stream_responses_websocket(plan)
        if isinstance(caught.value, RateLimitError):
            assert caught.value.is_quota is (code == 'insufficient_quota')
        assert connection.closed
    finally:
        ws._sessions().clear()


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_null_error_field_is_a_successful_chunk(run):
    event = {**_completion('openai')[0], 'error': None}
    result = run([], raw_chunks=[_wire([event])])
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == 'Finished.'


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('message', [None, {}, []])
def test_error_classification_survives_malformed_message_field(run, message):
    with pytest.raises(RateLimitError) as caught:
        run([], raw_chunks=[_wire([{'error': {
            'type': 'rate_limit_error', 'message': message,
        }}])])
    assert caught.value.status_code == 429


def test_nonstream_null_error_and_content_filter(monkeypatch):
    import importlib
    chat = importlib.import_module('lib.llm.chat')
    payload = {'error': None, 'choices': [{
        'message': {'role': 'assistant', 'content': 'Finished.'},
        'finish_reason': 'stop',
    }]}

    class Response:
        status_code = 200
        headers = {}
        text = '{}'

        def json(self):
            return payload

    monkeypatch.setattr(chat, 'http_post', lambda *_args, **_kwargs: Response())
    kwargs = dict(api_key='synthetic-key', base_url='https://api.example.test/v1')
    result = chat.chat([{'role': 'user', 'content': 'Hello'}], 'unit-model', **kwargs)
    assert result[0] == 'Finished.'
    payload = {'status': 'incomplete', 'output': [],
               'incomplete_details': {'reason': 'content_filter'}}
    with pytest.raises(ContentFilterError):
        chat.chat([{'role': 'user', 'content': 'Hello'}], 'unit-model',
                  api_protocol='responses', **kwargs)
