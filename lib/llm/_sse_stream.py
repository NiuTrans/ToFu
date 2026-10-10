"""Shared byte-stream lifecycle between SSE framing and provider accumulation.

Transports supply chunks and explicit interruption/EOF. Provider consumers alone
recognize terminal events or fail-fast rejection; this owner stops framing at
that exact event, retaining all earlier issues. It never classifies success.
WebSocket providers already have payload boundaries and do not use this layer.
"""
from __future__ import annotations

from typing import Protocol

from lib.llm._sse_framer import SSEEvent, SSEFramer


class _SSEConsumer(Protocol):
    def feed_event(self, event: SSEEvent) -> bool: ...

    def record_malformed_frames(
            self, count: int, diagnostics: tuple[str, ...]) -> None: ...


class SSEByteStream:
    """Own an attempt's parser lifecycle, not its terminal classification.

    ``stopped`` means the consumer requested no more input (DONE or rejection),
    never that the attempt succeeded. Only the consumer's final result decides
    whether finish, tools, usage and all prior malformed evidence are valid.
    """

    def __init__(self, consumer: _SSEConsumer):
        self._consumer = consumer
        self._framer = SSEFramer()
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    def _record_issues(self) -> None:
        issues = self._framer.drain_issues()
        if issues.count:
            self._consumer.record_malformed_frames(issues.count, issues.diagnostics)

    def feed(self, chunk: bytes) -> bool:
        """Consume through the first protocol stop, without parsing its trailer."""
        if self.stopped:
            return True
        events = self._framer.iter_events(chunk)
        try:
            for event in events:
                # Preserve the wire order: faults before this event are already
                # evidence before it can emit a tool callback or terminal.
                self._record_issues()
                if self._consumer.feed_event(event):
                    self._stopped = True
                    break
        except BaseException:
            # Provider rejection/caller cancellation also retires this parser;
            # a retained traceback must not keep a partially assembled event.
            self._stopped = True
            raise
        finally:
            # Release the suspended decoder and its chunk/slice references.
            # No lookahead beyond a yielded terminal is fed or diagnosed.
            events.close()
            self._record_issues()
            if self.stopped:
                self._framer.close()
        return self.stopped

    def finalize(self, *, interrupted: bool = False) -> None:
        """Validate EOF unless the consumer/client intentionally stopped input.

        Idempotent; input after finalization is forbidden. ``stopped`` still
        means consumer stop, not EOF/abort or success. Finish-reason alone is
        NOT a framing stop: pending data still fails at EOF. An abort discards
        pending bytes but keeps all prior malformed facts;
        it cannot become provider success because the consumer owns abort state.
        """
        if self.stopped or interrupted:
            self._framer.close()
        else:
            for event in self._framer.finalize():
                self._consumer.feed_event(event)
        self._record_issues()
