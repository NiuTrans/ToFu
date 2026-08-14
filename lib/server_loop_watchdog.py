"""Explicit lifecycle owner for production event-loop stall diagnostics."""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
from collections.abc import Mapping
from typing import Any, TextIO


def _float_setting(
    environ: Mapping[str, str],
    key: str,
    default: float,
    logger: logging.Logger,
) -> float:
    try:
        return float(environ.get(key, '') or str(default))
    except (ValueError, TypeError, OverflowError) as exc:
        logger.debug('[Server] bad %s, using %.1f: %s', key, default, exc)
        return default


class LoopWatchdog:
    """Own the on-loop heartbeat, off-loop watcher and C timer.

    ``hooks`` is the module containing the existing pure fault-sink and stall
    decision helpers. Keeping those helpers injected preserves their focused
    tests while moving process-resource ownership out of ``server.py``.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        shutdown_requested: Any,
        *,
        host: str,
        port: int,
        hooks: Any,
        fault_shm_log: TextIO | None,
        fault_log: TextIO | None,
        environ: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
        exit_process: Any = os._exit,
        ready_event: Any | None = None,
    ) -> None:
        self.loop = loop
        self.shutdown_requested = shutdown_requested
        self.host = host
        self.port = port
        self.hooks = hooks
        self.fault_shm_log = fault_shm_log
        self.fault_log = fault_log
        self.environ = os.environ if environ is None else environ
        self.logger = logger or logging.getLogger(__name__)
        self.exit_process = exit_process
        # Optional serving-readiness gate. Quart startup intentionally performs
        # synchronous asset validation/bundling before Hypercorn accepts a
        # request. Measuring that phase as an event-loop outage generated an
        # ERROR and all-thread dump on every healthy boot. The watchdog still
        # starts early (so lifecycle ownership/rollback remains unchanged),
        # but stall diagnostics and the C timer arm only after this gate.
        self.ready_event = ready_event

        self.heartbeat_task: asyncio.Task[None] | None = None
        self.watcher_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self._stopped = False
        self._loop_thread_id: int | None = None

        self.threshold = 0.0
        self.bump_interval = 1.0
        self.ctimer_timeout = 0.0
        self.arm_ctimer = False
        self.active_max_bytes = 0
        self.heartbeat = {'ts': time.monotonic()}
        self.listen_state = {'was_bound': False, 'misses': 0}
        self.listen_probe_host = '127.0.0.1'

    def _should_stop(self) -> bool:
        return self._stop.is_set() or self.shutdown_requested.is_set()

    def start(self) -> 'LoopWatchdog':
        if self._started:
            return self
        if self._stopped:
            raise RuntimeError('loop watchdog cannot restart after stop')

        self.threshold = _float_setting(
            self.environ, 'TOFU_LOOP_STALL_SECS', 5.0, self.logger)
        self.bump_interval = _float_setting(
            self.environ, 'TOFU_LOOP_HEARTBEAT_SECS', 1.0, self.logger)
        if self.bump_interval <= 0:
            self.bump_interval = 1.0

        self._started = True
        if self.threshold <= 0:
            self.logger.info(
                '[Server] Loop-stall watchdog disabled '
                '(TOFU_LOOP_STALL_SECS=0)')
            return self

        self.ctimer_timeout = max(
            self.threshold, self.bump_interval * 2.0)
        self.arm_ctimer = self.hooks._should_arm_ctimer(
            self.threshold, self.fault_shm_log)
        self.active_max_bytes = self.hooks._fault_dump_limits()[
            'active_bytes']
        self.listen_probe_host = (
            self.host
            if self.host not in ('0.0.0.0', '::', '')
            else '127.0.0.1'
        )
        self._loop_thread_id = threading.get_ident()
        try:
            self.heartbeat_task = self.loop.create_task(
                self._heartbeat_loop(), name='tofu-loop-heartbeat')
            self.watcher_thread = threading.Thread(
                target=self._stall_watch,
                name='tofu-loopwatch',
                daemon=True,
            )
            self.watcher_thread.start()
        except BaseException:
            self._stop.set()
            if self.heartbeat_task is not None:
                self.heartbeat_task.cancel()
                self.heartbeat_task = None
            raise

        self.logger.info(
            '[Server] Loop-stall watchdog armed (threshold=%.1fs, '
            'heartbeat=%.1fs, GIL-independent C-timer=%s @ %.1fs)',
            self.threshold,
            self.bump_interval,
            'on' if self.arm_ctimer else 'off',
            self.ctimer_timeout,
        )
        return self

    async def _heartbeat_loop(self) -> None:
        previous_tick = None
        try:
            while not self._should_stop():
                tick_now = time.monotonic()
                self.heartbeat['ts'] = tick_now
                if previous_tick is not None:
                    try:
                        from lib.observability import set_event_loop_lag
                        set_event_loop_lag(max(
                            0.0,
                            tick_now - previous_tick - self.bump_interval,
                        ))
                    except Exception as exc:
                        self.logger.debug(
                            '[Metrics] loop-lag observation skipped: %s', exc)
                previous_tick = tick_now
                self.hooks._write_heartbeat()

                try:
                    bound = await asyncio.to_thread(
                        self.hooks._port_bound,
                        self.port,
                        self.listen_probe_host,
                    )
                except Exception as exc:
                    self.logger.debug(
                        '[LoopWatch] listener probe unavailable; preserving '
                        'process: %s', exc)
                    bound = True
                was_bound, misses, serve_dead = (
                    self.hooks._listener_death_decide(
                        self.listen_state['was_bound'],
                        bound,
                        self.listen_state['misses'],
                        5,
                    )
                )
                self.listen_state.update(
                    was_bound=was_bound, misses=misses)
                if serve_dead:
                    self.logger.critical(
                        '[LoopWatch] serve listener on :%d lost for %d '
                        'consecutive checks while the loop is alive — serve '
                        'task is dead; exiting so the watchdog relaunches',
                        self.port,
                        misses,
                    )
                    try:
                        from lib.log import audit_log
                        audit_log(
                            'serve_listener_death',
                            port=self.port,
                            misses=misses,
                            pid=os.getpid(),
                        )
                    except Exception as exc:
                        self.logger.debug(
                            '[LoopWatch] listener-death audit failed: %s', exc)
                    self.exit_process(1)

                ready = (self.ready_event is None
                         or self.ready_event.is_set())
                if self.arm_ctimer and ready:
                    try:
                        faulthandler.cancel_dump_traceback_later()
                        self.hooks._trim_fault_sink_if_oversize(
                            self.fault_shm_log,
                            self.active_max_bytes,
                            header=(
                                '=== faulthandler retained pid=%d at %s ===\n'
                                % (os.getpid(), time.strftime(
                                    '%Y-%m-%d %H:%M:%S'))
                            ),
                        )
                        faulthandler.dump_traceback_later(
                            self.ctimer_timeout,
                            repeat=False,
                            file=self.fault_shm_log,
                            exit=False,
                        )
                    except Exception as exc:
                        self.logger.warning(
                            '[LoopWatch] could not arm C-timer: %s', exc)
                await asyncio.sleep(self.bump_interval)
        finally:
            if self.arm_ctimer:
                try:
                    faulthandler.cancel_dump_traceback_later()
                except Exception as exc:
                    self.logger.debug(
                        '[LoopWatch] final C-timer cancel failed: %s', exc)

    def _stall_watch(self) -> None:
        poll = max(0.5, min(self.bump_interval, self.threshold / 2.0))
        already_dumped = False
        ready_seen = self.ready_event is None
        while not self._should_stop():
            self._stop.wait(poll)
            if self._should_stop():
                break
            if self.ready_event is not None:
                if not self.ready_event.is_set():
                    already_dumped = False
                    ready_seen = False
                    continue
                if not ready_seen:
                    # Do not inherit startup's stale heartbeat at the instant
                    # readiness flips. The next interval is the first one a
                    # serving-loop stall can honestly occupy.
                    self.heartbeat['ts'] = time.monotonic()
                    already_dumped = False
                    ready_seen = True
                    continue
            age = time.monotonic() - self.heartbeat['ts']
            should_dump, already_dumped = self.hooks._loop_stall_decide(
                age, self.threshold, already_dumped)
            if not should_dump:
                continue

            top_frame = ''
            try:
                frames = sys._current_frames()
                top_frame = self.hooks._extract_loop_top_frame(
                    frames.get(self._loop_thread_id))
            except Exception as exc:
                self.logger.debug(
                    '[LoopWatch] top-frame extract failed: %s', exc)
            pressure = self.hooks._stall_pressure_context()
            try:
                from lib.log import audit_log
                audit_log(
                    'event_loop_stall',
                    duration=round(age, 1),
                    threshold=self.threshold,
                    top_frame=top_frame,
                    pressure=pressure,
                    pid=os.getpid(),
                )
            except Exception as exc:
                self.logger.debug('[LoopWatch] audit_log failed: %s', exc)
            self.logger.error(
                '[LoopWatch] event loop STALLED ~%.1fs (threshold=%.1fs) at '
                '%s%s — dumping all-thread stacks to faulthandler sinks',
                age,
                self.threshold,
                top_frame or '?',
                (' [' + pressure + ']') if pressure else '',
            )

            manual_sinks = [self.fault_log]
            if not self.arm_ctimer:
                manual_sinks.append(self.fault_shm_log)
            for sink in manual_sinks:
                if sink is None:
                    continue
                try:
                    self.hooks._reset_fault_sink(
                        sink,
                        header=(
                            '=== latest loop-stall dump pid=%d ===\n'
                            % os.getpid()
                        ),
                    )
                    sink.write(
                        '\n=== LOOP STALL pid=%d age=%.1fs at %s ===\n'
                        % (os.getpid(), age, time.strftime(
                            '%Y-%m-%d %H:%M:%S'))
                    )
                    sink.flush()
                    faulthandler.dump_traceback(file=sink, all_threads=True)
                    sink.flush()
                except Exception as exc:
                    self.logger.warning(
                        '[LoopWatch] dump to sink failed: %s', exc)

    async def stop(self, *, timeout: float = 2.0) -> bool:
        """Wake both diagnostics paths and release their resource owners."""
        if self._stopped:
            thread = self.watcher_thread
            return thread is None or not thread.is_alive()
        self._stopped = True
        self._stop.set()

        task = self.heartbeat_task
        self.heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.logger.warning(
                    '[LoopWatch] heartbeat stop failed: %s', exc)

        if self.arm_ctimer:
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception as exc:
                self.logger.debug(
                    '[LoopWatch] shutdown C-timer cancel failed: %s', exc)

        thread = self.watcher_thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self.watcher_thread = None
        else:
            self.logger.warning(
                '[LoopWatch] watcher thread did not stop within %.1fs', timeout)
        return stopped


__all__ = ['LoopWatchdog']
