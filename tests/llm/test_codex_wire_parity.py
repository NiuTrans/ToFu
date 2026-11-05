"""Codex subscription HTTP wire parity with the first-party Codex CLI contract.

Covers the sync SSE transport shell end to end with a fake HTTP session:
sticky routing (``x-codex-turn-state``) is captured from the first response of
a turn and replayed only within that turn, and Tofu-internal trace headers do
not reach chatgpt.com.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit

_CODEX_BASE = 'https://chatgpt.com/backend-api/codex'


def _sse_lines(text='ok'):
    completed = {
        'type': 'response.completed',
        'response': {
            'id': 'resp_1',
            'output': [{'type': 'message', 'id': 'msg_1', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': text}]}],
            'usage': {'input_tokens': 5, 'output_tokens': 1, 'total_tokens': 6},
        },
    }
    return [
        'data: ' + json.dumps({'type': 'response.output_text.delta',
                               'output_index': 0, 'content_index': 0,
                               'item_id': 'msg_1', 'delta': text}),
        'data: ' + json.dumps(completed),
    ]


class _FakeResponse:
    status_code = 200
    encoding = 'utf-8'

    def __init__(self, headers):
        self.headers = headers

    def iter_lines(self, decode_unicode=True):
        yield from _sse_lines()

    def close(self):
        pass


@pytest.fixture
def codex_http(monkeypatch):
    """Fake HTTP session plus hermetic Codex identity inputs."""
    import lib.llm.stream as stream

    sent = []
    responses = []

    class _Session:
        @staticmethod
        def post(url, **kwargs):
            sent.append({'url': url, 'headers': dict(kwargs.get('headers') or {}),
                         'data': kwargs.get('data')})
            return _FakeResponse(responses.pop(0) if responses else {})

    monkeypatch.setattr(stream, 'get_sync_session', lambda: _Session())
    monkeypatch.setattr('lib.desktop.egress.route_request',
                        lambda *args, **kwargs: 'direct')
    monkeypatch.setattr('lib.proxy.subscription_route_candidates',
                        lambda *args, **kwargs: [])
    monkeypatch.setattr('lib.oauth.codex.codex_get_valid_token',
                        lambda *args, **kwargs: 'access-tok')
    monkeypatch.setattr('lib.oauth.token_store.load_token',
                        lambda *args, **kwargs: {'account_id': 'acc-1'})
    monkeypatch.setattr('lib.oauth.codex_catalog.cached_codex_provider_models',
                        lambda: [])
    monkeypatch.setattr('lib.oauth.codex_version.codex_client_version',
                        lambda: '0.165.0')

    def run(task_id):
        return stream.stream_chat({
            'model': 'gpt-6-sol',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'stream': True, '_conv_id': 'conv-a', '_task_id': task_id,
        }, api_key='', base_url=_CODEX_BASE, api_protocol='responses',
            oauth='codex')

    yield run, sent, responses
    from lib.oauth.codex_turn_state import release_turn_state
    for task_id in ('turn-1', 'turn-2'):
        release_turn_state(task_id)


def test_sse_turn_state_is_replayed_within_turn_only(codex_http):
    run, sent, responses = codex_http
    responses.extend([{'X-Codex-Turn-State': 'routing-a'},
                      {'x-codex-turn-state': 'routing-b'}, {}])

    run('turn-1')
    run('turn-1')
    run('turn-2')

    first, second, other_turn = (request['headers'] for request in sent)
    assert 'x-codex-turn-state' not in first
    # The first issued value of the turn wins and is echoed unchanged.
    assert second['x-codex-turn-state'] == 'routing-a'
    assert 'x-codex-turn-state' not in other_turn


def test_sse_request_carries_first_party_headers_only(codex_http):
    run, sent, _responses = codex_http
    run('turn-1')

    headers = sent[0]['headers']
    assert sent[0]['url'] == _CODEX_BASE + '/responses'
    assert headers['Accept'] == 'text/event-stream'
    assert headers['version'] == '0.165.0'
    assert headers['x-codex-routing-hint'] == 'model=gpt-6-sol'
    assert headers['originator'] == 'codex-tui'
    assert 'OpenAI-Beta' not in headers
    assert 'M-TraceId' not in headers


def test_sse_body_uses_codex_cli_zstd_encoding(codex_http):
    import zstandard

    run, sent, _responses = codex_http
    run('turn-1')

    request = sent[0]
    assert request['headers']['Content-Encoding'] == 'zstd'
    body = json.loads(zstandard.ZstdDecompressor().decompress(request['data']))
    assert body['model'] == 'gpt-6-sol' and body['stream'] is True
    assert body['store'] is False


def test_sse_body_stays_identity_encoded_without_a_codec(codex_http, monkeypatch):
    import lib.llm.request_encoding as request_encoding

    monkeypatch.setattr(request_encoding, '_codec', None)
    monkeypatch.setattr(request_encoding, '_resolved', True)
    run, sent, _responses = codex_http
    run('turn-1')

    assert 'Content-Encoding' not in sent[0]['headers']
    assert json.loads(sent[0]['data'])['model'] == 'gpt-6-sol'


def test_only_the_codex_subscription_route_is_compressed():
    from lib.llm._sse_core import prepare_request

    plan = prepare_request({
        'model': 'gpt-5.6-sol',
        'messages': [{'role': 'user', 'content': 'hi'}],
    }, api_key='k', base_url='https://api.openai.com/v1',
        api_protocol='responses')
    assert plan.body.content_encoding == ''
    assert 'Content-Encoding' not in plan.hdrs


def test_sealed_payload_encodes_only_at_the_byte_transport_boundary():
    import zstandard
    from lib.llm.request import (
        seal_request, transport_body, transport_body_arguments,
        transport_payload)

    sealed = seal_request(
        {'model': 'm', 'input': [{'type': 'message', 'role': 'user',
                                  'content': 'x' * 2048}]},
        model='m', protocol='responses', input_tokens=1, semantic_id='s',
        content_encoding='zstd')
    decoded = zstandard.ZstdDecompressor().decompress

    assert decoded(transport_payload(sealed)) == sealed.payload
    assert decoded(transport_body_arguments(
        sealed, transport='requests')['data']) == sealed.payload
    assert decoded(transport_body_arguments(
        sealed, transport='httpx')['content']) == sealed.payload
    assert len(transport_payload(sealed)) < len(sealed.payload)
    # JSON consumers (diagnostics, WebSocket frames) see plain JSON.
    assert transport_body(sealed)['model'] == 'm'


def test_codex_task_round_falls_back_to_a_complete_sse_request(
        codex_http, monkeypatch):
    import zstandard
    import lib.llm.stream as stream
    from lib.llm import responses_websocket as ws

    def unavailable(*args, **kwargs):
        raise ws.ResponsesWebSocketUnavailable('HTTP 426 Upgrade Required')

    monkeypatch.setattr(ws, '_open_session', unavailable)
    _run, sent, _responses = codex_http
    try:
        _msg, finish, _usage = stream.stream_chat({
            'model': 'gpt-6-sol',
            'messages': [{'role': 'user', 'content': 'hi'}],
            '_conv_id': 'conv-a', '_task_id': 'turn-ws',
            '_responses_transport': 'sse',
        }, api_key='', base_url=_CODEX_BASE, api_protocol='responses',
            oauth='codex')
    finally:
        ws.release_task_websockets('turn-ws')

    assert finish == 'stop' and len(sent) == 1
    body = json.loads(zstandard.ZstdDecompressor().decompress(sent[0]['data']))
    assert body['stream'] is True
    assert 'previous_response_id' not in body


def test_task_lane_teardown_releases_turn_state():
    from lib.oauth.codex_turn_state import observe_turn_state, turn_state
    from lib.tasks_pkg.orchestrator._teardown import finalize_task_lane

    observe_turn_state('turn-teardown', 'routing-a')
    finalize_task_lane({'id': 'turn-teardown'}, 'turn-tea')
    assert turn_state('turn-teardown') == ''
