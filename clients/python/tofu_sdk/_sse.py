"""Bounded UTF-8 line framing shared by synchronous and asynchronous SDK streams.

Consumes byte chunks before transport line helpers can accumulate an unbounded
line. Counts every field/comment in a frame; CRLF and CR normalize to one LF.
No server imports, retries, JSON policy, or ownership of the response lifecycle.
"""
from .api_v4_generated import SDK_SSE_MAX_EVENT_BYTES, SDK_SSE_MAX_LINE_BYTES


class SSELimitError(ValueError):
    """The peer exceeded the published SDK stream budget."""


class SSELineFramer:
    def __init__(self):
        self.pending = bytearray()
        self.event_bytes = 0
        self.skip_lf = False
        self.first_line = True

    def _line(self):
        text = self.pending.decode('utf-8', errors='replace')
        self.pending.clear()
        if self.first_line:
            self.first_line = False
            text = text.removeprefix('\ufeff')
        return text

    def feed(self, chunk: bytes):
        start = 0
        chunk_size = len(chunk)
        view = memoryview(chunk)
        if self.skip_lf and chunk:
            start = int(chunk[0] == 10)
            self.skip_lf = False
        cr = chunk.find(b'\r', start)
        lf = chunk.find(b'\n', start)
        while start < chunk_size:
            if cr < 0:
                end = lf
            elif lf < 0:
                end = cr
            else:
                end = min(cr, lf)
            if end < 0:
                end = chunk_size
            additional = end - start
            if len(self.pending) + additional > SDK_SSE_MAX_LINE_BYTES:
                raise SSELimitError('SSE line exceeded the SDK byte budget')
            self.event_bytes += additional + int(end < chunk_size)
            if self.event_bytes > SDK_SSE_MAX_EVENT_BYTES:
                raise SSELimitError('SSE event exceeded the SDK byte budget')
            self.pending.extend(view[start:end])
            if end == chunk_size:
                return
            empty = not self.pending
            line = self._line()
            if empty:
                self.event_bytes = 0
            start = end + 1
            if chunk[end] == 13:
                if start < chunk_size and chunk[start] == 10:
                    start += 1
                elif start == chunk_size:
                    self.skip_lf = True
            if 0 <= cr < start:
                cr = chunk.find(b'\r', start)
            if 0 <= lf < start:
                lf = chunk.find(b'\n', start)
            yield line

    def finish(self):
        if self.pending:
            yield self._line()
