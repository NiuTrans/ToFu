"""Bounded byte-to-event framing for provider Server-Sent Events.

Responsibility
--------------
Decode arbitrary raw-byte chunking into complete SSE events.  Provider JSON
translation and assistant-message accumulation belong to ``_sse_core.py``;
HTTP/WebSocket/desktop transports only supply bytes or decoded payloads.

The framer implements the wire rules once for every provider: strict
incremental UTF-8, BOM handling, CR/LF/CRLF, comments, multi-line ``data:``,
multiple events per transport chunk, and EOF truncation.  A single event is
bounded to 1 MiB.  Diagnostics contain no provider payload and are capped.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import re


MAX_SSE_EVENT_BYTES = 1 << 20
_DECODE_SLICE_BYTES = 64 << 10
_MAX_PENDING_DIAGNOSTICS = 4
_MAX_DIAGNOSTIC_CHARS = 240
# These bytes cannot start UTF-8 code points. Each is a separate decoder fault
# outside a partial multibyte sequence, so a dense run can be counted at once.
_INVALID_UTF8_LEAD_RUN = re.compile(rb'[\x80-\xc1\xf5-\xff]+')


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """One fully framed SSE event."""

    data: str
    event: str = ''
    event_id: str = ''
    retry_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SSEFramingIssues:
    """Bounded issues accumulated since the previous drain."""

    count: int = 0
    diagnostics: tuple[str, ...] = ()


class SSEFramer:
    """Incrementally frame a raw SSE byte stream.

    ``iter_events`` yields before consuming the next event in a chunk, so a
    protocol consumer can stop at its terminal without parsing trailing bytes.
    Exhaust each iterator before feeding again, or close it and the framer when
    stopping. ``feed`` is the eager compatibility API. ``finalize`` validates
    actual EOF; ``close`` discards an intentional cutoff without inventing EOF.
    Both retain earlier issues for ``drain_issues``; neither grants completion.
    """

    def __init__(self, *, max_event_bytes: int = MAX_SSE_EVENT_BYTES):
        if max_event_bytes < 1:
            raise ValueError('max_event_bytes must be positive')
        self._max_event_bytes = int(max_event_bytes)
        self._pending_utf8 = b''
        self._stream_byte_offset = 0
        self._at_stream_start = True
        self._swallow_lf = False
        self._line_parts: list[str] = []
        self._line_has_content = False
        self._data_lines: list[str] = []
        self._saw_data_field = False
        self._event_name = ''
        self._event_id = ''
        self._retry_ms: int | None = None
        self._event_bytes = 0
        self._discard_event = False
        self._closed = False
        self._issue_count = 0
        self._issue_diagnostics: list[str] = []

    def _record_issue(self, kind: str, detail: str) -> None:
        self._issue_count += 1
        if len(self._issue_diagnostics) >= _MAX_PENDING_DIAGNOSTICS:
            return
        normalized = ' '.join(str(detail or '').split())
        diagnostic = f'{kind}: {normalized}'[:_MAX_DIAGNOSTIC_CHARS]
        self._issue_diagnostics.append(diagnostic)

    def drain_issues(self) -> SSEFramingIssues:
        issues = SSEFramingIssues(
            count=self._issue_count,
            diagnostics=tuple(self._issue_diagnostics),
        )
        self._issue_count = 0
        self._issue_diagnostics.clear()
        return issues

    def _reset_event(self) -> None:
        self._line_parts.clear()
        self._line_has_content = False
        self._data_lines.clear()
        self._saw_data_field = False
        self._event_name = ''
        self._event_id = ''
        self._retry_ms = None
        self._event_bytes = 0
        self._discard_event = False

    def _mark_event_oversized(self) -> None:
        if self._discard_event:
            return
        self._record_issue(
            'event_too_large',
            f'SSE event exceeded the {self._max_event_bytes}-byte limit',
        )
        self._discard_event = True
        self._line_parts.clear()
        self._data_lines.clear()
        self._saw_data_field = False

    def _append_text(self, text: str) -> None:
        if not text:
            return
        self._line_has_content = True
        if self._discard_event:
            return
        self._event_bytes += len(text.encode('utf-8'))
        if self._event_bytes > self._max_event_bytes:
            self._mark_event_oversized()
            return
        # Keep one reference per decoded slice/line segment rather than one
        # Python object per character. The wire cap is 1 MiB; character-wise
        # buffering could otherwise amplify that into tens of MiB per attempt.
        self._line_parts.append(text)

    def _parse_field_line(self, line: str) -> None:
        if not line or line.startswith(':'):
            return
        if ':' in line:
            field, value = line.split(':', 1)
            if value.startswith(' '):
                value = value[1:]
        else:
            field, value = line, ''
        if field == 'data':
            self._saw_data_field = True
            self._data_lines.append(value)
        elif field == 'event':
            self._event_name = value
        elif field == 'id' and '\x00' not in value:
            self._event_id = value
        elif field == 'retry' and value.isascii() and value.isdigit():
            # This is an advisory reconnection hint, never our retry policy.
            # Ignore values outside a bounded integer without parsing an
            # arbitrarily large decimal or accepting Unicode numeric glyphs.
            digits = value.lstrip('0') or '0'
            if len(digits) <= 10 and int(digits) <= 2_147_483_647:
                self._retry_ms = int(digits)

    def _finish_line(self) -> SSEEvent | None:
        line_was_empty = not self._line_has_content
        line = ''.join(self._line_parts) if not self._discard_event else ''
        self._line_parts.clear()
        self._line_has_content = False

        if not line_was_empty:
            if not self._discard_event:
                self._event_bytes += 1
                if self._event_bytes > self._max_event_bytes:
                    self._mark_event_oversized()
                else:
                    self._parse_field_line(line)
            return None

        if self._discard_event:
            self._reset_event()
            return None
        event = None
        if self._saw_data_field:
            event = SSEEvent(
                data='\n'.join(self._data_lines),
                event=self._event_name,
                event_id=self._event_id,
                retry_ms=self._retry_ms,
            )
        self._reset_event()
        return event

    def _process_text(self, text: str) -> Iterator[SSEEvent]:
        if not text:
            return
        if self._at_stream_start:
            self._at_stream_start = False
            if text.startswith('\ufeff'):
                text = text[1:]
        index = 0
        if self._swallow_lf:
            self._swallow_lf = False
            if text.startswith('\n'):
                index = 1
        segment_start = index
        while index < len(text):
            delimiter = text[index]
            if delimiter not in {'\r', '\n'}:
                index += 1
                continue
            self._append_text(text[segment_start:index])
            event = self._finish_line()
            if event is not None:
                yield event
            index += 1
            if delimiter == '\r':
                if index < len(text):
                    if text[index] == '\n':
                        index += 1
                else:
                    self._swallow_lf = True
            segment_start = index
        self._append_text(text[segment_start:])

    def _decode_slice(self, chunk: bytes) -> Iterator[SSEEvent]:
        """Strictly decode one block while preserving valid event prefixes."""
        pending = self._pending_utf8
        self._pending_utf8 = b''
        data = pending + chunk
        base_offset = self._stream_byte_offset - len(pending)
        consumed = 0
        while data:
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError as error:
                if error.start:
                    prefix = data[:error.start].decode('utf-8')
                    yield from self._process_text(prefix)
                if (error.reason == 'unexpected end of data'
                        and error.end == len(data)):
                    self._pending_utf8 = data[error.start:]
                    break
                skip = max(error.end, error.start + 1)
                invalid_run = _INVALID_UTF8_LEAD_RUN.match(data, error.start)
                fault_count = 1
                if invalid_run is not None:
                    skip = invalid_run.end()
                    fault_count = skip - error.start
                retained = min(fault_count, _MAX_PENDING_DIAGNOSTICS
                               - len(self._issue_diagnostics))
                for index in range(retained):
                    self._record_issue(
                        'invalid_utf8',
                        f'invalid UTF-8 near stream byte '
                        f'{base_offset + consumed + error.start + index}',
                    )
                self._issue_count += fault_count - retained
                # Discard only the event containing the invalid sequence. Keep
                # parsing delimiters so the next blank line resynchronizes.
                self._discard_event = True
                self._line_parts.clear()
                self._data_lines.clear()
                self._saw_data_field = False
                consumed += skip
                data = data[skip:]
            else:
                yield from self._process_text(text)
                break

    def iter_events(
            self, chunk: bytes | bytearray | memoryview) -> Iterator[SSEEvent]:
        """Frame lazily, never recording issues beyond the last yielded event.

        Decoding works in bounded slices, but issue recording and parser state
        advance in wire order. In particular, invalid UTF-8 later in a slice
        cannot contaminate an earlier terminal event.
        """
        if self._closed:
            raise RuntimeError('SSEFramer is already finalized')
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError('SSEFramer.feed requires bytes-like input')
        # Keep caller-owned bytes-like input as a view; copying a bytearray or
        # memoryview wholesale would defeat lazy terminal memory bounds.
        data = memoryview(chunk)
        if data.c_contiguous:
            data = data.cast('B')
        slice_items = max(1, _DECODE_SLICE_BYTES // data.itemsize)
        for start in range(0, len(data), slice_items):
            block = bytes(data[start:start + slice_items])
            yield from self._decode_slice(block)
            self._stream_byte_offset += len(block)

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[SSEEvent]:
        """Consume the whole chunk (callers without a protocol stop boundary)."""
        return list(self.iter_events(chunk))

    def close(self) -> None:
        """Release buffered bytes after intentional stop, retaining prior issues.

        Not an EOF or success signal: the consumer owns terminal/error/abort
        evidence. It must close any active event iterator before calling this.
        """
        self._closed = True
        self._pending_utf8 = b''
        self._reset_event()

    def finalize(self) -> list[SSEEvent]:
        """Close the decoder; reject unfinished data, discard control-only EOF."""
        if self._closed:
            return []
        self._closed = True
        events: list[SSEEvent] = []
        if self._pending_utf8:
            self._record_issue(
                'invalid_utf8',
                f'truncated UTF-8 sequence at stream byte '
                f'{self._stream_byte_offset - len(self._pending_utf8)}',
            )
            self._discard_event = True
            self._pending_utf8 = b''
        pending_field = ''.join(self._line_parts).partition(':')[0]
        if (self._discard_event or self._saw_data_field
                or pending_field == 'data'):
            self._record_issue(
                'truncated_event',
                'stream ended before the SSE event delimiter',
            )
        # Comments, unknown fields, and id/event/retry without data cannot
        # carry lost model output. EOF discards them without invalidating a
        # previously verified finish; unfinished provider data still fails.
        self._reset_event()
        return events


__all__ = [
    'MAX_SSE_EVENT_BYTES',
    'SSEEvent',
    'SSEFramer',
    'SSEFramingIssues',
]
