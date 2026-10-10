"""Opt-in, reversible asyncio slow-callback diagnostics."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Mapping


class SlowCallbackRateLimit(logging.Filter):
    """Bound slow-callback warnings without adding work when guard is off."""

    def __init__(
        self,
        *,
        burst: int = 20,
        window: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._burst = max(1, burst)
        self._window = max(0.1, window)
        self._clock = clock
        self._window_started = 0.0
        self._count = 0
        self._suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:
        now = self._clock()
        if now - self._window_started >= self._window:
            if self._suppressed:
                record.msg = (
                    '%s [+%d more slow-callback warnings suppressed in the '
                    'last %.0fs]'
                    % (record.getMessage(), self._suppressed, self._window)
                )
                record.args = ()
            self._window_started = now
            self._count = 0
            self._suppressed = 0
        if self._count < self._burst:
            self._count += 1
            return True
        self._suppressed += 1
        return False


class LoopDebugGuard:
    """Own debug-mode and the filter installed on the asyncio logger."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        environ: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.loop = loop
        self.environ = os.environ if environ is None else environ
        self.logger = logger or logging.getLogger(__name__)
        self.asyncio_logger = logging.getLogger('asyncio')
        self.rate_filter: SlowCallbackRateLimit | None = None
        self.enabled = False
        self._started = False
        self._stopped = False
        self._previous_debug = loop.get_debug()
        self._previous_duration = loop.slow_callback_duration
        self._previous_log_level = self.asyncio_logger.level

    def start(self) -> 'LoopDebugGuard':
        if self._started:
            return self
        if self._stopped:
            raise RuntimeError('loop debug guard cannot restart after stop')
        self._started = True

        raw = (self.environ.get('TOFU_LOOP_DEBUG_GUARD', '') or '')
        enabled = raw.strip().lower() in ('1', 'true', 'yes', 'on')
        try:
            threshold = float(
                self.environ.get('TOFU_LOOP_SLOW_CALLBACK_SECS', '') or '1.0')
        except (ValueError, TypeError, OverflowError) as exc:
            self.logger.debug(
                '[Server] bad TOFU_LOOP_SLOW_CALLBACK_SECS, using 1.0: %s',
                exc,
            )
            threshold = 1.0

        if not enabled or threshold <= 0:
            self.logger.info(
                '[Server] Loop blocking-guard OFF (default) — cheap LoopWatch '
                '5s net remains active. Set TOFU_LOOP_DEBUG_GUARD=1 to enable '
                'sub-stall detection.')
            return self

        self.loop.slow_callback_duration = threshold
        self.loop.set_debug(True)
        self.asyncio_logger.setLevel(logging.WARNING)
        self.rate_filter = SlowCallbackRateLimit()
        self.asyncio_logger.addFilter(self.rate_filter)
        self.enabled = True
        self.logger.info(
            '[Server] Loop blocking-guard armed '
            '(slow_callback_duration=%.1fs, rate-limited) — a single on-loop '
            'step over this logs "Executing … took N seconds". DIAGNOSTIC '
            'MODE (debug loop).',
            threshold,
        )
        return self

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if not self.enabled:
            return
        if self.rate_filter is not None:
            self.asyncio_logger.removeFilter(self.rate_filter)
            self.rate_filter = None
        self.asyncio_logger.setLevel(self._previous_log_level)
        self.loop.slow_callback_duration = self._previous_duration
        self.loop.set_debug(self._previous_debug)
        self.enabled = False


__all__ = ['LoopDebugGuard', 'SlowCallbackRateLimit']
