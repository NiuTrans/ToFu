"""Bounded diagnostic prefixes for provider HTTP error responses.

Sync and async streaming transports consume the same byte budget and keep the
known HTTP status authoritative. Captures retain only a prefix, never a replay
of an entire response. Async reads own a fixed five-second deadline; sync
transports arm the same deadline in their existing watchdog and pass expiry
back here. Response closure remains transport-owned. Raw archives use complete=False when a prefix is clipped.
"""
import asyncio
from dataclasses import dataclass
import time

ERROR_BODY_MAX_BYTES = 256 * 1024
ERROR_BODY_CHUNK_BYTES = 64 * 1024
# Diagnostic collection follows an already-known HTTP failure. It never inherits
# the disableable rolling timeout used while a model is still generating.
ERROR_BODY_TIMEOUT_S = 5.0
ERROR_BODY_ABORT_POLL_S = 0.1
ERROR_BODY_CLOSE_TIMEOUT_S = 0.1


def _log_close_failure(error):
    from lib.log import get_logger
    get_logger(__name__).debug('Provider error iterator cleanup failed: %s',
                               type(error).__name__)


async def _close_async_iterator(iterator):
    close = getattr(iterator, 'aclose', None)
    if not callable(close):
        return
    try:
        async with asyncio.timeout(ERROR_BODY_CLOSE_TIMEOUT_S):
            await close()
    except Exception as error:
        # Cleanup evidence is type-only; neither provider contents nor a
        # cleanup failure can replace status or caller cancellation.
        _log_close_failure(error)


@dataclass(frozen=True)
class ErrorBody:
    data: bytes
    complete: bool

    def text(self, response=None):
        from lib.llm_errors import decode_error_body
        return decode_error_body(_BufferedResponse(self.data, response))


class _BufferedResponse:
    """Decode only captured bytes; requests.apparent_encoding must not drain I/O."""
    def __init__(self, content, response):
        self.content = content
        encoding = getattr(response, 'encoding', None)
        self.encoding = encoding if isinstance(encoding, str) else None

    @property
    def apparent_encoding(self):
        from requests.compat import chardet
        return chardet.detect(self.content).get('encoding') if chardet else 'utf-8'


class _Prefix:
    def __init__(self, maximum):
        if type(maximum) is not int or maximum < 0 or maximum > ERROR_BODY_MAX_BYTES:
            raise ValueError('Invalid provider error body byte budget')
        self.maximum = maximum
        self.buffer = bytearray()

    def append(self, chunk):
        remaining = self.maximum - len(self.buffer)
        self.buffer.extend(chunk[:remaining])
        return len(chunk) > remaining

    def result(self, complete):
        return ErrorBody(bytes(self.buffer), complete)


def _check_abort(abort_check):
    if abort_check is not None and abort_check():
        from lib.llm_errors import AbortedError
        raise AbortedError('Provider error body read aborted')


def read_error_body(response, *, chunks=None, maximum=ERROR_BODY_MAX_BYTES,
                    abort_check=None, expired_check=None):
    """Consume at most the bounded prefix plus one transport chunk to prove EOF."""
    prefix = _Prefix(maximum)
    if chunks is None:
        iterator = getattr(response, 'iter_content', None)
        if callable(iterator):
            chunks = iterator(chunk_size=ERROR_BODY_CHUNK_BYTES)
        else:
            # Already-buffered response adapters retain their decoder surface.
            # Production streaming responses must expose iter_content.
            chunks = [getattr(response, 'content', b'') or b'']
    iterator = iter(chunks)
    try:
        while True:
            _check_abort(abort_check)
            if expired_check is not None and expired_check():
                return prefix.result(False)
            try:
                chunk = next(iterator)
            except StopIteration:
                _check_abort(abort_check)
                return prefix.result(not (expired_check is not None and expired_check()))
            except Exception:
                # Interrupting a blocked socket may surface EOF or a transport
                # error. A concurrent caller Stop owns either outcome.
                _check_abort(abort_check)
                if expired_check is not None and expired_check():
                    return prefix.result(False)
                raise
            _check_abort(abort_check)
            if chunk and prefix.append(chunk):
                return prefix.result(False)
    finally:
        close = getattr(iterator, 'close', None)
        if callable(close):
            try:
                close()
            except Exception as error:
                _log_close_failure(error)


async def aread_error_body(response, *, maximum=ERROR_BODY_MAX_BYTES,
                          abort_check=None):
    """Bound async error diagnostics by bytes, elapsed time and caller Stop.

    One attempt-owned reader task keeps an in-flight read cancellable while
    polling Stop; no per-chunk tasks or abandoned background reads survive.
    Timeout returns the captured prefix as incomplete, preserving HTTP status.
    External cancellation and explicit Stop remain caller control flow.
    """
    prefix = _Prefix(maximum)
    iterator = response.aiter_bytes().__aiter__()
    deadline = time.monotonic() + ERROR_BODY_TIMEOUT_S

    async def consume():
        reads = 0
        while True:
            _check_abort(abort_check)
            if time.monotonic() >= deadline:
                return prefix.result(False)
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                return prefix.result(True)
            if chunk and prefix.append(chunk):
                return prefix.result(False)
            reads += 1
            if reads % 32 == 0:
                # Buffered/custom iterators may never suspend, including an
                # unending source of empty chunks. Let task cancellation run.
                await asyncio.sleep(0)

    reader = asyncio.create_task(consume())
    try:
        while True:
            _check_abort(abort_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return prefix.result(False)
            done, _ = await asyncio.wait(
                (reader,), timeout=min(ERROR_BODY_ABORT_POLL_S, remaining))
            _check_abort(abort_check)
            if done:
                return reader.result()
    finally:
        reader.cancel()
        # Retrieving a finished reader's exception here must not mask external
        # cancellation / Stop already propagating through this finally block.
        try:
            # An async generator's finally block can itself await I/O. Bound
            # that retirement separately; timeout cancels the joined reader
            # again rather than leaving an orphaned diagnostic task.
            async with asyncio.timeout(ERROR_BODY_CLOSE_TIMEOUT_S):
                await reader
        except asyncio.CancelledError:
            # Our reader.cancel() is expected; cancellation delivered to the
            # owning task during retirement is not. Never consume user Stop.
            if asyncio.current_task().cancelling():
                raise
        except Exception as error:
            # reader.result() above already owns any read failure; when Stop
            # races with that failure, the caller's control flow takes priority.
            _log_close_failure(error)
        finally:
            await _close_async_iterator(iterator)
