"""Adaptive duplicate coalescing for production WARNING+ log records.

The filter runs on the producer side, before ``QueueHandler`` loses ContextVar
state.  It lets the first few occurrences through, then emits exponentially
spaced checkpoints (8, 16, 32, …) plus a periodic heartbeat.  Each checkpoint
carries an occurrence delta so downstream aggregate/incident handlers retain
the true event count even though repetitive raw text is omitted.

CRITICAL records and records explicitly marked ``tofu_no_coalesce`` are never
suppressed.  The state table and every environment value are bounded.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
from dataclasses import dataclass

from lib.log_redaction import redact_text


@dataclass
class _Window:
    started_at: float
    last_seen_at: float
    last_emitted_at: float
    seen: int = 0
    emitted_seen: int = 0
    latest_record: logging.LogRecord | None = None


def coalescing_enabled() -> bool:
    return os.environ.get('TOFU_LOG_COALESCE', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, '') or default)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float,
               maximum: float) -> float:
    try:
        value = float(os.environ.get(name, '') or default)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


class DuplicateCoalescingFilter(logging.Filter):
    """Suppress duplicate WARNING/ERROR floods without losing event counts."""

    def __init__(self, *, burst: int | None = None, window_seconds: float | None = None,
                 heartbeat_seconds: float | None = None, state_cap: int = 20_000,
                 clock=None):
        super().__init__()
        self._burst = burst
        self._window_seconds = window_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._state_cap = max(128, min(100_000, int(state_cap)))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}
        self._pending_sink = None
        self._flush_lifecycle_lock = threading.Lock()
        self._flush_stop = threading.Event()
        self._flush_wake = threading.Event()
        self._flush_enabled = False
        self._flush_armed_once = False
        self._flush_thread: threading.Thread | None = None

    def _settings(self) -> tuple[int, float, float]:
        burst = (max(1, int(self._burst)) if self._burst is not None else
                 _env_int('TOFU_LOG_COALESCE_BURST', 5, 1, 1000))
        window = (max(1.0, float(self._window_seconds))
                  if self._window_seconds is not None else
                  _env_float('TOFU_LOG_COALESCE_WINDOW_SEC', 300, 10, 86_400))
        heartbeat = (max(0.1, float(self._heartbeat_seconds))
                     if self._heartbeat_seconds is not None else
                     _env_float('TOFU_LOG_COALESCE_HEARTBEAT_SEC', 60, 1, 3600))
        return burst, window, heartbeat

    @staticmethod
    def _full_text(record: logging.LogRecord) -> str:
        try:
            text = record.getMessage()
        except Exception:
            return redact_text(record.msg, max_chars=16_384)
        if record.exc_info:
            try:
                text = '%s\n%s' % (
                    text, logging.Formatter().formatException(record.exc_info))
            except Exception:
                pass
        return redact_text(text, max_chars=16_384)

    @staticmethod
    def _fingerprint(record: logging.LogRecord, text: str) -> tuple[str, str]:
        try:
            from lib.log_aggregates import fingerprint_text
            return fingerprint_text(record.levelname, record.name, text)
        except Exception:
            # Logging overload protection must remain available even when an
            # optional diagnostics module is broken during boot.
            import hashlib
            raw = '%s|%s|%s' % (record.levelname, record.name, text[:512])
            return hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:16], \
                text.split('\n', 1)[0][:200]

    @staticmethod
    def _stamp(record: logging.LogRecord, *, fingerprint: str, template: str,
               delta: int = 1, window_count: int = 1) -> None:
        record.tofu_fingerprint = fingerprint
        record.tofu_template = template
        record.tofu_occurrence_delta = max(1, int(delta))
        record.tofu_window_count = max(1, int(window_count))
        if delta > 1:
            record.tofu_coalesce_note = (
                '[coalesced %d identical occurrences; window_total=%d] '
                % (delta, window_count))
        else:
            record.tofu_coalesce_note = ''

    @staticmethod
    def _snapshot(record: logging.LogRecord, text: str) -> logging.LogRecord:
        """Detach a bounded record from traceback frames and mutable arguments."""
        snapshot = copy.copy(record)
        snapshot.msg = text
        snapshot.args = ()
        snapshot.exc_info = None
        snapshot.exc_text = None
        snapshot.stack_info = None
        snapshot.__dict__.pop('message', None)
        event_fields = getattr(snapshot, 'tofu_event_fields', None)
        if event_fields:
            try:
                from lib.log_redaction import sanitize_value
                snapshot.tofu_event_fields = sanitize_value(
                    event_fields, field_name='event_fields', max_items=30,
                    max_string_chars=600)
            except Exception:
                snapshot.tofu_event_fields = {}
        return snapshot

    def drain_pending(self, *, force: bool = False,
                      now: float | None = None) -> list[logging.LogRecord]:
        """Return exact-count checkpoints for quiet duplicate tails.

        Power-of-two admission alone would strand the final suppressed tail if
        a flood became quiet before its next event.  The production heartbeat
        thread calls this method; tests and orderly shutdown can call it
        directly.  Returned records are already stamped and must bypass this
        filter when enqueued.
        """
        current = self._clock() if now is None else float(now)
        _burst, window_seconds, heartbeat_seconds = self._settings()
        pending: list[logging.LogRecord] = []
        expired: list[str] = []
        with self._lock:
            for fingerprint, state in self._windows.items():
                delta = state.seen - state.emitted_seen
                due = force or current - state.last_emitted_at >= heartbeat_seconds
                if delta > 0 and due and state.latest_record is not None:
                    record = copy.copy(state.latest_record)
                    template = str(getattr(record, 'tofu_template', '') or '')
                    self._stamp(
                        record, fingerprint=fingerprint, template=template,
                        delta=delta, window_count=state.seen)
                    state.emitted_seen = state.seen
                    state.last_emitted_at = current
                    pending.append(record)
                if (state.seen == state.emitted_seen
                        and current - state.last_seen_at >= window_seconds):
                    expired.append(fingerprint)
            for fingerprint in expired:
                self._windows.pop(fingerprint, None)
        return pending

    def _deliver_pending(self, records: list[logging.LogRecord]) -> None:
        sink = self._pending_sink
        if sink is None:
            return
        for record in records:
            try:
                sink(record)
            except Exception:
                # The normal queue handler owns overload accounting. A logging
                # safeguard must never raise into a business or shutdown path.
                continue

    def _next_pending_delay(self) -> float | None:
        """Return the exact next quiet-tail deadline, or ``None`` when idle."""
        current = self._clock()
        _burst, _window, heartbeat = self._settings()
        with self._lock:
            delays = [
                max(0.0, heartbeat - (current - state.last_emitted_at))
                for state in self._windows.values()
                if state.seen > state.emitted_seen
                and state.latest_record is not None
            ]
        return min(delays) if delays else None

    def _detach_if_idle(self, thread: threading.Thread) -> bool:
        """Atomically retire one exact worker unless a producer added work."""
        with self._flush_lifecycle_lock:
            if self._next_pending_delay() is not None:
                return False
            with self._lock:
                if any(state.seen > state.emitted_seen
                       for state in self._windows.values()):
                    return False
                # Keep lightweight counters for the rest of their coalescing
                # window, but release bounded message/traceback snapshots as
                # soon as every occurrence has a durable checkpoint.
                for state in self._windows.values():
                    state.latest_record = None
            if self._flush_thread is thread:
                self._flush_thread = None
            return True

    def _flush_loop(self) -> None:
        thread = threading.current_thread()
        while not self._flush_stop.is_set():
            # Clear before reading state: a producer racing after the clear
            # either appears in the deadline snapshot or leaves the event set.
            self._flush_wake.clear()
            delay = self._next_pending_delay()
            if delay is None:
                if self._detach_if_idle(thread):
                    return
                continue
            self._flush_wake.wait(delay)
            if self._flush_stop.is_set():
                break
            self._deliver_pending(self.drain_pending())
        with self._flush_lifecycle_lock:
            if self._flush_thread is thread:
                self._flush_thread = None

    def _ensure_pending_flush(self) -> bool | None:
        """Ensure delivery: true=available, false=spawn failed, none=unarmed."""
        with self._flush_lifecycle_lock:
            if (not self._flush_enabled or self._pending_sink is None
                    or self._flush_stop.is_set()):
                return None
            thread = self._flush_thread
            if thread is not None and thread.is_alive():
                self._flush_wake.set()
                return True
            thread = threading.Thread(
                target=self._flush_loop,
                name='tofu-log-coalesce-flush', daemon=True)
            self._flush_thread = thread
            try:
                thread.start()
            except Exception:
                if self._flush_thread is thread:
                    self._flush_thread = None
                # The caller publishes the accumulated delta synchronously on
                # the already non-blocking queue when spawning fails.
                return False
            self._flush_wake.set()
            return True

    def start_pending_flush(self, sink) -> bool:
        """Arm quiet-tail delivery; create a worker only for pending deltas."""
        with self._flush_lifecycle_lock:
            newly_enabled = not self._flush_enabled
            self._pending_sink = sink
            self._flush_enabled = True
            self._flush_armed_once = True
            self._flush_stop.clear()
        if self._next_pending_delay() is not None:
            available = self._ensure_pending_flush()
            if available is False:
                # Startup can inherit producer records queued before lifecycle
                # activation. If a thread cannot be created, enqueue their exact
                # deltas now rather than stranding a quiet tail forever.
                self._deliver_pending(self.drain_pending(force=True))
        return newly_enabled

    def stop_pending_flush(self, *, timeout: float = 5.0,
                           final_flush: bool = True) -> bool:
        """Stop the heartbeat and optionally enqueue every remaining delta."""
        with self._flush_lifecycle_lock:
            self._flush_enabled = False
            self._flush_stop.set()
            self._flush_wake.set()
            thread = self._flush_thread
        try:
            wait_seconds = max(0.0, float(timeout))
        except (TypeError, ValueError, OverflowError):
            wait_seconds = 5.0
        if thread is not None and thread is not threading.current_thread():
            thread.join(wait_seconds)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._flush_lifecycle_lock:
                if self._flush_thread is thread:
                    self._flush_thread = None
        if final_flush:
            self._deliver_pending(self.drain_pending(force=True))
        if stopped:
            with self._flush_lifecycle_lock:
                self._pending_sink = None
        return stopped

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            record.tofu_occurrence_delta = 1
            record.tofu_window_count = 1
            record.tofu_coalesce_note = ''
            return True

        text = self._full_text(record)
        fingerprint, template = self._fingerprint(record, text)
        with self._flush_lifecycle_lock:
            delivery_has_stopped = (
                self._flush_armed_once and not self._flush_enabled)
        if (not coalescing_enabled() or record.levelno >= logging.CRITICAL
                or bool(getattr(record, 'tofu_no_coalesce', False))
                or delivery_has_stopped):
            self._stamp(record, fingerprint=fingerprint, template=template)
            return True

        burst, window_seconds, heartbeat_seconds = self._settings()
        now = self._clock()
        carry = 0
        with self._lock:
            state = self._windows.get(fingerprint)
            if state is None:
                if len(self._windows) >= self._state_cap:
                    # Evict only fully-accounted stale entries. If a pathological
                    # unique-fingerprint storm fills the table with pending
                    # deltas, pass new records through instead of losing counts.
                    victims = sorted((
                        item for item in self._windows.items()
                        if item[1].seen == item[1].emitted_seen
                    ), key=lambda item: item[1].last_seen_at)
                    for victim, _ in victims[:max(1, self._state_cap // 4)]:
                        self._windows.pop(victim, None)
                    if len(self._windows) >= self._state_cap:
                        self._stamp(
                            record, fingerprint=fingerprint, template=template)
                        return True
                state = _Window(now, now, now)
                self._windows[fingerprint] = state
            elif now - state.started_at >= window_seconds:
                carry = max(0, state.seen - state.emitted_seen)
                state = _Window(now, now, now)
                self._windows[fingerprint] = state

            state.seen += 1
            state.last_seen_at = now
            checkpoint = (
                state.seen <= burst
                or (state.seen > 0 and state.seen & (state.seen - 1) == 0)
                or now - state.last_emitted_at >= heartbeat_seconds
            )
            if checkpoint:
                delta = carry + state.seen - state.emitted_seen
                state.emitted_seen = state.seen
                state.last_emitted_at = now
                # The admitted record itself carries this checkpoint. Retain a
                # detached payload only when a future quiet-tail flush needs it.
                state.latest_record = None
            else:
                state.latest_record = self._snapshot(record, text)
                state.latest_record.tofu_fingerprint = fingerprint
                state.latest_record.tofu_template = template

        if not checkpoint:
            # The first genuinely suppressed delta owns a short-lived worker.
            # No warnings and fully-accounted windows therefore cost no thread.
            available = self._ensure_pending_flush()
            if available is not False:
                return False
            # Fail open when the OS refuses a thread: publish every occurrence
            # accumulated through this record on the producer's bounded queue.
            with self._lock:
                current_state = self._windows.get(fingerprint)
                if current_state is None:
                    delta = 1
                    window_count = 1
                else:
                    delta = max(
                        0, current_state.seen - current_state.emitted_seen)
                    if delta == 0:
                        return False
                    current_state.emitted_seen = current_state.seen
                    current_state.last_emitted_at = now
                    current_state.latest_record = None
                    window_count = current_state.seen
            self._stamp(
                record, fingerprint=fingerprint, template=template,
                delta=delta, window_count=max(window_count, delta))
            return True

        self._stamp(
            record, fingerprint=fingerprint, template=template,
            # ``delta`` can include the unreported tail of an expired window.
            # Never publish an internally impossible checkpoint where the
            # displayed total is smaller than the occurrence delta.
            delta=delta, window_count=max(state.seen, delta))
        return True


__all__ = ['DuplicateCoalescingFilter', 'coalescing_enabled']
