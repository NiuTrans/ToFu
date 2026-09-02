"""Parity + characterization tests for the unified SSE streaming core.

Background
----------
``lib/llm/stream.py`` (sync, requests) and ``lib/llm/astream.py`` (async,
httpx) used to each carry a ~480-line copy of the identical SSE parsing
loop. They were collapsed onto ``lib/llm/_sse_core.py``. These tests lock
the behavior so the collapse is provably byte-for-byte:

1. **Parity** — the SAME recorded provider events driven through the sync
   shell and the async shell yield the SAME typed result and compatibility
   projection (modulo varying trace/time observations).
2. **Characterization** — known transcripts (normal, tool-call, MiniMax
   ``<think>`` demux, missing-[DONE], mid-JSON EOF, empty-stop, SSE-error-429)
   produce the expected message + the exact anomaly ``usage`` flags that
   ``lib/tasks_pkg/stream_handler.py`` keys its retry buckets off of.

No network: we monkeypatch ``requests.post`` and ``httpx.AsyncClient`` to
replay a fixed list of SSE lines.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm_errors import RateLimitError  # noqa: E402
from lib.llm._sse_core import SSEAccumulator  # noqa: E402
from lib.llm.anthropic_outbound import AnthropicSSETranslator  # noqa: E402
from lib.llm.diagnostics import RawSSEDumper  # noqa: E402
from lib.llm.responses_outbound import ResponsesSSETranslator  # noqa: E402
from lib.llm.stream_result import (  # noqa: E402
    ProviderStreamResult,
    ProviderStreamState,
)

pytestmark = pytest.mark.unit


# ── Recorded SSE transcripts (list of raw `data:` lines, no trailing \n) ──

def _sse(obj_lines):
    return obj_lines


NORMAL = [
    'data: {"choices":[{"delta":{"role":"assistant","content":"Hello"}}]}',
    'data: {"choices":[{"delta":{"content":" world"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2}}',
    'data: [DONE]',
]

TOOL_CALL = [
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc_0","function":{"name":"grep_search","arguments":""}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"pattern\\":\\"foo\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    'data: [DONE]',
]

# Standalone tool call whose `arguments` delta never arrives → empty string.
# Must be normalized to '{}' so a later replay to Gemini's OpenAI-compat proxy
# does not 400 with "Expected function 'arguments' ... to be populated".
EMPTY_ARGS_TOOL_CALL = [
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc_0","function":{"name":"get_status","arguments":""}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    'data: [DONE]',
]

# Two same-named calls: one with real args, one empty → the empty one is a
# phantom duplicate and must be DROPPED entirely (not normalized to '{}').
PHANTOM_DUP_TOOL_CALL = [
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc_0","function":{"name":"grep_search","arguments":"{\\"pattern\\":\\"foo\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"tc_1","function":{"name":"grep_search","arguments":""}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    'data: [DONE]',
]

MINIMAX_THINK = [
    'data: {"choices":[{"delta":{"content":"<think>reason"}}]}',
    'data: {"choices":[{"delta":{"content":"ing</think>answer"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}}]}',
    'data: [DONE]',
]

# Stream that never sends [DONE] — premature close anomaly.
MISSING_DONE = [
    'data: {"choices":[{"delta":{"content":"partial"}}]}',
]

# A provider finish frame is semantic completion even when this compatible
# endpoint omits the optional trailing ``[DONE]`` sentinel.
FINISH_WITHOUT_DONE = [
    'data: {"choices":[{"delta":{"content":"complete"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
]

# Production shape from conversation mt9lvcgir9n62o: the gateway closed the
# connection in the middle of an SSE JSON object. A parser cannot safely infer
# the missing structural bytes, so it must reject only that incomplete frame,
# preserve every prior complete frame, and surface the close as an anomaly for
# the prefix-preserving continuation layer above it.
MID_JSON_CLOSE = [
    'data: {"choices":[{"delta":{"content":"from the"}}]}',
    'data: {"choices":[{"delta":{"content":" guidance"}},"lastO',
]

# finish=stop but no content and no tool calls → empty_stop anomaly.
EMPTY_STOP = [
    'data: {"choices":[{"delta":{"role":"assistant"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    'data: [DONE]',
]

SSE_ERROR_429 = [
    'data: {"error":{"message":"Too many requests","http_code":429}}',
]


# ── Fake sync transport (requests.post) ──

class _FakeRequestsResp:
    def __init__(self, lines, status=200, headers=None, raw_chunks=None):
        self._lines = lines
        self._raw_chunks = raw_chunks
        self.status_code = status
        self.headers = headers or {}
        self.encoding = 'utf-8'
        self.text = '' if status == 200 else 'error body'

    def iter_lines(self, decode_unicode=True):
        for ln in self._lines:
            yield ln

    def iter_content(self, chunk_size=64 << 10):
        if self._raw_chunks is not None:
            yield from self._raw_chunks
            return
        for line in self._lines:
            value = line.encode('utf-8') if isinstance(line, str) else bytes(line)
            yield value + b'\n\n'

    def close(self):
        pass


def _run_sync(lines, model='gpt-4', status=200, raw_chunks=None):
    import lib.llm.stream as smod

    def fake_post(url, **kw):
        return _FakeRequestsResp(
            lines, status=status, raw_chunks=raw_chunks)

    class _FakeSession:
        post = staticmethod(fake_post)

    orig = smod.get_sync_session
    smod.get_sync_session = lambda: _FakeSession()
    # 传输壳与出口路由分层：本套件测 SSE 壳行为，路由层（探测/选 agent）
    # 归 tests/test_desktop_egress*.py 管 —— 钉住为直连，否则在 conftest 的
    # api.openai.com 默认 URL 下会真探测并拒绝。
    import lib.desktop.egress as _egmod
    orig_route = _egmod.route_request
    _egmod.route_request = lambda url, **kw: 'direct'
    try:
        body = {'model': model, 'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 100}
        return smod._stream_chat_once(body, log_prefix='[test]')
    finally:
        smod.get_sync_session = orig
        _egmod.route_request = orig_route


# ── Fake async transport (httpx.AsyncClient.stream) ──

class _FakeAsyncStreamCtx:
    def __init__(self, lines, status=200, headers=None, raw_chunks=None):
        self._lines = lines
        self._raw_chunks = raw_chunks
        self.status_code = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aiter_bytes(self):
        if self._raw_chunks is not None:
            for chunk in self._raw_chunks:
                yield chunk
            return
        for line in self._lines:
            value = line.encode('utf-8') if isinstance(line, str) else bytes(line)
            yield value + b'\n\n'

    async def aread(self):
        return b'error body'


class _FakeAsyncClient:
    def __init__(self, lines, status=200, raw_chunks=None):
        self._lines = lines
        self._status = status
        self._raw_chunks = raw_chunks

    def __init_subclass__(cls):  # pragma: no cover
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        return _FakeAsyncStreamCtx(
            self._lines, status=self._status, raw_chunks=self._raw_chunks)


def _run_async(lines, model='gpt-4', status=200, raw_chunks=None):
    import lib.llm.astream as amod
    import lib.desktop.egress as _egmod

    def fake_client_factory(*a, **kw):
        return _FakeAsyncClient(lines, status=status, raw_chunks=raw_chunks)

    orig = amod.get_async_client
    orig_route = _egmod.route_request
    amod.get_async_client = fake_client_factory
    _egmod.route_request = lambda url, **kw: 'direct'
    try:
        body = {'model': model, 'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 100}
        return asyncio.run(
            amod._async_stream_chat_once(body, log_prefix='[test]'))
    finally:
        amod.get_async_client = orig
        _egmod.route_request = orig_route


# ── Normalize: drop the always-varying fields before comparing ──

_VARYING = {
    'trace_id', 'resp_trace_id', 'stream_elapsed_ms',
    # Route selection is external transport state (health/circuit timing), not
    # an SSE parsing output. Each shell must carry it, but parity compares the
    # normalized message/anomaly contract rather than requiring the same path.
    '_network_route',
    '_transport_idle_ms',
}


def _norm_usage(usage):
    return {k: v for k, v in (usage or {}).items() if k not in _VARYING}


# ── Parity tests: sync vs async produce identical output ──

@pytest.mark.parametrize('name,lines,model', [
    ('normal', NORMAL, 'gpt-4'),
    ('tool_call', TOOL_CALL, 'gpt-4'),
    ('minimax_think', MINIMAX_THINK, 'MiniMax-M2.7'),
    ('missing_done', MISSING_DONE, 'gpt-4'),
    ('finish_without_done', FINISH_WITHOUT_DONE, 'gpt-4'),
    ('mid_json_close', MID_JSON_CLOSE, 'gpt-4'),
    ('empty_stop', EMPTY_STOP, 'gpt-4'),
])
def test_sync_async_parity(name, lines, model):
    result_s = _run_sync(lines, model=model)
    result_a = _run_async(lines, model=model)
    msg_s, fr_s, usage_s = result_s
    msg_a, fr_a, usage_a = result_a
    assert isinstance(result_s, ProviderStreamResult)
    assert isinstance(result_a, ProviderStreamResult)
    assert result_s.state is result_a.state
    assert msg_s == msg_a, f'{name}: assistant msg differs'
    assert fr_s == fr_a, f'{name}: finish_reason differs'
    assert _norm_usage(usage_s) == _norm_usage(usage_a), f'{name}: usage differs'


@pytest.mark.parametrize('chunk_size', [1, 2, 5, 17, 4096])
def test_sync_async_raw_byte_chunking_parity(chunk_size):
    wire = ('\ufeff: keep-alive\r\n'
            'data: {"choices":[{"delta":{"content":"你"}}]}\r\n\r\n'
            'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\r\r'
            'data: [DONE]\n\n').encode('utf-8')
    chunks = [wire[index:index + chunk_size]
              for index in range(0, len(wire), chunk_size)]

    sync_result = _run_sync([], raw_chunks=chunks)
    async_result = _run_async([], raw_chunks=chunks)

    assert sync_result.message['content'] == '你好'
    assert async_result.message == sync_result.message
    assert async_result.state is sync_result.state
    assert _norm_usage(async_result.usage) == _norm_usage(sync_result.usage)


# ── Characterization tests: exact expected output ──

def test_normal_content():
    result = _run_sync(NORMAL)
    msg, fr, usage = result
    assert msg == {'role': 'assistant', 'content': 'Hello world'}
    assert fr == 'stop'
    assert usage['prompt_tokens'] == 10
    assert usage['_chunks_received'] == 3
    assert '_stream_anomaly' not in usage
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.provider_finish_reason == 'stop'
    assert result.is_verified_complete is True


def test_tool_call_accumulation():
    msg, fr, usage = _run_sync(TOOL_CALL)
    assert fr == 'tool_calls'
    assert msg['tool_calls'][0]['function']['name'] == 'grep_search'
    assert msg['tool_calls'][0]['function']['arguments'] == '{"pattern":"foo"}'


def test_empty_args_tool_call_normalized_to_empty_object():
    """A standalone no-arg tool call must replay with arguments='{}' (not '').

    Regression for the Gemini HTTP 400 "Expected function 'arguments' ... to
    be populated" that killed follow-up turns (esp. swarm sub-agents, which
    replay the streamed assistant msg verbatim).
    """
    msg, fr, usage = _run_sync(EMPTY_ARGS_TOOL_CALL)
    assert fr == 'tool_calls'
    assert len(msg['tool_calls']) == 1
    assert msg['tool_calls'][0]['function']['name'] == 'get_status'
    assert msg['tool_calls'][0]['function']['arguments'] == '{}'
    # Parity: the async shell normalizes identically.
    msg_a, _, _ = _run_async(EMPTY_ARGS_TOOL_CALL)
    assert msg_a['tool_calls'][0]['function']['arguments'] == '{}'


def test_phantom_duplicate_still_dropped_not_normalized():
    """Empty-args duplicate of a real same-named call is dropped, not kept as '{}'."""
    msg, fr, usage = _run_sync(PHANTOM_DUP_TOOL_CALL)
    assert fr == 'tool_calls'
    # The empty phantom must be filtered out — only the real call survives.
    assert len(msg['tool_calls']) == 1
    assert msg['tool_calls'][0]['function']['arguments'] == '{"pattern":"foo"}'


def test_minimax_think_demux():
    msg, fr, usage = _run_sync(MINIMAX_THINK, model='MiniMax-M2.7')
    assert msg['content'] == 'answer'
    assert msg['reasoning_content'] == 'reasoning'


def test_missing_done_anomaly():
    result = _run_sync(MISSING_DONE)
    msg, fr, usage = result
    assert usage['_missing_done'] is True
    assert usage['_stream_anomaly'] is True
    assert usage['_chunks_received'] == 1
    assert result.state is ProviderStreamState.PREMATURE_CLOSE
    assert result.provider_finish_reason is None


def test_finish_frame_without_done_is_verified_completion():
    result = _run_sync(FINISH_WITHOUT_DONE)
    msg, finish, usage = result

    assert msg['content'] == 'complete'
    assert finish == 'stop'
    assert usage['_missing_done'] is True
    assert '_stream_anomaly' not in usage
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.provider_finish_reason == 'stop'
    assert result.is_verified_complete is True


def test_mid_json_close_preserves_complete_frames_and_flags_anomaly():
    """Never guess content out of a corrupt JSON frame; retry from the last
    byte that was carried by a complete, independently parseable event."""
    result = _run_sync(MID_JSON_CLOSE)
    msg, fr, usage = result

    assert msg['content'] == 'from the'
    assert ' guidance' not in msg['content']
    # Tuple compatibility still exposes ``stop``. The typed state retains the
    # authority; usage flags are only a migration relay for legacy callers.
    assert fr == 'stop'
    assert usage['_chunks_received'] == 2
    assert usage['_missing_done'] is True
    assert usage['_missing_finish_reason'] is True
    assert usage['_stream_anomaly'] is True
    assert usage['_malformed_frames'] == 1
    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert result.provider_finish_reason is None


def test_empty_stop_anomaly():
    result = _run_sync(EMPTY_STOP)
    msg, fr, usage = result
    assert usage['_empty_stop'] is True
    assert usage['_stream_anomaly'] is True
    assert fr == 'stop'
    assert msg.get('content', '') == ''
    assert result.state is ProviderStreamState.EMPTY_RESPONSE
    assert result.provider_finish_reason == 'stop'
    assert result.evidence.empty_response is True


def test_one_malformed_frame_cannot_be_laundered_by_later_finish():
    result = _run_sync([
        'data: {"choices":[{"delta":{"content":"safe prefix"}}]}',
        'data: {"choices":[{"delta":{"content":" dropped"}}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ])

    msg, finish, usage = result
    assert msg['content'] == 'safe prefix'
    assert finish == 'stop'  # tuple compatibility is not completion evidence
    assert result.provider_finish_reason == 'stop'
    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert result.is_verified_complete is False
    assert usage['_stream_state'] == 'malformed_stream'
    assert usage['_stream_anomaly'] is True


@pytest.mark.parametrize('translator', [
    AnthropicSSETranslator(model='claude-test'),
    ResponsesSSETranslator(model='responses-test'),
])
def test_translated_provider_invalid_json_is_typed_malformed(translator):
    body = {'model': 'provider-test', 'messages': []}
    accumulator = SSEAccumulator(
        body,
        'trace-invalid-json',
        RawSSEDumper('provider-test', 'trace-invalid-json', body),
        translator,
        time.monotonic(),
    )

    assert accumulator.feed_payload('{"broken":') is False
    result = accumulator.finalize()

    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert result.evidence.malformed_frame_count == 1
    assert result.evidence.diagnostics[0].startswith('invalid_json:')


def test_sse_error_429_raises_ratelimit_sync():
    with pytest.raises(RateLimitError):
        _run_sync(SSE_ERROR_429)


def test_sse_error_429_raises_ratelimit_async():
    with pytest.raises(RateLimitError):
        _run_async(SSE_ERROR_429)
