"""Owned translation backlog without count admission or pressure eviction.

One lazy anonymous file carries pending UTF-8 text; the ordered in-memory index
contains only keys and byte ranges. Repeated keys coalesce before consumption.
The oldest pending narration segment is handed out before any reasoning
segment; each kind stays FIFO, and a terminal item follows every segment.
Terminal replacement preserves pending segments; explicit cancellation may
withdraw them. Idle retirement, terminal consumption and close stop producers
atomically. Reconstructible bytes retire with this buffer, never durable state.
"""
from __future__ import annotations

from collections import deque
import queue
import tempfile
import threading
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

NARRATION = 'narration'
REASONING = 'reasoning'
# The reader follows narration while the turn runs; a backlog of (typically
# longer) reasoning blocks must never delay it.
_HANDOUT_ORDER = (NARRATION, REASONING)
_NO_TERMINAL = object()


def segment_kind(key: int | str) -> str:
    """Reasoning keys are ``thinking:`` block ids; ``text:`` ids and legacy
    integer rounds are narration."""
    return REASONING if str(key).startswith('thinking:') else NARRATION


class IncrementalOperationBuffer:
    def __init__(self, capacity: int | None = None) -> None:
        # Retain the private constructor's old argument for existing adapters;
        # it no longer imposes a count quota or changes accepted work.
        self._condition = threading.Condition()
        self._pending: dict[str, deque[int | str]] = {
            kind: deque() for kind in _HANDOUT_ORDER}
        self._terminal_item: Any = _NO_TERMINAL
        self._segments: dict[int | str, tuple[int, int]] = {}
        self._spool = None
        self._live_bytes = 0
        self._stored_bytes = 0
        self._terminal_queued = False
        self._closed = False
        self._idle_retired = False
        self._dropped_segments = 0
        self._peak_depth = 0

    def _append_text(self, text: str) -> tuple[int, int]:
        raw = text.encode('utf-8')
        if self._spool is None:
            self._spool = tempfile.TemporaryFile(mode='w+b', buffering=0)
        offset = self._spool.seek(0, 2)
        try:
            view = memoryview(raw)
            while view:
                written = self._spool.write(view)
                if not written:
                    raise OSError('translation spool write made no progress')
                view = view[written:]
        finally:
            self._stored_bytes = self._spool.seek(0, 2)
        return offset, len(raw)

    def _reclaim(self) -> None:
        if self._spool is None:
            return
        if not self._live_bytes:
            self._spool.truncate(0)
            self._stored_bytes = 0
            return
        if self._stored_bytes <= self._live_bytes * 2:
            return
        replacement = tempfile.TemporaryFile(mode='w+b', buffering=0)
        ranges = {}
        try:
            for key, (offset, length) in self._segments.items():
                start = replacement.tell()
                self._spool.seek(offset)
                remaining = length
                while remaining:
                    chunk = self._spool.read(min(remaining, 64 * 1024))
                    if not chunk:
                        raise OSError('translation spool ended before its owned range')
                    view = memoryview(chunk)
                    while view:
                        written = replacement.write(view)
                        if not written:
                            raise OSError('translation spool reclamation made no progress')
                        view = view[written:]
                    remaining -= len(chunk)
                ranges[key] = (start, length)
        except BaseException:
            replacement.close()
            raise
        previous = self._spool
        self._spool = replacement
        self._segments = ranges
        self._stored_bytes = self._live_bytes
        previous.close()

    def _try_reclaim(self) -> None:
        try:
            self._reclaim()
        except OSError as exc:
            # Already accepted ranges remain on their original file if copying
            # failed. Reclamation is housekeeping, never admission authority.
            logger.warning('[IncTranslate] spool reclamation deferred: %s', type(exc).__name__)

    def put_segment(self, key: int | str, text: str) -> int:
        """Accept/coalesce a complete segment; -1 means its owner has retired."""
        with self._condition:
            if self._closed or self._terminal_queued:
                return -1
            previous = self._segments.get(key)
            offset, length = self._append_text(text)
            self._segments[key] = (offset, length)
            self._live_bytes += length - (previous[1] if previous else 0)
            if previous is None:
                self._pending[segment_kind(key)].append(key)
            self._peak_depth = max(self._peak_depth, self._depth())
            self._try_reclaim()
            self._condition.notify()
            return 0

    def put_terminal(self, item: Any, *, replace: bool = False,
                     discard_segments: bool = False,
                     preserve_segment_keys: frozenset[int | str] = frozenset()) -> int:
        """Replace a pending handoff; only explicit cancellation discards text."""
        with self._condition:
            if self._closed or (self._terminal_queued and not replace):
                return -1
            dropped = 0
            if replace and discard_segments:
                for kind, pending in self._pending.items():
                    retained = deque()
                    for key in pending:
                        if key in preserve_segment_keys:
                            retained.append(key)
                            continue
                        _, length = self._segments.pop(key)
                        self._live_bytes -= length
                        dropped += 1
                    self._pending[kind] = retained
                self._try_reclaim()
            self._dropped_segments += dropped
            self._terminal_item = item
            self._terminal_queued = True
            self._peak_depth = max(self._peak_depth, self._depth())
            self._condition.notify()
            return dropped

    def _depth(self) -> int:
        return (sum(len(pending) for pending in self._pending.values())
                + (self._terminal_item is not _NO_TERMINAL))

    def _close_locked(self) -> None:
        self._closed = True
        for pending in self._pending.values():
            pending.clear()
        self._terminal_item = _NO_TERMINAL
        self._segments.clear()
        self._live_bytes = self._stored_bytes = 0
        if self._spool is not None:
            self._spool.close()
            self._spool = None
        self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._close_locked()

    def get(self, timeout: float) -> Any:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: bool(self._depth()) or self._closed, timeout=max(0.0, float(timeout)))
            if not ready or not self._depth():
                if not self._closed:
                    self._idle_retired = True
                self._close_locked()
                raise queue.Empty
            for kind in _HANDOUT_ORDER:
                pending = self._pending[kind]
                if not pending:
                    continue
                key = pending[0]
                offset, length = self._segments[key]
                self._spool.seek(offset)
                raw = self._spool.read(length)
                if len(raw) != length:
                    raise OSError('translation spool ended before its owned range')
                text = raw.decode('utf-8')
                pending.popleft()
                del self._segments[key]
                self._live_bytes -= length
                self._try_reclaim()
                return ('segment', key, text)
            item = self._terminal_item
            self._close_locked()
            return item

    def retired_for_idle(self) -> bool:
        with self._condition:
            return self._idle_retired

    def snapshot(self) -> dict[str, int | bool]:
        with self._condition:
            return {'depth': self._depth(), 'peakDepth': self._peak_depth,
                    'droppedSegments': self._dropped_segments,
                    'terminalQueued': self._terminal_queued,
                    'pendingTextBytes': self._live_bytes, 'spoolBytes': self._stored_bytes,
                    'spoolOpen': self._spool is not None}
