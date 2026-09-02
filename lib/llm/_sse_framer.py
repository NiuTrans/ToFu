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

from dataclasses import dataclass


MAX_SSE_EVENT_BYTES = 1 << 20
_DECODE_SLICE_BYTES = 64 << 10
_MAX_PENDING_DIAGNOSTICS = 4
_MAX_DIAGNOSTIC_CHARS = 240


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

    ``feed`` and ``finalize`` return only complete events.  Malformed wire
    evidence is retrieved with ``drain_issues`` so callers can fail the final
    attempt closed while still preserving already-complete response prefixes.
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
        self._saw_event_field = False
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
        self._saw_event_field = False
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
            self._saw_event_field = True
            self._data_lines.append(value)
        elif field == 'event':
            self._saw_event_field = True
            self._event_name = value
        elif field == 'id' and '\x00' not in value:
            self._saw_event_field = True
            self._event_id = value
        elif field == 'retry' and value.isdigit():
            self._saw_event_field = True
            self._retry_ms = int(value)

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

    def _process_text(self, text: str) -> list[SSEEvent]:
        if not text:
            return []
        if self._at_stream_start:
            self._at_stream_start = False
            if text.startswith('\ufeff'):
                text = text[1:]
        events: list[SSEEvent] = []
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
                events.append(event)
            index += 1
            if delimiter == '\r':
                if index < len(text):
                    if text[index] == '\n':
                        index += 1
                else:
                    self._swallow_lf = True
            segment_start = index
        self._append_text(text[segment_start:])
        return events

    def _decode_slice(self, chunk: bytes) -> list[SSEEvent]:
        """Strictly decode one block while preserving valid event prefixes."""
        pending = self._pending_utf8
        self._pending_utf8 = b''
        data = pending + chunk
        base_offset = self._stream_byte_offset - len(pending)
        consumed = 0
        events: list[SSEEvent] = []
        while data:
            try:
                events.extend(self._process_text(data.decode('utf-8')))
                break
            except UnicodeDecodeError as error:
                if error.start:
                    prefix = data[:error.start].decode('utf-8')
                    events.extend(self._process_text(prefix))
                if (error.reason == 'unexpected end of data'
                        and error.end == len(data)):
                    self._pending_utf8 = data[error.start:]
                    break
                self._record_issue(
                    'invalid_utf8',
                    f'invalid UTF-8 near stream byte '
                    f'{base_offset + consumed + max(0, error.start)}',
                )
                # Discard only the event containing the invalid sequence. Keep
                # parsing delimiters so the next blank line resynchronizes.
                self._discard_event = True
                self._line_parts.clear()
                self._data_lines.clear()
                self._saw_data_field = False
                skip = max(error.end, error.start + 1)
                consumed += skip
                data = data[skip:]
        return events

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[SSEEvent]:
        """Consume one arbitrary raw-byte chunk."""
        if self._closed:
            raise RuntimeError('SSEFramer is already finalized')
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError('SSEFramer.feed requires bytes-like input')
        data = bytes(chunk)
        events: list[SSEEvent] = []
        for start in range(0, len(data), _DECODE_SLICE_BYTES):
            block = data[start:start + _DECODE_SLICE_BYTES]
            events.extend(self._decode_slice(block))
            self._stream_byte_offset += len(block)
        return events

    def finalize(self) -> list[SSEEvent]:
        """Close the decoder and reject any unterminated event at EOF."""
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
        if (self._discard_event or self._line_has_content
                or self._saw_data_field or self._saw_event_field):
            self._record_issue(
                'truncated_event',
                'stream ended before the SSE event delimiter',
            )
            self._reset_event()
        return events


__all__ = [
    'MAX_SSE_EVENT_BYTES',
    'SSEEvent',
    'SSEFramer',
    'SSEFramingIssues',
]
