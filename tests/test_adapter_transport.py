#!/usr/bin/env python3
"""tests/test_adapter_transport.py — the ``adapter`` provider marker rides the relay.

A provider carrying ``adapter: {'agent_id': …, 'port': …}`` is a CLIProxyAPI
sidecar on the user's desktop agent. Its base_url is loopback-ON-THE-AGENT,
so the server can NEVER reach it directly: every request must ride
``lib.desktop.adapter.relay_stream`` / ``relay_http`` — no route_request, no
direct fallback. This suite pins that chain end to end:

  provider card → dispatcher → Slot.adapter → api.py kwargs → transport
  (stream / async stream / non-stream chat) → probe.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_adapter_transport.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

MARKER = {'agent_id': 'agent-xyz', 'port': 8317}
BASE_URL = 'http://127.0.0.1:8317/v1'

_SSE_LINES = [
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}',
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"content":"lo"}}]}',
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
    'data: [DONE]',
]

_BODY = {'model': 'm',
         'messages': [{'role': 'user', 'content': 'hi'}],
         'stream': True}


class _FakeStreamReader:
    """EgressStreamReader-shaped fake."""

    def __init__(self, lines, status=200, text=''):
        self.status_code = status
        self.headers = {}
        self._lines = lines
        self._text = text
        self.encoding = None
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def close(self):
        self.closed = True

    def read_all_text(self):
        return self._text


class _FakeHTTPResponse:
    """EgressResponse-shaped fake."""

    def __init__(self, status=200, payload=None, text=''):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


def _no_direct_route(url, **kwargs):
    raise AssertionError(
        'adapter providers must NEVER consult route_request — the server '
        'structurally cannot reach agent loopback (got %s)' % url)


# ═══════════════════════════════════════════════════════════
#  1. Provider card → dispatcher → Slot.adapter
# ═══════════════════════════════════════════════════════════

def _adapter_card():
    return {
        'id': 'adapter_agentxy',
        'name': '订阅适配器',
        'base_url': BASE_URL,
        'enabled': True,
        'adapter': dict(MARKER),
        'api_keys': ['ta_deadbeef'],
        'protocol': 'openai',
        'models': [{'model_id': 'claude-sub', 'capabilities': ['text']}],
    }


def test_slot_adapter_defaults_to_empty_dict():
    from lib.llm_dispatch.slot import Slot
    s = Slot(key_name='k', api_key='a', model='m', capabilities={'text'})
    assert s.adapter == {}


def test_dispatcher_carries_the_adapter_marker_onto_the_slot():
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    d = LLMDispatcher()
    d.slots = []
    d._build_slots_from_providers([_adapter_card()])
    got = [s for s in d.slots if s.model == 'claude-sub']
    assert got, 'no slot built for the adapter provider'
    for s in got:
        assert s.adapter == MARKER, s


def test_dispatcher_leaves_normal_providers_markerless():
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    card = _adapter_card()
    del card['adapter']
    d = LLMDispatcher()
    d.slots = []
    d._build_slots_from_providers([card])
    assert all(s.adapter == {} for s in d.slots)


# ═══════════════════════════════════════════════════════════
#  2. Sync stream transport
# ═══════════════════════════════════════════════════════════

def test_stream_chat_adapter_rides_relay_stream():
    from lib.llm.stream import stream_chat
    reader = _FakeStreamReader(_SSE_LINES)
    with mock.patch('lib.desktop.adapter.relay_stream',
                    return_value=reader) as relay, \
         mock.patch('lib.desktop.egress.route_request', _no_direct_route):
        msg, finish, usage = stream_chat(
            dict(_BODY), api_key='ta_deadbeef', base_url=BASE_URL,
            adapter=dict(MARKER))
    assert msg['content'] == 'Hello'
    assert finish == 'stop'
    relay.assert_called_once()
    args, kwargs = relay.call_args
    assert args[0] == 'agent-xyz'
    assert args[1] == 8317
    assert args[2] == '/v1/chat/completions', args
    assert kwargs['headers'].get('Authorization') == 'Bearer ta_deadbeef'


def test_stream_chat_adapter_relay_failure_is_unreachable():
    from lib.desktop.egress import EgressUnavailable
    from lib.llm.stream import stream_chat
    from lib.llm_errors import EndpointUnreachableError
    with mock.patch('lib.desktop.adapter.relay_stream',
                    side_effect=EgressUnavailable('agent offline')), \
         mock.patch('lib.desktop.egress.route_request', _no_direct_route):
        with pytest.raises(EndpointUnreachableError):
            stream_chat(dict(_BODY), api_key='ta_deadbeef',
                        base_url=BASE_URL, adapter=dict(MARKER))


def test_stream_chat_adapter_error_body_via_read_all_text():
    from lib.llm.stream import stream_chat
    from lib.llm_errors import RateLimitError
    reader = _FakeStreamReader([], status=429,
                               text='{"error":{"message":"quota"}}')
    with mock.patch('lib.desktop.adapter.relay_stream', return_value=reader), \
         mock.patch('lib.desktop.egress.route_request', _no_direct_route):
        with pytest.raises(RateLimitError):
            stream_chat(dict(_BODY), api_key='ta_deadbeef',
                        base_url=BASE_URL, adapter=dict(MARKER))


def test_stream_chat_without_adapter_still_routes_normally():
    """No marker → the existing route_request egress path is untouched."""
    from lib.llm.stream import stream_chat
    reader = _FakeStreamReader(_SSE_LINES)
    with mock.patch('lib.desktop.adapter.relay_stream') as relay, \
         mock.patch('lib.desktop.egress.route_request',
                    return_value='agent-9'), \
         mock.patch('lib.desktop.egress.open_stream', return_value=reader):
        msg, finish, usage = stream_chat(
            dict(_BODY), api_key='k',
            base_url='https://api.anthropic.com/v1')
    relay.assert_not_called()
    assert msg['content'] == 'Hello'


# ═══════════════════════════════════════════════════════════
#  3. Async stream transport (relay wrapped in asyncio.to_thread)
# ═══════════════════════════════════════════════════════════

def test_async_stream_chat_adapter_rides_relay_off_the_loop():
    from lib.llm.astream import async_stream_chat
    reader = _FakeStreamReader(_SSE_LINES)
    seen = {}

    def _relay(agent_id, port, path, **kwargs):
        import threading
        seen['thread'] = threading.current_thread().name
        seen['args'] = (agent_id, port, path)
        return reader

    with mock.patch('lib.desktop.adapter.relay_stream', side_effect=_relay), \
         mock.patch('lib.desktop.egress.route_request', _no_direct_route):
        msg, finish, usage = asyncio.run(async_stream_chat(
            dict(_BODY), api_key='ta_deadbeef', base_url=BASE_URL,
            adapter=dict(MARKER)))
    assert msg['content'] == 'Hello'
    assert finish == 'stop'
    assert seen['args'] == ('agent-xyz', 8317, '/v1/chat/completions')
    assert 'MainThread' not in seen['thread'], (
        'blocking bridge RTT must not run on the Quart event loop')


# ═══════════════════════════════════════════════════════════
#  4. Non-stream chat transport
# ═══════════════════════════════════════════════════════════

def test_chat_adapter_rides_relay_http():
    from lib.llm.chat import chat
    payload = {
        'choices': [{'message': {'role': 'assistant', 'content': 'hi there'},
                     'finish_reason': 'stop'}],
        'usage': {'total_tokens': 3},
    }
    with mock.patch('lib.desktop.adapter.relay_http',
                    return_value=_FakeHTTPResponse(200, payload)) as relay, \
         mock.patch('lib.desktop.egress.route_request', _no_direct_route):
        content, usage = chat(
            [{'role': 'user', 'content': 'hi'}], model='m',
            api_key='ta_deadbeef', base_url=BASE_URL, max_retries=0,
            adapter=dict(MARKER))
    assert content == 'hi there'
    args, kwargs = relay.call_args
    assert args[:3] == ('agent-xyz', 8317, '/v1/chat/completions')
    assert kwargs['method'] == 'POST'
    assert kwargs['headers'].get('Authorization') == 'Bearer ta_deadbeef'


def test_chat_adapter_relay_failure_is_unreachable():
    from lib.desktop.egress import EgressUnavailable
    from lib.llm.chat import chat
    from lib.llm_errors import EndpointUnreachableError
    with mock.patch('lib.desktop.adapter.relay_http',
                    side_effect=EgressUnavailable('agent offline')), \
         mock.patch('lib.desktop.egress.route_request', _no_direct_route):
        with pytest.raises(EndpointUnreachableError):
            chat([{'role': 'user', 'content': 'hi'}], model='m',
                 api_key='ta_deadbeef', base_url=BASE_URL, max_retries=0,
                 adapter=dict(MARKER))


# ═══════════════════════════════════════════════════════════
#  5. Probe chain
# ═══════════════════════════════════════════════════════════

def test_probe_one_cell_adapter_uses_relay_and_classifies():
    import lib.provider_probe as pp
    payload = {'choices': [{'message': {'content': 'OK'}}]}
    with mock.patch('lib.desktop.adapter.relay_http',
                    return_value=_FakeHTTPResponse(
                        200, payload,
                        '{"choices":[{"message":{"content":"OK"}}]}')) as relay:
        status, detail = pp.probe_one_cell(
            BASE_URL, 'ta_deadbeef', 'claude-sub', {}, 5,
            adapter=dict(MARKER))
    assert status == 'ok', (status, detail)
    args, kwargs = relay.call_args
    assert args[:3] == ('agent-xyz', 8317, '/v1/chat/completions')
    assert kwargs['method'] == 'POST'
    # The provider's api_key IS the adapter key — sent literally.
    assert kwargs['headers'].get('Authorization') == 'Bearer ta_deadbeef'


def test_probe_one_cell_adapter_unavailable_when_agent_down():
    import lib.provider_probe as pp
    from lib.desktop.egress import EgressUnavailable
    with mock.patch('lib.desktop.adapter.relay_http',
                    side_effect=EgressUnavailable('no agent')):
        status, detail = pp.probe_one_cell(
            BASE_URL, 'ta_deadbeef', 'claude-sub', {}, 5,
            adapter=dict(MARKER))
    assert status == 'unavailable', (status, detail)


def test_probe_cell_multi_forwards_adapter():
    import lib.provider_probe as pp
    seen = []
    orig = pp.probe_one_cell

    def _spy(base_url, api_key, model_id, extra_headers, timeout,
             protocol='openai', oauth='', adapter=None):
        seen.append(adapter)
        return 'ok', 'HTTP 200'

    pp.probe_one_cell = _spy
    try:
        status, _ = pp.probe_cell_multi(BASE_URL, 'k', 'm', {}, 5,
                                        attempts=1, adapter=dict(MARKER))
    finally:
        pp.probe_one_cell = orig
    assert status == 'ok'
    assert seen == [MARKER]


def test_run_cell_probe_task_threads_marker_from_task_key():
    import lib.provider_probe as pp
    seen = []
    orig = pp.probe_one_cell
    orig_persist = pp.persist_probe_task

    def _spy(base_url, api_key, model_id, extra_headers, timeout,
             protocol='openai', oauth='', adapter=None):
        seen.append(adapter)
        return 'ok', 'HTTP 200'

    task = {
        'provider_id': 'adapter_agentxy', 'status': 'running',
        'started_at': 0, 'finished_at': None, 'total': 1, 'done_count': 0,
        'cells': {}, 'summary': {'ok': 0, 'disable': 0}, 'error': None,
        'attempts': 1, '_abort': False, '_base_url': BASE_URL,
        '_extra_headers': {}, '_protocol': 'openai', '_oauth': '',
        '_adapter': dict(MARKER),
    }
    work = [(0, 'ta_deadbeef', 'claude-sub', 'claude-sub', ['text'])]
    pp.probe_one_cell = _spy
    pp.persist_probe_task = lambda t: None
    try:
        pp.run_cell_probe_task(task, work, timeout=5)
    finally:
        pp.probe_one_cell = orig
        pp.persist_probe_task = orig_persist
    assert seen == [MARKER], seen


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
