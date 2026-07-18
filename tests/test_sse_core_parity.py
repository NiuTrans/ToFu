"""Parity + characterization tests for the unified SSE streaming core.

Background
----------
``lib/llm/stream.py`` (sync, requests) and ``lib/llm/astream.py`` (async,
httpx) used to each carry a ~480-line copy of the identical SSE parsing
loop. They were collapsed onto ``lib/llm/_sse_core.py``. These tests lock
the behavior so the collapse is provably byte-for-byte:

1. **Parity** — the SAME recorded SSE transcript driven through the sync
   shell and the async shell yields the SAME ``(msg, finish_reason, usage)``
   (modulo the always-varying ``trace_id`` / ``stream_elapsed_ms``).
2. **Characterization** — known transcripts (normal, tool-call, MiniMax
   ``<think>`` demux, missing-[DONE], empty-stop, SSE-error-429) produce
   the expected message + the exact anomaly ``usage`` flags that
   ``lib/tasks_pkg/stream_handler.py`` keys its retry buckets off of.

No network: we monkeypatch ``requests.post`` and ``httpx.AsyncClient`` to
replay a fixed list of SSE lines.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm_errors import RateLimitError  # noqa: E402

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

# finish=stop but no content and no tool calls → empty_stop anomaly.
EMPTY_STOP = [
    'data: {"choices":[{"delta":{"role":"assistant"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}}]}',
    'data: [DONE]',
]

SSE_ERROR_429 = [
    'data: {"error":{"message":"Too many requests","http_code":429}}',
]


# ── Fake sync transport (requests.post) ──

class _FakeRequestsResp:
    def __init__(self, lines, status=200, headers=None):
        self._lines = lines
        self.status_code = status
        self.headers = headers or {}
        self.encoding = 'utf-8'
        self.text = '' if status == 200 else 'error body'

    def iter_lines(self, decode_unicode=True):
        for ln in self._lines:
            yield ln

    def close(self):
        pass


def _run_sync(lines, model='gpt-4', status=200):
    import lib.llm.stream as smod

    def fake_post(url, **kw):
        return _FakeRequestsResp(lines, status=status)

    class _FakeSession:
        post = staticmethod(fake_post)

    orig = smod.get_sync_session
    smod.get_sync_session = lambda: _FakeSession()
    try:
        body = {'model': model, 'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 100}
        return smod._stream_chat_once(body, log_prefix='[test]')
    finally:
        smod.get_sync_session = orig


# ── Fake async transport (httpx.AsyncClient.stream) ──

class _FakeAsyncStreamCtx:
    def __init__(self, lines, status=200, headers=None):
        self._lines = lines
        self.status_code = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b'error body'


class _FakeAsyncClient:
    def __init__(self, lines, status=200):
        self._lines = lines
        self._status = status

    def __init_subclass__(cls):  # pragma: no cover
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        return _FakeAsyncStreamCtx(self._lines, status=self._status)


def _run_async(lines, model='gpt-4', status=200):
    import lib.llm.astream as amod

    def fake_client_factory(*a, **kw):
        return _FakeAsyncClient(lines, status=status)

    orig = amod.get_async_client
    amod.get_async_client = fake_client_factory
    try:
        body = {'model': model, 'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 100}
        return asyncio.run(
            amod._async_stream_chat_once(body, log_prefix='[test]'))
    finally:
        amod.get_async_client = orig


# ── Normalize: drop the always-varying fields before comparing ──

_VARYING = {'trace_id', 'resp_trace_id', 'stream_elapsed_ms'}


def _norm_usage(usage):
    return {k: v for k, v in (usage or {}).items() if k not in _VARYING}


# ── Parity tests: sync vs async produce identical output ──

@pytest.mark.parametrize('name,lines,model', [
    ('normal', NORMAL, 'gpt-4'),
    ('tool_call', TOOL_CALL, 'gpt-4'),
    ('minimax_think', MINIMAX_THINK, 'MiniMax-M2.7'),
    ('missing_done', MISSING_DONE, 'gpt-4'),
    ('empty_stop', EMPTY_STOP, 'gpt-4'),
])
def test_sync_async_parity(name, lines, model):
    msg_s, fr_s, usage_s = _run_sync(lines, model=model)
    msg_a, fr_a, usage_a = _run_async(lines, model=model)
    assert msg_s == msg_a, f'{name}: assistant msg differs'
    assert fr_s == fr_a, f'{name}: finish_reason differs'
    assert _norm_usage(usage_s) == _norm_usage(usage_a), f'{name}: usage differs'


# ── Characterization tests: exact expected output ──

def test_normal_content():
    msg, fr, usage = _run_sync(NORMAL)
    assert msg == {'role': 'assistant', 'content': 'Hello world'}
    assert fr == 'stop'
    assert usage['prompt_tokens'] == 10
    assert usage['_chunks_received'] == 3
    assert '_stream_anomaly' not in usage


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
    msg, fr, usage = _run_sync(MISSING_DONE)
    assert usage['_missing_done'] is True
    assert usage['_stream_anomaly'] is True
    assert usage['_chunks_received'] == 1


def test_empty_stop_anomaly():
    msg, fr, usage = _run_sync(EMPTY_STOP)
    assert usage['_empty_stop'] is True
    assert usage['_stream_anomaly'] is True
    assert fr == 'stop'
    assert msg.get('content', '') == ''


def test_sse_error_429_raises_ratelimit_sync():
    with pytest.raises(RateLimitError):
        _run_sync(SSE_ERROR_429)


def test_sse_error_429_raises_ratelimit_async():
    with pytest.raises(RateLimitError):
        _run_async(SSE_ERROR_429)
