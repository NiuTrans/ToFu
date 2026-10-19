"""Diagnostic iterator cleanup cannot replace status or caller control flow."""
import asyncio

import pytest

from lib.llm import provider_error_body as bodies
from lib.llm.llm_errors import AbortedError

pytestmark = pytest.mark.unit


class BrokenClose:
    def __init__(self):
        self.read = False
    def __iter__(self):
        return self
    def __next__(self):
        if self.read:
            raise StopIteration
        self.read = True
        return b'diagnostic'
    def close(self):
        raise OSError('sensitive cleanup detail')
    def iter_content(self, **kwargs):
        return self
    def aiter_bytes(self):
        return self
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            return next(self)
        except StopIteration:
            raise StopAsyncIteration from None
    async def aclose(self):
        self.close()


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('abort', [False, True])
def test_cleanup_failure_preserves_result_or_abort(asynchronous, abort, caplog):
    response = BrokenClose()
    check = lambda: abort and response.read
    def run():
        return (asyncio.run(bodies.aread_error_body(response, abort_check=check))
                if asynchronous else bodies.read_error_body(response, abort_check=check))
    if abort:
        with pytest.raises(AbortedError):
            run()
    else:
        assert run().data == b'diagnostic'
    assert 'sensitive cleanup detail' not in caplog.text


def test_stalled_async_iterator_cleanup_has_its_own_budget(monkeypatch):
    monkeypatch.setattr(bodies, 'ERROR_BODY_CLOSE_TIMEOUT_S', .02, raising=False)
    class SlowClose(BrokenClose):
        closed = False
        async def aclose(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.closed = True
    async def run():
        response = SlowClose()
        before = set(asyncio.all_tasks())
        result = await asyncio.wait_for(bodies.aread_error_body(response), .3)
        assert result.data == b'diagnostic'
        assert response.closed
        assert set(asyncio.all_tasks()) == before
    asyncio.run(run())


def test_cancellation_during_reader_retirement_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(bodies, 'ERROR_BODY_TIMEOUT_S', .01)
    async def run():
        retiring = asyncio.Event()
        class Reader(BrokenClose):
            async def __anext__(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    retiring.set()
                    await asyncio.Event().wait()
                    raise
            async def aclose(self):
                pass
        task = asyncio.create_task(bodies.aread_error_body(Reader()))
        await asyncio.wait_for(retiring.wait(), .3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_reader_cancellation_cleanup_cannot_extend_diagnostic_deadline(monkeypatch):
    monkeypatch.setattr(bodies, 'ERROR_BODY_TIMEOUT_S', .01)
    monkeypatch.setattr(bodies, 'ERROR_BODY_CLOSE_TIMEOUT_S', .02)
    async def run():
        class Reader(BrokenClose):
            retired = False
            async def __anext__(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.retired = True
            async def aclose(self):
                pass
        response = Reader()
        before = set(asyncio.all_tasks())
        result = await asyncio.wait_for(bodies.aread_error_body(response), .3)
        assert result.data == b'' and not result.complete
        assert response.retired
        assert set(asyncio.all_tasks()) == before
    asyncio.run(run())


@pytest.mark.parametrize('end', ['eof', 'read_error', 'oversized_chunk'])
def test_stop_racing_with_sync_read_completion_wins(end):
    aborted = False
    def chunks():
        nonlocal aborted
        aborted = True
        if end == 'read_error':
            raise OSError('socket interrupted')
        if end == 'oversized_chunk':
            yield b'x' * (bodies.ERROR_BODY_MAX_BYTES + 1)
    with pytest.raises(AbortedError):
        bodies.read_error_body(None, chunks=chunks(), abort_check=lambda: aborted)
