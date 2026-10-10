"""A provider status must not leave async diagnostic capture waiting forever."""
import asyncio
import time

import httpx
import pytest

from lib.llm import provider_error_body as bodies
from lib.llm_errors import AbortedError

pytestmark = pytest.mark.unit


class StalledStream(httpx.AsyncByteStream):
    def __init__(self, aborted=None):
        self.closed = False
        self.aborted = aborted
    async def __aiter__(self):
        try:
            yield '诊断前缀'.encode()
            if self.aborted is not None:
                self.aborted.set()
            await asyncio.Event().wait()
        finally:
            self.closed = True


@pytest.mark.parametrize('abort', [False, True])
def test_pending_error_read_has_deadline_and_observes_abort(monkeypatch, abort):
    monkeypatch.setattr(bodies, 'ERROR_BODY_TIMEOUT_S', .04, raising=False)
    monkeypatch.setattr(bodies, 'ERROR_BODY_ABORT_POLL_S', .01, raising=False)
    async def run():
        aborted = asyncio.Event()
        stream = StalledStream(aborted if abort else None)
        response = httpx.Response(503, stream=stream)
        before = time.monotonic()
        if abort:
            with pytest.raises(AbortedError):
                await asyncio.wait_for(bodies.aread_error_body(response, abort_check=aborted.is_set), .3)
        else:
            result = await asyncio.wait_for(bodies.aread_error_body(response), .3)
            assert not result.complete
            assert result.text(response) == '诊断前缀'
        assert time.monotonic() - before < .25
        assert stream.closed
        await response.aclose()
    asyncio.run(run())


def test_external_task_cancellation_is_not_changed_to_partial_capture():
    async def run():
        entered = asyncio.Event()
        stream = StalledStream(entered)
        response = httpx.Response(401, stream=stream)
        task = asyncio.create_task(bodies.aread_error_body(response))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        await response.aclose()
    asyncio.run(run())


def test_non_yielding_empty_chunks_cannot_renew_deadline(monkeypatch):
    monkeypatch.setattr(bodies, 'ERROR_BODY_TIMEOUT_S', .02)
    class EmptyStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b''
    async def run():
        response = httpx.Response(503, stream=EmptyStream())
        # HTTPX filters empty chunks inside aiter_bytes; use a raw adapter to
        # exercise the helper's empty-chunk contract without an inner busy loop.
        class Adapter:
            def aiter_bytes(self):
                return EmptyStream().__aiter__()
        before = set(asyncio.all_tasks())
        result = await bodies.aread_error_body(Adapter())
        assert result.data == b'' and not result.complete
        assert set(asyncio.all_tasks()) == before
        await response.aclose()
    asyncio.run(run())


def test_timed_out_diagnostics_still_classify_known_http_status(monkeypatch):
    from tests.sse.test_sse_core_parity import _run_async, _FakeAsyncStreamCtx
    from lib.llm_errors import RateLimitError
    monkeypatch.setattr(bodies, 'ERROR_BODY_TIMEOUT_S', .02)
    async def stalled(self):
        yield '网关不可用'.encode()
        await asyncio.Event().wait()
    monkeypatch.setattr(_FakeAsyncStreamCtx, 'aiter_bytes', stalled)
    with pytest.raises(RateLimitError) as error:
        _run_async([], status=503)
    assert error.value.status_code == 503
    assert '网关不可用' in str(error.value)
