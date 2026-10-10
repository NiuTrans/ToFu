"""SDK byte budgets and owned-response cleanup under streaming failures."""
import asyncio
from pathlib import Path
import sys

import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'clients' / 'python'))
from tofu_sdk import Tofu, TofuError, _parse_sse
from tofu_sdk.async_client import _parse_async_sse
from tofu_sdk import _sse

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def small_budget(monkeypatch):
    monkeypatch.setattr(_sse, 'SDK_SSE_MAX_LINE_BYTES', 64)
    monkeypatch.setattr(_sse, 'SDK_SSE_MAX_EVENT_BYTES', 128)


class Response:
    status_code = 200
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
    def iter_content(self, chunk_size):
        assert chunk_size == 512
        yield from self.chunks
    def close(self):
        self.closed = True
    def json(self):
        return {'error': 'refused'}


@pytest.mark.parametrize('chunks', [[b'x'*64, b'x'], [b'data: '+b' '*20+b'\n']*6,
                                   ['界'.encode()*22]])
def test_sync_overflow_is_typed_nonretryable_and_closes(chunks):
    response = Response(chunks)
    with pytest.raises(TofuError) as error:
        list(_parse_sse(response))
    assert error.value.status == 413 and not error.value.retryable
    assert error.value.kind == 'stream_resource_exhausted'
    assert response.closed


def test_sync_chunk_size_is_not_a_frame_budget_and_utf8_survives_splits():
    frame = 'data: {"text":"界"}\r\n\r\n'.encode()
    for chunks in [[frame*100], [bytes([byte]) for byte in frame]]:
        response = Response(chunks)
        events = list(_parse_sse(response))
        assert all(event == {'text': '界'} for event in events)
        assert len(events) == (100 if len(chunks) == 1 else 1)
        assert response.closed


def test_sync_tasks_do_not_retry_overflow_or_leak_on_abandonment(monkeypatch):
    client = Tofu(base_url='http://unused.invalid', max_retries=3)
    response = Response([b'x'*65])
    calls = []
    monkeypatch.setattr(client, '_request', lambda *a, **k: calls.append(1) or response)
    with pytest.raises(TofuError) as error:
        list(client.tasks.stream('task'))
    assert error.value.status == 413 and calls == [1] and response.closed
    response = Response([b'data: {"seq":1}\n\n', b'data: {"seq":2}\n\n'])
    stream = client.tasks.stream('task')
    assert next(stream)['seq'] == 1
    stream.close()
    assert response.closed
    response = Response([])
    response.status_code = 403
    with pytest.raises(TofuError):
        list(client.tasks.stream('task'))
    assert response.closed


class AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, chunks, *, hold=False):
        self.chunks, self.hold, self.closed = chunks, hold, False
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.hold:
            await asyncio.Event().wait()
    async def aclose(self):
        self.closed = True


def test_async_first_event_is_not_buffered_until_read_chunk_size():
    async def run():
        wire = AsyncBytes([b'data: {"seq":1}\r', b'\n\r', b'\n'], hold=True)
        response = httpx.Response(200, stream=wire)
        events = _parse_async_sse(response)
        assert await asyncio.wait_for(anext(events), .5) == {'seq': 1}
        await events.aclose()
        assert wire.closed and response.is_closed
    asyncio.run(run())


@pytest.mark.parametrize('chunks', [[b'x'*65], [b'data: '+b' '*20+b'\n']*6])
def test_async_oversized_line_and_frame_close_without_retry(chunks):
    async def run():
        wire = AsyncBytes(chunks)
        response = httpx.Response(200, stream=wire)
        with pytest.raises(TofuError) as error:
            async for _ in _parse_async_sse(response):
                pass
        assert error.value.status == 413 and not error.value.retryable
        assert wire.closed
    asyncio.run(run())


def test_async_cancellation_closes_pending_read():
    async def run():
        wire = AsyncBytes([], hold=True)
        events = _parse_async_sse(httpx.Response(200, stream=wire))
        pending = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert wire.closed
    asyncio.run(run())


def test_async_malformed_frame_does_not_leak_event_identity():
    async def run():
        wire = AsyncBytes([b'id: 9\nevent: error\ndata: invalid\n\ndata: {"type":"done"}\n\n'])
        result = [event async for event in _parse_async_sse(httpx.Response(200, stream=wire))]
        assert result == [{'type': 'done'}]
        assert wire.closed
    asyncio.run(run())


def test_dense_frames_scan_each_input_byte_a_bounded_number_of_times():
    class MeasuredBytes(bytes):
        scanned = 0
        def find(self, needle, start=0):
            result = super().find(needle, start)
            self.scanned += (result - start + 1 if result >= 0 else len(self) - start)
            return result
    chunk = MeasuredBytes(b':\n\n' * 10000)
    assert len(list(_sse.SSELineFramer().feed(chunk))) == 20000
    assert chunk.scanned <= 3 * len(chunk)
