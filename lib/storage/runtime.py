"""Process-wide readiness and crash fencing for application integration."""

from __future__ import annotations

from collections import deque
import os
import threading
import time
from typing import Callable

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError
from lib.storage.supervisor import StorageSupervisor
from lib.log import get_logger


logger = get_logger('tofu.storage.runtime')

# Restart backoff ceiling. Transient crashes still retry almost immediately
# (the delay doubles from 0.1 s), but a sustained failure — e.g. a starving
# host where every cold sidecar start on a network volume fails again —
# must not hammer the machine with a fresh spawn every few seconds.
_RESTART_MAX_DELAY_SECONDS = 60.0

# Bounded wait for a supervised restart already in flight. A sidecar
# recycle completes in ~10 s (supervisor liveness cadence + spawn +
# handshake); an in-flight caller whose RPC deadline already exceeds that
# window should wait the authority out instead of failing fast with a 503.
# The wait is hard-bounded so a stuck restart can never wedge a request
# thread: past the cap the caller gets the same classified fence error as
# before. Reads and writes share the cap; write fencing semantics are
# unchanged while the fence is actually held (during the wait no client
# exists, so no write can pass).
_FENCE_RESTART_WAIT_CAP_S = 30.0
_FENCE_RESTART_WAIT_POLL_S = 0.05

# Restart-loop circuit breaker. A single crash restarts fast (0.1 s first
# delay, doubling toward _RESTART_MAX_DELAY_SECONDS); a STORM of crashes or
# failed starts inside one sliding window means the authority cannot
# currently stay up (cgroup OOM, corrupt volume) and an unbounded restart
# loop only adds spawn churn to an
# already-degraded host while producing no durable evidence of the verdict.
# Past the limit the runtime enters an explicit terminal `degraded` state:
# restarts stop, readiness stays revoked/fenced, and the only way out is a
# process restart (no runtime backend switch exists by design).
_DEFAULT_CRASH_WINDOW_S = 600.0
_DEFAULT_CRASH_LIMIT = 5
_DEGRADED_REMINDER_INTERVAL_S = 60.0


def _bounded_knob(raw: str, name: str, default: float,
                  lo: float, hi: float) -> float:
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            '[Storage] invalid %s=%r; using %s', name, raw, default)
        return default
    if not lo <= value <= hi:
        logger.warning(
            '[Storage] %s=%r outside [%s, %s]; clamping', name, raw, lo, hi)
    return max(lo, min(hi, value))


def _restart_breaker_config() -> tuple[float, int]:
    """Sliding-window crash budget before the restart loop gives up.

    Env-tunable and hard-bounded like the liveness knobs in
    ``lib/storage/supervisor.py``: a longer window or higher limit only
    delays the degraded verdict, it cannot revive a dead authority. Reads
    stay literal so contracts/config_knobs_v1.json tracks the knobs.
    """
    window = _bounded_knob(
        os.environ.get('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', '').strip(),
        'TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS',
        _DEFAULT_CRASH_WINDOW_S, 60.0, 86400.0)
    limit = int(_bounded_knob(
        os.environ.get('TOFU_STORAGE_RESTART_CRASH_LIMIT', '').strip(),
        'TOFU_STORAGE_RESTART_CRASH_LIMIT',
        _DEFAULT_CRASH_LIMIT, 1, 100))
    return window, limit


class StorageRuntime:
    """Own the sidecar and fail closed when its process disappears.

    A supervisor may restart the same configured backend, but this state never
    synthesizes a fallback backend and never reports ready before a new health
    handshake succeeds. A crash storm (more than
    ``TOFU_STORAGE_RESTART_CRASH_LIMIT`` crashes/failed starts inside
    ``TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS``) trips a circuit breaker:
    the runtime stops spawning, enters the terminal ``degraded`` state, and
    stays fenced until the process restarts.
    """

    def __init__(
        self,
        supervisor: StorageSupervisor | None = None,
        *,
        on_write_fence: Callable[[], None] | None = None,
        auto_restart: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._ready = False
        self._ready_condition = threading.Condition(self._lock)
        self._on_write_fence = on_write_fence
        self._auto_restart = auto_restart
        self._stopping = threading.Event()
        self._restart_thread: threading.Thread | None = None
        self._restart_attempts = 0
        self._last_error = ''
        self._last_exit_code: int | None = None
        (self._crash_window_seconds,
         self._crash_limit) = _restart_breaker_config()
        self._crash_events: deque[float] = deque()
        self._degraded = False
        self._degraded_reason = ''
        self._degraded_reminder_thread: threading.Thread | None = None
        self._supervisor = supervisor or StorageSupervisor()
        self._supervisor.add_crash_callback(self._crashed)

    def _record_restart_event_locked(self, now: float) -> int:
        """Add one crash/failed-start event to the sliding window."""
        self._crash_events.append(now)
        horizon = now - self._crash_window_seconds
        while self._crash_events and self._crash_events[0] < horizon:
            self._crash_events.popleft()
        return len(self._crash_events)

    def _enter_degraded(self, reason: str) -> None:
        """Trip the restart circuit breaker into the terminal degraded state.

        Idempotent: the first trip logs CRITICAL once; later events only
        refresh the reason. Readiness remains revoked and no further spawns
        are scheduled. Leaving degraded requires a process restart — there
        is deliberately no runtime surface that un-fences storage without
        re-running the full boot gates.
        """
        with self._lock:
            if self._degraded:
                self._degraded_reason = reason
                return
            self._degraded = True
            self._degraded_reason = reason
            self._ready = False
            self._last_error = reason
            self._ready_condition.notify_all()
        logger.critical(
            'Storage restart circuit breaker tripped: %s. Restarts are '
            'stopped and storage stays fenced (degraded); leaving this '
            'state requires a process restart', reason)
        with self._lock:
            reminder = self._degraded_reminder_thread
            if reminder is None or not reminder.is_alive():
                self._degraded_reminder_thread = threading.Thread(
                    target=self._degraded_reminder_loop,
                    name='storage-degraded-reminder',
                    daemon=True,
                )
                self._degraded_reminder_thread.start()

    def _crashed(self, code: int) -> None:
        with self._lock:
            self._ready = False
            self._last_exit_code = code
            self._last_error = f'sidecar exited unexpectedly ({code})'
            events = self._record_restart_event_locked(time.monotonic())
            degraded = self._degraded
        if self._on_write_fence is not None:
            try:
                self._on_write_fence()
            except Exception as exc:
                logger.error(
                    'Storage write-fence callback failed: %s: %s',
                    type(exc).__name__, str(exc)[:200])
        if degraded:
            logger.warning(
                'Storage writes remain fenced after sidecar exit code=%d; '
                'the runtime is degraded and will not restart storage', code)
            return
        # The breaker guards the restart loop; a manual-recovery runtime
        # (auto_restart=False) has no loop to break and keeps its explicit
        # operator-driven start() path.
        if self._auto_restart and events > self._crash_limit:
            self._enter_degraded(
                f'{events} storage crashes/failed starts within '
                f'{self._crash_window_seconds:.0f}s (limit '
                f'{self._crash_limit}); last exit code={code}')
            return
        if self._auto_restart and not self._stopping.is_set():
            logger.warning(
                'Storage writes fenced after sidecar exit code=%d; '
                'scheduling restart (crash %d/%d in %.0fs window)',
                code, events, self._crash_limit, self._crash_window_seconds)
            with self._lock:
                if self._restart_thread is None or not self._restart_thread.is_alive():
                    self._restart_thread = threading.Thread(
                        target=self._restart_loop,
                        name='storage-restart',
                        daemon=True,
                    )
                    self._restart_thread.start()

    def _restart_loop(self) -> None:
        delay = 0.1
        while not self._stopping.wait(delay):
            with self._lock:
                if self._degraded:
                    return
            try:
                with self._lock:
                    self._restart_attempts += 1
                    attempt = self._restart_attempts
                logger.info(
                    'Storage sidecar restart attempt %d starting (backoff '
                    'reached %.1fs)', attempt, delay)
                self._start(clear_stop=False)
                logger.info(
                    'Storage sidecar restart succeeded after %d attempt(s)',
                    self._restart_attempts)
                return
            except Exception as exc:
                # Readiness remains false; retry the same immutable supervisor
                # configuration without choosing another backend.
                with self._lock:
                    events = self._record_restart_event_locked(
                        time.monotonic())
                if self._auto_restart and events > self._crash_limit:
                    self._enter_degraded(
                        f'{events} storage crashes/failed starts within '
                        f'{self._crash_window_seconds:.0f}s (limit '
                        f'{self._crash_limit}); last restart error: '
                        f'{type(exc).__name__}: {str(exc)[:200]}')
                    return
                delay = min(_RESTART_MAX_DELAY_SECONDS, delay * 2)
                logger.warning(
                    'Storage sidecar restart attempt %d failed: %s: %s; '
                    'retrying in %.1fs (restart pressure %d/%d in %.0fs '
                    'window)',
                    self._restart_attempts, type(exc).__name__,
                    str(exc)[:200], delay, events, self._crash_limit,
                    self._crash_window_seconds)

    def _degraded_reminder_loop(self) -> None:
        """Periodic degraded-state reminder until shutdown.

        Entry into degraded logs CRITICAL once; this loop keeps a
        heartbeat-level WARNING in the evidence stream so a long-fenced
        process never goes silent about why it is serving 503s. The shared
        duplicate coalescer keeps the physical volume bounded while the
        occurrence delta preserves the true reminder count.
        """
        while not self._stopping.wait(_DEGRADED_REMINDER_INTERVAL_S):
            with self._lock:
                if not self._degraded:
                    return
                reason = self._degraded_reason
            logger.warning(
                'Storage runtime remains degraded (fenced): %s', reason)

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready and self._supervisor.ready

    def status(self) -> dict[str, object]:
        """Return the process-wide readiness state without probing storage."""
        supervisor = self._supervisor.status()
        with self._lock:
            ready = self._ready and bool(supervisor['ready'])
            restarting = bool(
                self._restart_thread is not None
                and self._restart_thread.is_alive()
            )
            if ready:
                state = 'ready'
            elif self._degraded:
                state = 'degraded'
            elif (self._stopping.is_set()
                    and supervisor['state'] == 'stopped'
                    and not restarting):
                state = 'stopped'
            elif self._stopping.is_set():
                state = 'stopping'
            elif restarting:
                state = 'restarting'
            else:
                state = str(supervisor['state'])
            return {
                **supervisor,
                'ready': ready,
                'state': state,
                'degraded': self._degraded,
                'restarting': restarting,
                'restart_attempts': self._restart_attempts,
                'last_exit_code': (
                    self._last_exit_code
                    if self._last_exit_code is not None
                    else supervisor['last_exit_code']
                ),
                'last_error': self._last_error,
            }

    def start(self) -> StorageClient:
        return self._start(clear_stop=True)

    def _start(self, *, clear_stop: bool) -> StorageClient:
        if clear_stop:
            self._stopping.clear()
        elif self._stopping.is_set():
            raise StorageError(
                'database_unavailable', 'Storage runtime is stopping')
        with self._lock:
            if self._degraded:
                raise StorageError(
                    'database_unavailable',
                    'Storage runtime is degraded after repeated crashes; '
                    'restart the process',
                    retryable=True, retry_after_ms=1000)
        try:
            client = self._supervisor.start()
            health = client.health()
            if not health.get('ready'):
                self._supervisor.stop()
                raise StorageError(
                    'database_unavailable',
                    'Storage sidecar failed its ready handshake')
        except Exception as exc:
            with self._lock:
                self._ready = False
                self._last_error = f'{type(exc).__name__}: {str(exc)[:200]}'
            raise
        stopping_after_handshake = False
        with self._lock:
            # A crash-restart thread can finish its health handshake while the
            # production shutdown owner is concurrently joining it.  Never
            # publish that late authority as ready after stop() won.
            if self._stopping.is_set():
                self._ready = False
                self._last_error = 'Storage runtime stopped during startup'
                stopping_after_handshake = True
            else:
                self._ready = True
                self._last_error = ''
            # Wake fence waiters either way: ready callers proceed, and a
            # lost-to-stop startup lets their restart-in-flight check fail.
            self._ready_condition.notify_all()
        if stopping_after_handshake:
            self._supervisor.stop()
            raise StorageError(
                'database_unavailable',
                'Storage runtime stopped during startup',
                retryable=True,
            )
        return client

    def _restart_in_flight_locked(self) -> bool:
        """True while a supervised crash-restart is expected to restore the
        authority shortly: a restart worker is alive, the runtime is not
        stopping, and the circuit breaker has not latched degraded. Any
        other not-ready state must keep failing fast (a stopped runtime
        must never park a request thread)."""
        return (
            not self._degraded
            and not self._stopping.is_set()
            and self._restart_thread is not None
            and self._restart_thread.is_alive()
        )

    def client(self, *, write: bool = False) -> StorageClient:
        """Return the ready client, waiting out a bounded in-flight restart.

        A crash-restart window (~10 s) used to fail every in-flight caller
        instantly with a classified fence error even though the caller's RPC
        deadline (20 s in production) would have carried it past the
        restart. While a restart worker is alive — and only then — park the
        caller on the readiness condition for up to
        ``_FENCE_RESTART_WAIT_CAP_S`` so a transient recycle surfaces as
        latency instead of a 503. Degraded/stopping/not-started states keep
        the original fail-fast semantics: waiting there would hide a real
        outage behind request-thread latency.
        """
        if self.ready:
            return self._supervisor.client
        deadline = time.monotonic() + _FENCE_RESTART_WAIT_CAP_S
        with self._ready_condition:
            while self._restart_in_flight_locked():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._ready_condition.wait(
                    min(_FENCE_RESTART_WAIT_POLL_S, remaining))
                if self.ready:
                    return self._supervisor.client
        if not self.ready:
            message = ('Storage writes are fenced while the sidecar is unavailable'
                       if write else 'Storage sidecar is unavailable')
            raise StorageError(
                'database_unavailable', message, retryable=True, retry_after_ms=100)
        return self._supervisor.client

    def stop(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + max(0.1, float(timeout))
        self._stopping.set()
        with self._lock:
            self._ready = False
            # Fail fence waiters fast: stopping ends any restart-in-flight
            # window, so a parked client() must re-check and raise now
            # rather than outlive the shutdown deadline.
            self._ready_condition.notify_all()
            restart_thread = self._restart_thread
            reminder_thread = self._degraded_reminder_thread
        self._supervisor.stop(timeout=max(0.1, deadline - time.monotonic()))
        for worker in (restart_thread, reminder_thread):
            if (worker is not None
                    and worker is not threading.current_thread()
                    and worker.is_alive()):
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if (restart_thread is not None
                and restart_thread is not threading.current_thread()
                and restart_thread.is_alive()):
            # Returning success here used to let the re-exec gate certify the
            # storage boundary while this daemon could still complete a late
            # start and reacquire the project lease.  Fail closed: ordinary
            # process exit releases the owner pipe, and the external manager
            # starts a fresh generation only after the OS lease disappears.
            raise StorageError(
                'database_timeout',
                'Storage restart worker did not stop before the shutdown deadline',
                retryable=True,
            )
        with self._lock:
            if self._restart_thread is restart_thread:
                self._restart_thread = None
            if self._degraded_reminder_thread is reminder_thread:
                self._degraded_reminder_thread = None


__all__ = ['StorageRuntime']
