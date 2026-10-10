"""Streaming waits preserve serving-loop progress and connection accounting."""
import asyncio
import threading

import pytest

from lib import observability

pytestmark = pytest.mark.unit


@pytest.fixture
def connections(monkeypatch):
    calls = []
    monkeypatch.setattr(observability, 'connection_open',
                        lambda *args: calls.append(('open', *args)))
    monkeypatch.setattr(observability, 'connection_close',
                        lambda *args: calls.append(('close', *args)))
    return calls


def test_waiting_sync_source_does_not_block_the_serving_loop(connections):
    entered = threading.Event()
    release = threading.Event()

    def source():
        entered.set()
        release.wait()
        yield 'data: preserved\n\n'

    # Independent watchdog also releases a regressed blocking implementation;
    # the test must fail promptly rather than leave an executor thread behind.
    watchdog = threading.Timer(2, release.set)
    watchdog.start()

    async def exercise():
        stream = observability.instrument_sse(source(), 'test')
        next_frame = asyncio.create_task(anext(stream))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert not release.is_set(), 'sync iteration blocked the serving loop'
            assert not next_frame.done()
            release.set()
            assert await asyncio.wait_for(next_frame, 1) == 'data: preserved\n\n'
            await stream.aclose()
        finally:
            release.set()
            await asyncio.gather(next_frame, return_exceptions=True)
            await stream.aclose()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        watchdog.cancel()
        watchdog.join()
    assert connections == [('open', 'sse', 'test'),
                           ('close', 'sse', 'test', 'disconnected')]


@pytest.mark.parametrize('asynchronous', [False, True])
def test_sse_preserves_frames_and_accounts_for_source_error(connections, asynchronous):
    def source():
        yield 'id: 7\n'
        raise ValueError('source failure')

    async def async_source():
        yield 'id: 7\n'
        raise ValueError('source failure')

    async def exercise():
        stream = observability.instrument_sse(
            async_source() if asynchronous else source(), 'test')
        assert await anext(stream) == 'id: 7\n'
        with pytest.raises(ValueError, match='source failure'):
            await anext(stream)

    asyncio.run(exercise())
    assert connections == [('open', 'sse', 'test'),
                           ('close', 'sse', 'test', 'error')]


def test_async_idle_source_cancels_and_closes_without_a_worker(connections):
    closed = []

    async def source():
        try:
            await asyncio.Event().wait()
            yield 'unreachable'
        finally:
            closed.append(True)

    async def exercise():
        stream = observability.instrument_sse(source(), 'test')
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)

    asyncio.run(exercise())
    assert closed == [True]
    assert connections == [('open', 'sse', 'test'),
                           ('close', 'sse', 'test', 'disconnected')]
