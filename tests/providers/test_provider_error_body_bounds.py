"""Provider error diagnostics keep byte, charset and cancellation boundaries."""
import asyncio

import pytest

from lib.llm.provider_error_body import (ERROR_BODY_MAX_BYTES, aread_error_body,
                                         read_error_body)
from lib.llm.llm_errors import AbortedError

pytestmark = pytest.mark.unit


class StreamingResponse:
    encoding = 'ISO-8859-1'
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False
    @property
    def content(self):
        raise AssertionError('must not materialize complete response.content')
    def iter_content(self, chunk_size=None):
        try:
            for chunk in self.chunks:
                self.reads += 1
                yield chunk
        finally:
            self.closed = True
    async def aiter_bytes(self, chunk_size=None):
        for chunk in self.iter_content(chunk_size):
            yield chunk
    async def aread(self):
        raise AssertionError('must not materialize complete response.aread')


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('size', [0, 16, 17, ERROR_BODY_MAX_BYTES * 2])
def test_prefix_bound_and_completion(asynchronous, size):
    response = StreamingResponse([b'x' * size])
    result = (asyncio.run(aread_error_body(response, maximum=16)) if asynchronous
              else read_error_body(response, maximum=16))
    assert result.data == b'x' * min(size, 16)
    assert result.complete is (size <= 16)
    assert response.closed


@pytest.mark.parametrize('asynchronous', [False, True])
def test_stops_consuming_unending_body_after_budget(asynchronous):
    def chunks():
        while True:
            yield b'x' * 8
    response = StreamingResponse(chunks())
    result = (asyncio.run(aread_error_body(response, maximum=16)) if asynchronous
              else read_error_body(response, maximum=16))
    assert result.data == b'x' * 16 and not result.complete
    assert response.reads == 3 and response.closed


@pytest.mark.parametrize('asynchronous', [False, True])
def test_utf8_and_mojibake_decoder_are_preserved(asynchronous):
    response = StreamingResponse(['网关错误，稍后重试'.encode('utf-8')])
    result = asyncio.run(aread_error_body(response)) if asynchronous else read_error_body(response)
    assert result.text(response) == '网关错误，稍后重试'


@pytest.mark.parametrize('asynchronous', [False, True])
def test_abort_stops_before_next_chunk_and_releases_iterator(asynchronous):
    response = StreamingResponse([b'first', b'second'])
    def aborted():
        return response.reads == 1
    with pytest.raises(AbortedError):
        if asynchronous:
            asyncio.run(aread_error_body(response, abort_check=aborted))
        else:
            read_error_body(response, abort_check=aborted)
    assert response.reads == 1 and response.closed


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('oversized', [False, True])
def test_transport_error_status_and_archive_prefix(monkeypatch, asynchronous, oversized):
    """Exercise both real transport shells, not only the capture helper."""
    import importlib
    from tests.sse.test_sse_core_parity import _run_async, _run_sync
    from lib.llm.llm_errors import RateLimitError
    module = importlib.import_module('lib.llm.astream' if asynchronous else 'lib.llm.stream')
    captured = bytearray()
    receipts = []
    class Archive:
        def append_response(self, data):
            captured.extend(data)
        def commit(self, **receipt):
            receipts.append(receipt)
    prepare = module.prepare_request
    def prepare_with_archive(*args, **kwargs):
        plan = prepare(*args, **kwargs)
        plan.raw_archive_capture = Archive()
        return plan
    monkeypatch.setattr(module, 'prepare_request', prepare_with_archive)
    message = '服务暂时不可用，请稍后重试'.encode('utf-8')
    body = message + (b'x' * ERROR_BODY_MAX_BYTES if oversized else b'')
    run = _run_async if asynchronous else _run_sync
    with pytest.raises(RateLimitError) as error:
        run([], status=503, raw_chunks=[body])
    assert error.value.status_code == 503
    assert '服务暂时不可用' in str(error.value)
    assert captured == body[:ERROR_BODY_MAX_BYTES]
    assert receipts[0] == {'response_complete': not oversized, 'status_code': 503}
