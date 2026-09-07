"""Native-Codex-style rolling stream-activity timeout contracts.

The default 300-second product bound is ``IDLE_STREAM_TIMEOUT_S``. Every
received transport event renews it, including SSE comments/keep-alives and
WebSocket protocol messages. It is never a total request deadline. The socket
read timeout remains ``None`` and a genuinely silent attempt finalizes through
the existing premature-close recovery path.

The old semantic clock remains import-compatible for stored/plugin adapters,
but production transports never arm it as a termination condition.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm._sse_core import SSEAccumulator  # noqa: E402
from lib.llm._transport import (  # noqa: E402
    SemanticStallClock,
    StreamIdleWatchdog,
)
from lib.llm.anthropic_outbound import AnthropicSSETranslator  # noqa: E402
from lib.llm.responses_outbound import ResponsesSSETranslator  # noqa: E402
from lib.llm.stream_result import ProviderStreamState  # noqa: E402

pytestmark = pytest.mark.unit


class TestStreamIdleWatchdogIdleTimeout:
    @pytest.mark.parametrize(('configured', 'expected'), [
        (None, 300.0),
        ('91', 91.0),
        ('0', 0.0),
        ('-1', 0.0),
        ('12', 30.0),
        ('invalid', 300.0),
        ('inf', 300.0),
        ('nan', 300.0),
    ])
    def test_stream_idle_window_configuration(
            self, configured, expected):
        env = dict(os.environ)
        env.pop('TOFU_LLM_SEMANTIC_IDLE_TIMEOUT_S', None)
        env.pop('TOFU_LLM_NO_ACTIONABLE_TIMEOUT_S', None)
        if configured is None:
            env.pop('TOFU_LLM_IDLE_STREAM_TIMEOUT_S', None)
        else:
            env['TOFU_LLM_IDLE_STREAM_TIMEOUT_S'] = configured
        probe = subprocess.run(
            [sys.executable, '-c',
             'from lib.llm._transport import ('
             'IDLE_STREAM_TIMEOUT_S, SEMANTIC_IDLE_TIMEOUT_S); '
             'print(IDLE_STREAM_TIMEOUT_S, SEMANTIC_IDLE_TIMEOUT_S)'],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert probe.stdout.strip() == f'{expected} 0.0'

    def test_canonical_stream_idle_timeout_has_priority_over_legacy_alias(self):
        env = dict(os.environ)
        env['TOFU_LLM_IDLE_STREAM_TIMEOUT_S'] = '91'
        env['TOFU_LLM_SEMANTIC_IDLE_TIMEOUT_S'] = '123'
        env['TOFU_LLM_NO_ACTIONABLE_TIMEOUT_S'] = '123'
        probe = subprocess.run(
            [sys.executable, '-c',
             'from lib.llm._transport import IDLE_STREAM_TIMEOUT_S; '
             'print(IDLE_STREAM_TIMEOUT_S)'],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert probe.stdout.strip() == '91.0'
        assert 'deprecated' not in probe.stderr.lower()

    def test_legacy_semantic_timeout_alias_migrates_to_transport_idle(self):
        env = dict(os.environ)
        env.pop('TOFU_LLM_IDLE_STREAM_TIMEOUT_S', None)
        env.pop('TOFU_LLM_NO_ACTIONABLE_TIMEOUT_S', None)
        env['TOFU_LLM_SEMANTIC_IDLE_TIMEOUT_S'] = '123'
        probe = subprocess.run(
            [sys.executable, '-c',
             'from lib.llm._transport import ('
             'IDLE_STREAM_TIMEOUT_S, SEMANTIC_IDLE_TIMEOUT_S); '
             'print(IDLE_STREAM_TIMEOUT_S, SEMANTIC_IDLE_TIMEOUT_S)'],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert probe.stdout.strip() == '123.0 0.0'
        assert probe.stderr.lower().count('deprecated') == 1

    def test_idle_timeout_fires_and_latches(self):
        fired = []
        wd = StreamIdleWatchdog(idle_timeout=0.2,
                                on_idle_timeout=lambda: fired.append(1))
        wd.start()
        time.sleep(0.6)
        assert wd.idle_timed_out is True, 'idle timeout did not latch'
        assert fired == [1], 'on_idle_timeout did not fire'
        wd.cancel()

    def test_watchdog_without_explicit_idle_timeout_does_not_kill(self):
        beats = []
        wd = StreamIdleWatchdog(heartbeat_interval=0.05,
                                on_beat=lambda idle: beats.append(idle),
                                abort_check=lambda: False)
        wd.start()
        time.sleep(0.4)
        assert wd.idle_timed_out is False, 'default (no idle_timeout) must not kill'
        assert len(beats) >= 2, 'beats should still fire'
        wd.cancel()

    def test_activity_resets_idle_timeout(self):
        wd = StreamIdleWatchdog(idle_timeout=0.15)
        wd.start()
        for _ in range(6):
            wd.notify_activity()
            time.sleep(0.05)
        assert wd.idle_timed_out is False, 'activity must keep the stream alive'
        time.sleep(0.4)
        assert wd.idle_timed_out is True, 'silence after activity must still time out'
        wd.cancel()

    def test_keepalive_activity_renews_stream_idle_window(self):
        semantic_fired = []
        wd = StreamIdleWatchdog(
            idle_timeout=0.18,
            actionable_timeout=0.10,
            on_actionable_timeout=lambda: semantic_fired.append(1),
        )
        wd.start()
        for _ in range(8):
            wd.notify_activity()  # transport bytes / keep-alives only
            time.sleep(0.04)
        assert wd.idle_timed_out is False
        assert wd.actionable_timed_out is False
        assert semantic_fired == []
        time.sleep(0.25)
        assert wd.idle_timed_out is True
        wd.cancel()

    def test_semantic_progress_alone_does_not_replace_transport_activity(self):
        wd = StreamIdleWatchdog(
            idle_timeout=0.18,
        )
        wd.start()
        for _ in range(6):
            wd.notify_reasoning_progress('diagnostic-only reasoning')
            time.sleep(0.04)
        assert wd.idle_timed_out is True
        wd.cancel()


class _ManualMonotonic:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class TestLegacySemanticStallClockTimeline:
    def test_reasoning_every_299_seconds_can_run_for_hours(self):
        monotonic = _ManualMonotonic()
        progress = SemanticStallClock(300, monotonic=monotonic)

        for _ in range(25):
            monotonic.advance(299)
            assert progress.timed_out() is False
            assert progress.notify_reasoning_progress('reasoning step') is True

        snapshot = progress.snapshot()
        assert snapshot['request_elapsed_s'] > 2 * 60 * 60
        assert snapshot['last_progress_age_s'] == 0
        assert snapshot['reasoning_chunks'] == 25

    def test_last_reasoning_boundary_is_inclusive_at_300_seconds(self):
        monotonic = _ManualMonotonic()
        progress = SemanticStallClock(300, monotonic=monotonic)
        progress.notify_reasoning_progress('fresh')

        monotonic.advance(299.9)
        assert progress.timed_out() is False
        monotonic.advance(0.1)
        assert progress.timed_out() is True

    def test_total_request_age_does_not_override_fresh_reasoning(self):
        monotonic = _ManualMonotonic()
        progress = SemanticStallClock(300, monotonic=monotonic)
        for _ in range(12):
            monotonic.advance(250)
            progress.notify_reasoning_progress('still working')
        monotonic.advance(299.9)

        assert progress.snapshot()['request_elapsed_s'] > 3_299
        assert progress.timed_out() is False

    @pytest.mark.parametrize('non_progress', ['', ' ', '\n\t'])
    def test_blank_reasoning_cannot_renew_the_window(self, non_progress):
        monotonic = _ManualMonotonic()
        progress = SemanticStallClock(300, monotonic=monotonic)
        monotonic.advance(299)
        assert progress.notify_reasoning_progress(non_progress) is False
        monotonic.advance(1)

        assert progress.timed_out() is True
        assert progress.snapshot()['reasoning_chunks'] == 0

    def test_actionable_output_renews_the_rolling_window(self):
        monotonic = _ManualMonotonic()
        progress = SemanticStallClock(300, monotonic=monotonic)
        monotonic.advance(299.9)
        progress.notify_actionable_output()
        monotonic.advance(299.9)

        assert progress.timed_out() is False
        assert progress.remaining_seconds() == pytest.approx(0.1)
        monotonic.advance(0.1)
        assert progress.timed_out() is True

    def test_headers_and_transport_events_do_not_renew_semantic_progress(self):
        monotonic = _ManualMonotonic()
        progress = SemanticStallClock(300, monotonic=monotonic)
        monotonic.advance(299)
        progress.mark_response_headers()
        progress.mark_transport_bytes(100)
        progress.mark_sse_event()
        monotonic.advance(1)

        assert progress.timed_out() is True
        evidence = progress.evidence()
        assert evidence.response_headers_seen is True
        assert evidence.transport_byte_count == 100
        assert evidence.sse_event_count == 1

    def test_provider_finish_is_terminal_evidence_not_semantic_progress(self):
        monotonic = _ManualMonotonic()
        progress = SemanticStallClock(300, monotonic=monotonic)
        monotonic.advance(299)
        progress.mark_provider_finish()

        assert progress.timed_out() is False
        evidence = progress.evidence()
        assert evidence.provider_finish_seen is True
        assert evidence.last_semantic_progress_age_ms == 299_000

    def test_content_and_valid_tool_progress_each_renew_the_window(self):
        monotonic = _ManualMonotonic()
        monotonic.advance(299)
        content_progress = SemanticStallClock(300, monotonic=monotonic)
        tool_progress = SemanticStallClock(300, monotonic=monotonic)
        argument_progress = SemanticStallClock(300, monotonic=monotonic)

        assert content_progress.mark_content('answer') is True
        assert tool_progress.mark_tool_delta(recognized=True) is True
        assert argument_progress.mark_tool_delta(argument_delta='{"x":') is True
        monotonic.advance(299.9)

        assert content_progress.timed_out() is False
        assert tool_progress.timed_out() is False
        assert argument_progress.timed_out() is False
        assert content_progress.evidence().content_chunks == 1
        assert tool_progress.evidence().tool_call_count == 1
        assert argument_progress.evidence().tool_argument_chunks == 1

        monotonic.advance(0.1)
        assert content_progress.timed_out() is True
        assert tool_progress.timed_out() is True
        assert argument_progress.timed_out() is True

    def test_empty_tool_shell_and_user_abort_are_not_progress(self):
        monotonic = _ManualMonotonic()
        progress = SemanticStallClock(300, monotonic=monotonic)
        monotonic.advance(299)
        assert progress.mark_tool_delta() is False
        monotonic.advance(1)
        assert progress.timed_out() is True
        progress.mark_client_aborted()
        assert progress.timed_out() is False


class _IdleWedgedResp:
    """200 OK then zero body bytes — the silent-then-die incident shape."""

    headers = {}
    status_code = 200
    encoding = None

    def __init__(self):
        self._closed = threading.Event()

    def iter_lines(self, decode_unicode=False):
        self._closed.wait(timeout=10)
        raise ValueError('I/O operation on closed file')
        yield  # pragma: no cover

    def close(self):
        self._closed.set()


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def post(self, *a, **k):
        return self._resp


class _FiniteResp:
    headers = {}
    status_code = 200
    encoding = None

    def __init__(self, lines):
        self.lines = list(lines)

    def iter_lines(self, decode_unicode=False):
        yield from self.lines

    def close(self):
        return None


class _RecordingDumper:
    def __init__(self):
        self.anomalies = []

    def line(self, _line):
        return None

    def dump_anomaly(self, reason, **_fields):
        self.anomalies.append(reason)

    def finish(self, **_fields):
        return None


def _data(payload):
    return 'data: ' + json.dumps(payload, ensure_ascii=False)


def _semantic_protocol_case(protocol):
    if protocol == 'openai':
        translator = None
        prelude = []
        reasoning = lambda text: _data({
            'id': 'chatcmpl-kimi-k3',
            'object': 'chat.completion.chunk',
            'model': 'kimi-k3',
            'choices': [{
                'index': 0,
                'delta': {'reasoning_content': text},
                'finish_reason': None,
            }],
        })
        terminal = [
            _data({'choices': [{'index': 0, 'delta': {'tool_calls': [{
                'index': 0,
                'id': 'call_kimi',
                'type': 'function',
                'function': {'name': 'get_status', 'arguments': '{}'},
            }]}, 'finish_reason': None}]}),
            _data({'choices': [{
                'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}),
            'data: [DONE]',
        ]
    elif protocol == 'anthropic':
        translator = AnthropicSSETranslator(model='claude-test')
        prelude = [_data({
            'type': 'content_block_start',
            'index': 0,
            'content_block': {'type': 'thinking', 'thinking': ''},
        })]
        reasoning = lambda text: _data({
            'type': 'content_block_delta',
            'index': 0,
            'delta': {'type': 'thinking_delta', 'thinking': text},
        })
        terminal = [
            _data({
                'type': 'content_block_start',
                'index': 1,
                'content_block': {
                    'type': 'tool_use', 'id': 'call_anthropic',
                    'name': 'get_status', 'input': {},
                },
            }),
            _data({
                'type': 'message_delta',
                'delta': {'stop_reason': 'tool_use'},
                'usage': {'output_tokens': 8},
            }),
            _data({'type': 'message_stop'}),
        ]
    else:
        translator = ResponsesSSETranslator(model='responses-test')
        prelude = []
        reasoning = lambda text: _data({
            'type': 'response.reasoning_text.delta', 'delta': text})
        terminal = [
            _data({'type': 'response.output_text.delta', 'delta': 'done'}),
            _data({
                'type': 'response.completed',
                'response': {
                    'id': 'resp_test',
                    'output': [],
                    'usage': {'input_tokens': 10, 'output_tokens': 8},
                },
            }),
        ]
    return translator, prelude, reasoning, terminal


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_normalized_reasoning_renews_same_cross_protocol_window(protocol):
    monotonic = _ManualMonotonic()
    progress = SemanticStallClock(300, monotonic=monotonic)
    translator, prelude, reasoning, terminal = _semantic_protocol_case(protocol)
    body = {
        'model': 'kimi-k3' if protocol == 'openai' else f'{protocol}-test',
        'messages': [],
        'tools': [{
            'type': 'function',
            'function': {
                'name': 'get_status', 'description': '',
                'parameters': {'type': 'object', 'properties': {}},
            },
        }],
    }
    dumper = _RecordingDumper()
    acc = SSEAccumulator(
        body, f'trace-{protocol}', dumper, translator, time.time(),
        progress=progress,
    )
    for line in prelude:
        acc.feed_line(line)

    # Four semantic deltas span 1,196 seconds in total. The former absolute
    # 300-second deadline would have killed this healthy stream after delta 1.
    for index in range(4):
        monotonic.advance(299)
        assert progress.timed_out() is False
        acc.feed_line(reasoning(f'step-{index}'))
        assert progress.timed_out() is False

    for line in terminal:
        if acc.feed_line(line):
            break
    monotonic.advance(6 * 60 * 60)
    assert progress.timed_out() is False

    result = acc.finalize()
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.usage['_stream_state'] == 'provider_finished'
    assert '_no_actionable_timeout' not in result.usage
    assert dumper.anomalies == []
    assert progress.snapshot()['reasoning_chunks'] == 4
    assert (result.message.get('content') == 'done'
            or result.message.get('tool_calls'))


def test_protocol_noise_cannot_counterfeit_semantic_progress():
    monotonic = _ManualMonotonic()
    progress = SemanticStallClock(300, monotonic=monotonic)
    acc = SSEAccumulator(
        {'model': 'kimi-k3', 'messages': []},
        'trace-noise', _RecordingDumper(), None, time.time(),
        on_reasoning_progress=progress.notify_reasoning_progress,
        on_actionable_output=progress.notify_actionable_output,
    )
    non_progress_frames = [
        _data({'choices': [{'delta': {'reasoning_content': ' \n\t'}}]}),
        _data({'choices': [{'delta': {'thinking_signature': 'opaque'}}]}),
        'data: ',
        ': keep-alive',
        _data({'choices': [{'delta': {'role': 'assistant'}}]}),
    ]
    for line in non_progress_frames:
        monotonic.advance(50)
        acc.feed_line(line)

    monotonic.advance(49.9)
    assert progress.timed_out() is False
    monotonic.advance(0.1)
    assert progress.timed_out() is True
    assert progress.snapshot()['reasoning_chunks'] == 0


def test_legacy_parser_clock_does_not_treat_keepalive_as_semantic_progress():
    monotonic = _ManualMonotonic()
    progress = SemanticStallClock(300, monotonic=monotonic)
    acc = SSEAccumulator(
        {'model': 'kimi-k3', 'messages': []},
        'trace-keepalive', _RecordingDumper(), None, time.time(),
        on_reasoning_progress=progress.notify_reasoning_progress,
        on_actionable_output=progress.notify_actionable_output,
    )
    acc.feed_line(_data({
        'choices': [{'delta': {'reasoning_content': 'last real thought'}}]}))
    for _ in range(5):
        monotonic.advance(60)
        acc.feed_line(': keep-alive')

    assert progress.timed_out() is True
    assert progress.snapshot()['last_progress_age_s'] == 300


class _ReasoningThenToolResp(_IdleWedgedResp):
    """Kimi-compatible reasoning outlives the old deadline, then uses a tool."""

    def iter_lines(self, decode_unicode=False):
        line = _data({
            'id': 'chatcmpl-kimi-k3',
            'object': 'chat.completion.chunk',
            'model': 'kimi-k3',
            'choices': [{
                'index': 0,
                'delta': {'reasoning_content': 'x'},
                'finish_reason': None,
            }],
        })
        started = time.monotonic()
        while (not self._closed.is_set()
               and time.monotonic() - started < 0.42):
            yield line
            time.sleep(0.02)
        if self._closed.is_set():
            return
        yield _data({'choices': [{'index': 0, 'delta': {'tool_calls': [{
            'index': 0,
            'id': 'call_kimi',
            'type': 'function',
            'function': {'name': 'get_status', 'arguments': '{}'},
        }]}, 'finish_reason': None}]})
        yield _data({'choices': [{
            'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})
        yield 'data: [DONE]'


class _ReasoningThenKeepAliveResp(_IdleWedgedResp):
    """Protocol-only activity outlives the window, then completes normally."""

    def iter_lines(self, decode_unicode=False):
        yield _data({
            'choices': [{
                'index': 0,
                'delta': {'reasoning_content': 'last thought'},
                'finish_reason': None,
            }],
        })
        started = time.monotonic()
        while (not self._closed.is_set()
               and time.monotonic() - started < 0.42):
            yield ': keep-alive'
            time.sleep(0.02)
        if self._closed.is_set():
            return
        yield _data({'choices': [{
            'index': 0,
            'delta': {'content': 'finished'},
            'finish_reason': None,
        }]})
        yield _data({'choices': [{
            'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
        yield 'data: [DONE]'


def test_sync_silent_stream_finalizes_as_premature_close(monkeypatch):
    monkeypatch.setattr('lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.3)
    monkeypatch.setattr('lib.llm._transport.ABORT_POLL_INTERVAL', 0.05)
    monkeypatch.setattr('lib.llm._transport.IDLE_HEARTBEAT_S', 0.05)
    monkeypatch.setattr('lib.llm.stream.get_sync_session',
                        lambda: _FakeSession(_IdleWedgedResp()))
    import lib.desktop.egress as _eg
    monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')

    from lib.llm.stream import _stream_chat_once

    t0 = time.monotonic()
    msg, finish, usage = _stream_chat_once(
        {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
        api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, (
        f'silent stream was not cut short (took {elapsed:.1f}s)')
    assert usage.get('_missing_done') is True, (
        'idle-timeout close must be classified as a premature close')
    assert finish == 'stop'
    assert (msg.get('content') or '') == ''


@pytest.mark.parametrize(('lines', 'expected_ok'), [
    ([
        'data: {"choices":[{"delta":{"content":"ok"},'
        '"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ], True),
    ([
        'data: {"choices":[{"delta":{"content":"partial"},'
        '"finish_reason":null}]}',
    ], False),
    ([
        'data: {"choices":[{"delta":{"content":"complete"},'
        '"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    ], True),
    ([
        'data: {"choices":[{"delta":{"content":"safe prefix"}}]}',
        'data: {"choices":[{"delta":{"content":"broken"}}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ], True),
    ([
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ], True),
    ([
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        'data: [DONE]',
    ], True),
])
def test_route_health_settles_from_complete_stream_not_headers(
        monkeypatch, lines, expected_ok):
    outcomes = []
    monkeypatch.setattr('lib.llm.stream.get_sync_session',
                        lambda: _FakeSession(_FiniteResp(lines)))
    monkeypatch.setattr(
        'lib.llm.stream._proxy_report_outcome',
        lambda _url, ok, _latency=None, **_kwargs: outcomes.append(ok),
    )
    import lib.desktop.egress as _eg
    monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')

    from lib.llm.stream import _stream_chat_once

    _stream_chat_once(
        {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
        api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')

    assert outcomes == [expected_ok]


def test_async_finish_without_done_keeps_route_healthy(monkeypatch):
    import contextlib

    import lib.desktop.egress as _eg
    from lib.llm import astream as async_stream

    class AsyncFiniteResponse:
        status_code = 200
        headers = {}
        extensions = {
            'tofu_network_route': {
                'routeId': 'direct:test',
                'routeMode': 'direct',
                'decisionReason': 'test',
            },
            'tofu_network_latency_ms': 1.0,
        }

        async def aiter_lines(self):
            yield ('data: {"choices":[{"delta":{"content":"complete"},'
                   '"finish_reason":null}]}')
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'

        async def aclose(self):
            return None

    @contextlib.asynccontextmanager
    async def open_stream(_plan, _log_prefix=''):
        yield AsyncFiniteResponse(), None

    outcomes = []
    monkeypatch.setattr(async_stream, '_open_server_stream', open_stream)
    monkeypatch.setattr(
        async_stream,
        '_proxy_report_outcome',
        lambda _url, ok, _latency=None, **_kwargs: outcomes.append(ok),
    )
    monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')

    async def run():
        return await async_stream._async_stream_chat_once(
            {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
            api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')

    result = asyncio.run(run())
    assert result.is_verified_complete is True
    assert result.saw_done is False
    assert outcomes == [True]


def test_sync_reasoning_activity_outlives_idle_window_and_finishes(
        monkeypatch):
    from lib.llm import diagnostics

    anomaly_blocks = []
    monkeypatch.setattr(
        diagnostics,
        '_append_anomaly',
        lambda _path, block: anomaly_blocks.append(block),
    )
    monkeypatch.setattr('lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.15)
    monkeypatch.setattr('lib.llm._transport.ABORT_POLL_INTERVAL', 0.03)
    monkeypatch.setattr('lib.llm.stream.get_sync_session',
                        lambda: _FakeSession(_ReasoningThenToolResp()))
    import lib.desktop.egress as _eg
    monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')

    from lib.llm.stream import _stream_chat_once

    started = time.monotonic()
    result = _stream_chat_once(
        {
            'model': 'kimi-k3',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [{
                'type': 'function',
                'function': {
                    'name': 'get_status', 'description': '',
                    'parameters': {'type': 'object', 'properties': {}},
                },
            }],
        },
        api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')
    msg, finish, usage = result

    assert time.monotonic() - started < 3.0
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert usage['_stream_state'] == 'provider_finished'
    assert '_no_actionable_timeout' not in usage
    assert '_failure_stage' not in usage
    assert finish == 'tool_calls'
    assert msg.get('tool_calls')
    assert len(msg.get('reasoning_content') or '') > 0
    assert anomaly_blocks == []


def test_async_reasoning_activity_outlives_idle_window_and_finishes(
        monkeypatch):
    import lib.llm.astream as async_stream
    import lib.desktop.egress as _eg

    class AsyncReasoningThenContentResp:
        status_code = 200
        headers = {}

        def __init__(self):
            self.extensions = {
                'tofu_network_route': {
                    'routeId': 'direct:test', 'routeMode': 'direct',
                    'decisionReason': 'test',
                },
                'tofu_network_latency_ms': 1.0,
            }
            self.closed = False

        async def aiter_lines(self):
            line = _data({
                'choices': [{
                    'index': 0,
                    'delta': {'reasoning_content': 'x'},
                    'finish_reason': None,
                }],
            })
            started = time.monotonic()
            while (not self.closed
                   and time.monotonic() - started < 0.42):
                yield line
                await asyncio.sleep(0.02)
            if self.closed:
                return
            yield _data({'choices': [{
                'index': 0,
                'delta': {'content': 'finished'},
                'finish_reason': None,
            }]})
            yield _data({'choices': [{
                'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
            yield 'data: [DONE]'

        async def aclose(self):
            self.closed = True

    response = AsyncReasoningThenContentResp()

    @contextlib.asynccontextmanager
    async def open_stream(_plan, _log_prefix=''):
        yield response, None

    monkeypatch.setattr('lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.15)
    monkeypatch.setattr(async_stream, '_open_server_stream', open_stream)
    monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')
    route_outcomes = []
    monkeypatch.setattr(
        async_stream,
        '_proxy_report_outcome',
        lambda _url, ok, _latency=None, **_kwargs: route_outcomes.append(ok),
    )

    async def run():
        return await async_stream._async_stream_chat_once(
            {'model': 'm',
             'messages': [{'role': 'user', 'content': 'hi'}]},
            api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')

    result = asyncio.run(run())
    msg, finish, usage = result
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert usage['_stream_state'] == 'provider_finished'
    assert '_no_actionable_timeout' not in usage
    assert '_failure_stage' not in usage
    assert finish == 'stop'
    assert msg.get('content') == 'finished'
    assert len(msg.get('reasoning_content') or '') > 0
    assert route_outcomes == [True]


def test_sync_keepalive_only_interval_renews_stream_idle_window(monkeypatch):
    from lib.llm import diagnostics
    from lib.llm.stream import _stream_chat_once

    anomaly_blocks = []
    monkeypatch.setattr(
        diagnostics,
        '_append_anomaly',
        lambda _path, block: anomaly_blocks.append(block),
    )
    monkeypatch.setattr('lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.15)
    monkeypatch.setattr('lib.llm._transport.ABORT_POLL_INTERVAL', 0.03)
    monkeypatch.setattr(
        'lib.llm.stream.get_sync_session',
        lambda: _FakeSession(_ReasoningThenKeepAliveResp()),
    )
    import lib.desktop.egress as _eg
    monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')
    route_outcomes = []
    monkeypatch.setattr(
        'lib.llm.stream._proxy_report_outcome',
        lambda _url, ok, _latency=None, **_kwargs: route_outcomes.append(ok),
    )

    result = _stream_chat_once(
        {'model': 'kimi-k3',
         'messages': [{'role': 'user', 'content': 'hi'}]},
        api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')

    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    usage = result.usage
    assert usage['_stream_state'] == 'provider_finished'
    assert '_failure_stage' not in usage
    assert '_no_actionable_timeout' not in usage
    assert '_semantic_progress_timeout' not in usage
    assert result.message['content'] == 'finished'
    assert route_outcomes == [True]
    assert anomaly_blocks == []


def test_async_keepalive_only_interval_renews_stream_idle_window(monkeypatch):
    import lib.desktop.egress as _eg
    from lib.llm import astream as async_stream

    class AsyncReasoningThenKeepAliveResp:
        status_code = 200
        headers = {}

        def __init__(self):
            self.extensions = {
                'tofu_network_route': {
                    'routeId': 'direct:test', 'routeMode': 'direct',
                    'decisionReason': 'test',
                },
                'tofu_network_latency_ms': 1.0,
            }
            self.closed = False

        async def aiter_lines(self):
            yield _data({
                'choices': [{
                    'index': 0,
                    'delta': {'reasoning_content': 'last thought'},
                    'finish_reason': None,
                }],
            })
            started = time.monotonic()
            while (not self.closed
                   and time.monotonic() - started < 0.42):
                yield ': keep-alive'
                await asyncio.sleep(0.02)
            if self.closed:
                return
            yield _data({'choices': [{
                'index': 0,
                'delta': {'content': 'finished'},
                'finish_reason': None,
            }]})
            yield _data({'choices': [{
                'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
            yield 'data: [DONE]'

        async def aclose(self):
            self.closed = True

    response = AsyncReasoningThenKeepAliveResp()

    @contextlib.asynccontextmanager
    async def open_stream(_plan, _log_prefix=''):
        yield response, None

    monkeypatch.setattr('lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.15)
    monkeypatch.setattr(async_stream, '_open_server_stream', open_stream)
    monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')
    route_outcomes = []
    monkeypatch.setattr(
        async_stream,
        '_proxy_report_outcome',
        lambda _url, ok, _latency=None, **_kwargs: route_outcomes.append(ok),
    )

    async def run():
        return await async_stream._async_stream_chat_once(
            {'model': 'kimi-k3',
             'messages': [{'role': 'user', 'content': 'hi'}]},
            api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')

    result = asyncio.run(run())
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    usage = result.usage
    assert usage['_stream_state'] == 'provider_finished'
    assert '_failure_stage' not in usage
    assert '_no_actionable_timeout' not in usage
    assert '_semantic_progress_timeout' not in usage
    assert result.message['content'] == 'finished'
    assert route_outcomes == [True]
