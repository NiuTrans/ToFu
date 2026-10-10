"""Reusable tool-progress sink for streaming tool output.

``ToolProgressSink`` is the presentation side-channel for a long-running tool.
It wraps the chat task's existing ``append_event`` chokepoint, coalesces
high-frequency ``on_chunk`` callbacks into bounded ``tool_progress`` frames, and
maintains the bounded reconnect snapshot on the active round entry
(``_partialOutput`` / ``_partialOutputTotalChars`` / ``_partialOutputTruncated``)
so a mid-stream reconnect replays live output without retaining the whole
transcript.

The model-visible result is deliberately untouched: progress is presentation
only.  One final authoritative tool result still settles the round; this sink
never creates a second model-visible result and never stores an unbounded
transcript.

Frame contract (additive to the registered ``tool_progress`` event in
``lib/agent_core/events.py``):

* ``contractVersion`` / ``version`` — this sink's payload contract.
* ``taskId`` / ``roundNum`` / ``toolCallId`` / ``toolName`` — correlation.
* ``seq`` — monotonically increasing per-call sequence (1-based).
* ``stream`` / ``chunk`` — one ordered stream delta (consecutive same-stream
  chunks are merged without reordering stdout/stderr interleaving).
* ``bytes`` / ``chars`` — UTF-8 byte and character lengths of this frame's
  chunk.
* ``spooling`` — True when this frame coalesced more than one observed chunk.
* ``truncated`` — True when the bounded reconnect snapshot is a prefix/tail
  projection rather than the exact output.
* ``terminalReason`` — optional reason stamped on the final frame by
  :meth:`close`.
"""

from __future__ import annotations

import codecs
import threading
import time
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.project_mod.config import MAX_COMMAND_OUTPUT
from lib.tasks_pkg.tool_runtime.context import ToolExecutionContext

logger = get_logger(__name__)


DEFAULT_COALESCE_MS = 200
DEFAULT_COALESCE_BYTES = 4096
TOOL_PROGRESS_CONTRACT_VERSION = 'tofu.tool-progress/v1'
TOOL_PROGRESS_VERSION = 1

# Structural fields owned by this sink. Caller-supplied ``**fields`` may add
# extra presentation metadata (e.g. ``detail``) but must never clobber these.
_RESERVED_FIELDS = frozenset({
    'type', 'contractVersion', 'version', 'taskId', 'roundNum',
    'toolCallId', 'toolName', 'seq', 'stream', 'chunk', 'bytes', 'chars',
    'spooling', 'truncated', 'terminalReason', 'emittedAt',
})


class ToolProgressSink:
    """Coalescing, bounded ``tool_progress`` emitter for one tool call.

    Parameters mirror the per-call correlation already available on
    :class:`~lib.tasks_pkg.tool_runtime.context.ToolExecutionContext`:

    * ``task`` — the live chat task dict (events appended).
    * ``round_num`` / ``tool_call_id`` / ``tool_name`` — correlation.
    * ``round_entry`` — the active round entry; its reconnect snapshot fields
      are updated in place (optional, for state-snapshot replay).
    * ``append_event_fn`` — defaults to ``lib.tasks_pkg.manager.append_event``.
      Injectable for tests. Failures are non-fatal and counted.
    * ``coalesce_ms`` / ``coalesce_bytes`` — flush a trailing buffer after this
      wall-clock window or as soon as buffered bytes exceed this bound
      (whichever first). Defaults match the existing run-command coalescer.
    * ``snapshot_limit`` — reconnect snapshot character budget; defaults to
      ``MAX_COMMAND_OUTPUT``.
    """

    def __init__(
        self,
        task: dict[str, Any],
        *,
        round_num: int,
        tool_call_id: str,
        tool_name: str,
        round_entry: dict[str, Any] | None = None,
        append_event_fn: Any = None,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        coalesce_bytes: int = DEFAULT_COALESCE_BYTES,
        snapshot_limit: int | None = None,
    ) -> None:
        self._task = task
        self._task_id = str((task or {}).get('id') or '')
        self._round_num = int(round_num)
        self._tool_call_id = str(tool_call_id or '')
        self._tool_name = str(tool_name or '')
        self._round_entry = round_entry
        self._append_event = (
            append_event_fn if append_event_fn is not None
            else self._resolve_append_event()
        )
        self._coalesce_ms = max(0.001, float(coalesce_ms))
        self._coalesce_bytes = max(1, int(coalesce_bytes))

        snapshot_limit = (
            MAX_COMMAND_OUTPUT if snapshot_limit is None else snapshot_limit)
        self._snapshot_limit = max(1, int(snapshot_limit))
        self._prefix_limit = self._snapshot_limit * 3 // 4
        self._suffix_limit = max(1, self._snapshot_limit - self._prefix_limit)

        # Bounded reconnect snapshot (exact below the cap, prefix/tail above).
        self._prefix = ''
        self._suffix = ''
        self._total_chars = 0
        self._snapshot_truncated = False

        # Coalescing buffer: (stream, text, caller_fields) in observed order.
        self._buffer: list[tuple[str, str, dict[str, Any]]] = []
        self._buffered_bytes = 0
        self._last_flush = time.monotonic()
        self._flush_deadline: float | None = None
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._closed = False

        # A single incremental decoder handles multi-byte UTF-8 sequences split
        # across successive byte chunks; it is only created when bytes arrive.
        self._decoder: codecs.IncrementalDecoder | None = None
        self._last_stream = 'stdout'

        # Monotonic per-call sequence and accounting.
        self._seq = 0
        self._raw_chunks = 0
        self._emitted = 0
        self._coalesced = 0
        self._dropped = 0
        self._failed_emits = 0
        self._observed_bytes = 0
        self._observed_chars = 0

    # ── Public API ─────────────────────────────────────────
    def publish(self, stream: str, text: Any = None, **fields: Any) -> None:
        """Accept one observed output chunk (``str`` or ``bytes``).

        Empty chunks and chunks published after :meth:`close` are dropped.
        Caller ``fields`` ride along on the emitted frame as extra metadata.
        """
        with self._condition:
            if self._closed:
                self._dropped += 1
                return
            if text is None:
                self._dropped += 1
                return
            was_bytes = isinstance(text, bytes)
            if was_bytes and not text:
                self._dropped += 1
                return
            text = self._decode_text(text)
            if not text:
                # A non-empty byte chunk may decode to '' while the
                # incremental decoder holds a partial multi-byte sequence;
                # that is pending input, not a dropped empty chunk.
                if not was_bytes:
                    self._dropped += 1
                return
            stream = str(stream or '')
            self._last_stream = stream
            text_bytes = len(text.encode('utf-8'))
            self._raw_chunks += 1
            self._observed_chars += len(text)
            self._observed_bytes += text_bytes
            self._buffer.append((stream, text, dict(fields)))
            self._buffered_bytes += text_bytes

            now = time.monotonic()
            if (self._buffered_bytes >= self._coalesce_bytes
                    or (now - self._last_flush) * 1000.0 >= self._coalesce_ms):
                self._flush_locked()
                self._condition.notify_all()
            elif self._flush_deadline is None:
                self._flush_deadline = (
                    self._last_flush + self._coalesce_ms / 1000.0)
                self._ensure_worker_locked()
                self._condition.notify_all()

    def flush(self) -> int:
        """Synchronously drain pending buffered output; return frames emitted."""
        with self._condition:
            if self._closed:
                return 0
            self._flush_deadline = None
            emitted = self._flush_locked()
            self._condition.notify_all()
            return emitted

    def close(self, terminal_reason: str | None = None) -> int:
        """Drain once, stop the worker, and refuse later publishes.

        When ``terminal_reason`` is given it is stamped on the final emitted
        frame (or an empty terminal frame when nothing was pending), so a
        consumer can observe why the progress side-channel ended.
        """
        with self._condition:
            if self._closed:
                return 0
            emitted = 0
            tail = self._drain_decoder()
            if tail:
                self._raw_chunks += 1
                self._observed_chars += len(tail)
                self._observed_bytes += len(tail.encode('utf-8'))
                self._buffer.append((self._last_stream, tail, {}))
                self._buffered_bytes += len(tail.encode('utf-8'))
            if self._buffer:
                emitted += self._flush_locked(terminal_reason=terminal_reason)
            elif terminal_reason is not None:
                self._snapshot_truncated = self._total_chars > self._snapshot_limit
                if self._emit_group(
                        self._last_stream, '', 0, {},
                        terminal_reason=terminal_reason):
                    emitted += 1
            self._closed = True
            worker = self._worker
            self._flush_deadline = None
            self._condition.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(1.0, self._coalesce_ms / 1000.0 * 2))
        return emitted

    @property
    def emitted(self) -> int:
        with self._condition:
            return self._emitted

    @property
    def coalesced(self) -> int:
        with self._condition:
            return self._coalesced

    @property
    def dropped(self) -> int:
        with self._condition:
            return self._dropped

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def snapshot(self) -> str:
        """Current bounded reconnect preview (exact below the cap)."""
        with self._condition:
            return self._render_bounded_locked()

    @property
    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                'emitted': self._emitted,
                'coalesced': self._coalesced,
                'dropped': self._dropped,
                'failed_emits': self._failed_emits,
                'raw_chunks': self._raw_chunks,
                'observed_bytes': self._observed_bytes,
                'observed_chars': self._observed_chars,
                'seq': self._seq,
                'pending_chunks': len(self._buffer),
                'pending_bytes': self._buffered_bytes,
                'truncated': self._snapshot_truncated,
                'closed': self._closed,
            }

    # ── Coalescing / emission internals ────────────────────
    @staticmethod
    def _resolve_append_event():
        from lib.tasks_pkg.manager import append_event
        return append_event

    def _decode_text(self, text: Any) -> str:
        if isinstance(text, str):
            return text
        if isinstance(text, bytes):
            if self._decoder is None:
                self._decoder = codecs.getincrementaldecoder('utf-8')(
                    errors='replace')
            return self._decoder.decode(text, final=False)
        if text is None:
            return ''
        return str(text)

    def _drain_decoder(self) -> str:
        if self._decoder is None:
            return ''
        try:
            return self._decoder.decode(b'', final=True)
        except Exception as exc:
            logger.debug('[tool-progress] decoder drain failed: %s', exc)
            return ''

    def _merge(self) -> list[tuple[str, str, int, dict[str, Any]]]:
        """Merge consecutive same-stream chunks without reordering streams."""
        merged: list[tuple[str, str, int, dict[str, Any]]] = []
        cur_stream: str | None = None
        cur_parts: list[str] = []
        cur_count = 0
        cur_fields: dict[str, Any] = {}
        for stream, text, fields in self._buffer:
            if stream == cur_stream:
                cur_parts.append(text)
                cur_count += 1
                cur_fields.update(fields)
            else:
                if cur_stream is not None:
                    merged.append(
                        (cur_stream, ''.join(cur_parts), cur_count, cur_fields))
                cur_stream = stream
                cur_parts = [text]
                cur_count = 1
                cur_fields = dict(fields)
        if cur_stream is not None:
            merged.append((cur_stream, ''.join(cur_parts), cur_count, cur_fields))
        return merged

    def _flush_locked(self, terminal_reason: str | None = None) -> int:
        if not self._buffer:
            return 0
        merged = self._merge()
        self._buffer.clear()
        self._buffered_bytes = 0
        self._last_flush = time.monotonic()
        self._flush_deadline = None

        for _stream, text, _count, _fields in merged:
            self._remember_bounded_locked(text)
        self._snapshot_truncated = self._total_chars > self._snapshot_limit
        self._write_snapshot_locked(self._render_bounded_locked())

        emitted = 0
        last_index = len(merged) - 1
        for index, (stream, text, raw_count, fields) in enumerate(merged):
            reason = (
                terminal_reason
                if (terminal_reason is not None and index == last_index)
                else None)
            self._coalesced += max(0, raw_count - 1)
            if self._emit_group(stream, text, raw_count, fields,
                                terminal_reason=reason):
                emitted += 1
        return emitted

    def _emit_group(
        self,
        stream: str,
        text: str,
        raw_count: int,
        fields: dict[str, Any],
        terminal_reason: str | None = None,
    ) -> bool:
        event = build_event(EventType.TOOL_PROGRESS)
        for key, value in fields.items():
            if key not in _RESERVED_FIELDS:
                event[key] = value
        self._seq += 1
        event.update({
            'contractVersion': TOOL_PROGRESS_CONTRACT_VERSION,
            'version': TOOL_PROGRESS_VERSION,
            'taskId': self._task_id,
            'roundNum': self._round_num,
            'toolCallId': self._tool_call_id,
            'toolName': self._tool_name,
            'seq': self._seq,
            'stream': stream,
            'chunk': text,
            'bytes': len(text.encode('utf-8')),
            'chars': len(text),
            'spooling': raw_count > 1,
            'truncated': self._snapshot_truncated,
        })
        if terminal_reason is not None:
            event['terminalReason'] = str(terminal_reason)
        try:
            self._append_event(self._task, event)
        except Exception as exc:
            self._failed_emits += 1
            logger.warning(
                '[tool-progress] append_event failed (non-fatal) task=%s '
                'round=%s tool=%s: %s',
                self._task_id[:8], self._round_num, self._tool_name, exc)
            return False
        self._emitted += 1
        return True

    # ── Bounded reconnect snapshot ─────────────────────────
    def _remember_bounded_locked(self, text: str) -> None:
        if not text:
            return
        self._total_chars += len(text)
        prefix_room = self._prefix_limit - len(self._prefix)
        if prefix_room > 0:
            self._prefix += text[:prefix_room]
            text = text[prefix_room:]
        if text:
            self._suffix = (self._suffix + text)[-self._suffix_limit:]

    def _render_bounded_locked(self) -> str:
        total = self._total_chars
        prefix = self._prefix
        suffix = self._suffix
        limit = self._snapshot_limit
        if total <= limit:
            return prefix + suffix
        marker = f'\n\n… [live output truncated: {total:,} chars total] …\n\n'
        if len(marker) >= limit:
            return marker[:limit]
        available = limit - len(marker)
        prefix_size = min(len(prefix), available * 3 // 4)
        suffix_size = available - prefix_size
        tail = suffix[-suffix_size:] if suffix_size else ''
        return prefix[:prefix_size] + marker + tail

    def _write_snapshot_locked(self, rendered: str) -> None:
        entry = self._round_entry
        if not isinstance(entry, dict):
            return
        entry['_partialOutput'] = rendered
        entry['_partialOutputTotalChars'] = self._total_chars
        if self._snapshot_truncated:
            entry['_partialOutputTruncated'] = True
        else:
            entry.pop('_partialOutputTruncated', None)

    # ── Single bounded worker (no per-window Timer pileup) ──
    def _ensure_worker_locked(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name='tofu-tool-progress-sink',
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._closed:
                    if self._flush_deadline is None:
                        self._condition.wait()
                        continue
                    remaining = self._flush_deadline - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue
                    self._flush_deadline = None
                    try:
                        self._flush_locked()
                    except BaseException as exc:
                        logger.exception(
                            'background tool-progress flush failed: %s', exc)
                    break
                if self._closed:
                    return


def progress_sink_for_context(
    context: ToolExecutionContext,
    **kwargs: Any,
) -> ToolProgressSink:
    """Build a sink from one tool-runtime context (does not bind it)."""
    return ToolProgressSink(
        context.task,
        round_num=context.round_num,
        tool_call_id=context.tool_call_id,
        tool_name=context.tool_name,
        round_entry=context.round_entry,
        **kwargs,
    )


def bind_tool_progress_sink(
    context: ToolExecutionContext,
    **kwargs: Any,
) -> ToolProgressSink:
    """Build a sink and bind it onto ``context`` for ``publish_progress``."""
    sink = progress_sink_for_context(context, **kwargs)
    context.bind_progress_sink(sink)
    return sink


__all__ = [
    'DEFAULT_COALESCE_BYTES',
    'DEFAULT_COALESCE_MS',
    'TOOL_PROGRESS_CONTRACT_VERSION',
    'TOOL_PROGRESS_VERSION',
    'ToolProgressSink',
    'bind_tool_progress_sink',
    'progress_sink_for_context',
]
