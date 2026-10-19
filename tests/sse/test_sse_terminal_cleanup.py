"""Post-terminal transport cleanup cannot change model semantics or grow forever."""
from __future__ import annotations

import asyncio

import httpx
import pytest
import requests

from lib.llm import _transport as transport
from lib.llm.stream_result import ProviderStreamState
from lib.llm.llm_errors import AbortedError
from lib.desktop.egress import EgressUnavailable
from tests.sse.test_sse_core_parity import _FakeAsyncStreamCtx, _run_async, _run_sync
from tests.sse.test_sse_terminal_boundary import _response

pytestmark = pytest.mark.unit


def _drain_sync(chunks, **kwargs):
    watcher = transport.StreamIdleWatchdog()
    try:
        return transport.drain_completed_sync_stream(
            iter(chunks), response=None, watchdog=watcher, **kwargs)
    finally:
        watcher.cancel()
        watcher._thread.join(1)
        assert not watcher._thread.is_alive()


@pytest.mark.parametrize('chunk', [b'x', b'x' * (32 << 10)])
def test_sync_endless_tail_has_byte_and_iteration_bounds(chunk):
    reads = []

    def tail():
        while True:
            reads.append(len(chunk))
            yield chunk

    assert _drain_sync(tail()) is False
    assert 0 < len(reads) <= transport.POST_TERMINAL_DRAIN_MAX_CHUNKS
    assert sum(reads) <= transport.POST_TERMINAL_DRAIN_MAX_BYTES


def test_sync_cleanup_preserves_abort_and_rejects_oversized_capture():
    with pytest.raises(AbortedError):
        _drain_sync([b'tail'], abort_check=lambda: True)
    captured = []
    assert _drain_sync([b'x' * (transport.POST_TERMINAL_DRAIN_MAX_BYTES + 1)],
                       on_chunk=captured.append) is False
    assert captured == []


@pytest.mark.parametrize('error', [TimeoutError, ValueError])
def test_sync_cleanup_does_not_hide_callback_errors(error):
    def callback(chunk):
        raise error('callback failed')

    with pytest.raises(error, match='callback failed'):
        _drain_sync([b'tail'], on_chunk=callback)


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
@pytest.mark.parametrize('error', [requests.exceptions.ChunkedEncodingError,
                                 EgressUnavailable])
def test_sync_late_transport_error_cannot_retry_completed_work(protocol, error):
    prefix, terminal = _response(protocol)
    reads = []

    def tail():
        yield prefix + terminal
        reads.append('cleanup')
        raise error('late disconnect')

    result = _run_sync([], protocol=protocol, raw_chunks=tail())
    assert reads == ['cleanup']
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == 'complete 世界'
    assert '_failure_stage' not in result.usage


def test_sync_bounded_tail_releases_reader_without_reparsing(monkeypatch):
    monkeypatch.setattr(transport, 'POST_TERMINAL_DRAIN_MAX_CHUNKS', 2)
    prefix, terminal = _response('responses')
    closed = []

    def tail():
        try:
            yield prefix + terminal
            while True:
                yield b'invalid trailing bytes'
        finally:
            closed.append(True)

    result = _run_sync([], protocol='responses', raw_chunks=tail())
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert closed == [True]


def test_small_tail_is_drained_to_eof_for_connection_reuse():
    observed = []

    async def tail():
        yield b'data: [DONE]\n\n'
        observed.append('eof')

    assert asyncio.run(transport.drain_completed_stream(tail())) is True
    assert observed == ['eof']


@pytest.mark.parametrize('chunk', [b'x', b'x' * (32 << 10)],
                         ids=['iteration-budget', 'byte-budget'])
def test_endless_tail_has_both_byte_and_iteration_bounds(chunk):
    reads = []

    async def tail():
        while True:
            reads.append(len(chunk))
            yield chunk

    async def run():
        chunks = tail()
        try:
            return await transport.drain_completed_stream(chunks)
        finally:
            await chunks.aclose()

    assert asyncio.run(run()) is False
    assert 0 < len(reads) <= transport.POST_TERMINAL_DRAIN_MAX_CHUNKS
    assert sum(reads) <= transport.POST_TERMINAL_DRAIN_MAX_BYTES


def test_silent_tail_uses_fixed_deadline_and_cancels_pending_read(monkeypatch):
    monkeypatch.setattr(transport, 'POST_TERMINAL_DRAIN_TIMEOUT_S', 0.02)
    closed = []

    async def tail():
        try:
            await asyncio.Event().wait()
            yield b'never'
        finally:
            closed.append(True)

    assert asyncio.run(transport.drain_completed_stream(tail())) is False
    assert closed == [True]


def test_oversized_tail_is_not_retained_by_capture_callback():
    captured = []

    async def tail():
        yield b'x' * (transport.POST_TERMINAL_DRAIN_MAX_BYTES + 1)

    assert asyncio.run(transport.drain_completed_stream(
        tail(), on_chunk=captured.append)) is False
    assert captured == []


@pytest.mark.parametrize('error', [TimeoutError, ValueError])
def test_cleanup_does_not_hide_callback_errors(error):
    async def tail():
        yield b'trailer'

    def callback(chunk):
        raise error('callback failed')

    with pytest.raises(error):
        asyncio.run(transport.drain_completed_stream(tail(), on_chunk=callback))


def test_user_abort_is_not_swallowed_by_cleanup():
    async def tail():
        yield b'unread'

    with pytest.raises(AbortedError):
        asyncio.run(transport.drain_completed_stream(tail(), abort_check=lambda: True))


def test_cleanup_does_not_hide_iterator_programming_errors():
    async def tail():
        raise ValueError('invalid iterator')
        yield b'never'

    with pytest.raises(ValueError, match='invalid iterator'):
        asyncio.run(transport.drain_completed_stream(tail()))


def test_task_cancellation_is_not_swallowed_by_cleanup():
    async def tail():
        raise asyncio.CancelledError
        yield b'never'

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(transport.drain_completed_stream(tail()))


def test_external_task_cancel_during_blocked_cleanup_releases_reader(monkeypatch):
    monkeypatch.setattr(transport, 'POST_TERMINAL_DRAIN_TIMEOUT_S', 5)
    closed = []

    async def run():
        reading = asyncio.Event()

        async def tail():
            try:
                reading.set()
                await asyncio.Event().wait()
                yield b'never'
            finally:
                closed.append(True)

        task = asyncio.create_task(transport.drain_completed_stream(tail()))
        await asyncio.wait_for(reading.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert task.cancelled()

    asyncio.run(run())
    assert closed == [True]


def test_task_cancellation_wins_race_with_idle_watchdog(monkeypatch):
    # The watchdog latches idle before closing the response. The caller may
    # cancel at that exact boundary: it remains cancellation, not EOF/retry.
    monkeypatch.setattr(transport, 'stream_idle_timeout_seconds', lambda: 0.001)
    monkeypatch.setattr(transport.StreamProgress, 'transport_idle_seconds',
                        lambda self, now=None: 1)
    closed = []

    async def silent(self):
        self.reader_task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
            yield b'never'
        finally:
            closed.append(True)

    async def close_and_cancel(self):
        self.reader_task.cancel()

    monkeypatch.setattr(_FakeAsyncStreamCtx, 'aiter_bytes', silent)
    monkeypatch.setattr(_FakeAsyncStreamCtx, 'aclose', close_and_cancel, raising=False)
    with pytest.raises(asyncio.CancelledError):
        _run_async([])
    assert closed == [True]


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_late_transport_error_does_not_retry_a_completed_response(monkeypatch, protocol):
    prefix, terminal = _response(protocol)
    reads = []

    async def bytes_then_error(self):
        yield prefix + terminal + b'data: '
        reads.append('cleanup')
        raise httpx.RemoteProtocolError('disconnected after terminal')

    monkeypatch.setattr(_FakeAsyncStreamCtx, 'aiter_bytes', bytes_then_error)
    result = _run_async([], protocol=protocol)
    assert reads == ['cleanup']
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == 'complete 世界'
    assert '_failure_stage' not in result.usage


def test_preterminal_transport_error_remains_retryable(monkeypatch):
    prefix, _terminal = _response('openai')

    async def bytes_then_error(self):
        yield prefix
        raise httpx.RemoteProtocolError('disconnected before terminal')

    monkeypatch.setattr(_FakeAsyncStreamCtx, 'aiter_bytes', bytes_then_error)
    # finish_reason alone cannot suppress transport errors: final usage might
    # still be missing. The existing retry exception contract remains intact.
    from lib.llm.llm_errors import RetryableAPIError
    with pytest.raises(RetryableAPIError):
        _run_async([])


@pytest.mark.parametrize('outcome', ['silent', 'cancelled'])
def test_async_terminal_cleanup_settles_or_cancels_and_releases_reader(monkeypatch, outcome):
    prefix, terminal = _response('openai')
    closed = []
    monkeypatch.setattr(transport, 'POST_TERMINAL_DRAIN_TIMEOUT_S', 0.02)

    async def tail(self):
        try:
            yield prefix + terminal + b'data: '
            if outcome == 'cancelled':
                raise asyncio.CancelledError
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(_FakeAsyncStreamCtx, 'aiter_bytes', tail)
    if outcome == 'cancelled':
        with pytest.raises(asyncio.CancelledError):
            _run_async([])
    else:
        result = _run_async([])
        assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert closed == [True]
