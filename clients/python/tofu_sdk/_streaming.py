"""Response-owned SSE readers for Requests and HTTPX.

Entry points: _parse_sse / _parse_async_sse. Both delegate frame semantics to one
SSEEventDecoder and byte admission to SSELineFramer; neither retries transport.
"""
from collections.abc import AsyncIterator, Iterator

from ._protocol import TofuError
from ._sse import SSELineFramer, SSELimitError
from ._sse_events import SSEEventDecoder
from .api_v4_generated import (
    SDK_SSE_READ_CHUNK_BYTES, SDK_SSE_OVERFLOW_STATUS, SDK_SSE_OVERFLOW_CODE,
)


def _overflow_error(error: SSELimitError) -> TofuError:
    return TofuError(SDK_SSE_OVERFLOW_STATUS, {'error': {
        'kind': SDK_SSE_OVERFLOW_CODE, 'message': str(error), 'source': 'sdk'}})


def _parse_sse(response) -> Iterator[dict]:
    framer, decoder = SSELineFramer(), SSEEventDecoder()
    try:
        for chunk in response.iter_content(chunk_size=SDK_SSE_READ_CHUNK_BYTES):
            if not chunk:
                continue
            for line in framer.feed(chunk):
                event = decoder.feed(line)
                if event is not None:
                    yield event
                if decoder.stopped:
                    return
        # A final line without an empty-line separator cannot dispatch a frame.
        for line in framer.finish():
            decoder.feed(line)
    except SSELimitError as error:
        raise _overflow_error(error) from error
    finally:
        response.close()


async def _parse_async_sse(response) -> AsyncIterator[dict]:
    framer, decoder = SSELineFramer(), SSEEventDecoder()
    try:
        async for chunk in response.aiter_bytes():
            for line in framer.feed(chunk):
                event = decoder.feed(line)
                if event is not None:
                    yield event
                if decoder.stopped:
                    return
        for line in framer.finish():
            decoder.feed(line)
    except SSELimitError as error:
        raise _overflow_error(error) from error
    finally:
        await response.aclose()
