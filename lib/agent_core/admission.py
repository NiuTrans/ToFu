"""Root execution accounting and event-driven task waiting.

Accepted work receives an exact local ownership token; task count and memory
observations cannot refuse it. Terminal settlement releases the token once.
No shared capacity lease, TTL or heartbeat participates in execution. The
waiter registry wakes async request handlers from worker-thread task events,
so waiting for a result never pins an HTTP worker thread.

Dependencies: standard library and logging only. ExecutionSession carries
user/task identity and owns token release; this module owns no durable state.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Optional

from lib.log import get_logger

logger = get_logger(__name__)


class AdmissionController:
    """Track exact execution ownership without a task admission ceiling.

    The historical name and ``max_inflight`` input remain for existing
    compositions. A positive old setting no longer rejects user work.
    """

    def __init__(self, max_inflight: Optional[int] = None):
        self.max_inflight = 0
        self._lock = threading.Lock()
        self._held: dict[str, None] = {}

    def acquire(self) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._held[token] = None
        return token

    def release(self, lease_id: str | None = None) -> bool:
        """Release only an owned token; duplicate/foreign releases are no-ops."""
        with self._lock:
            token = (next(reversed(self._held), None) if lease_id is None
                     else str(lease_id or ''))
            if token not in self._held:
                return False
            del self._held[token]
            return True

    def try_acquire(self) -> bool:
        return bool(self.acquire())

    def resource_snapshot(self) -> dict:
        with self._lock:
            return {'capacity': 0, 'used': len(self._held),
                    'blocked': 0, 'pressure': False}

    def resize(self, capacity: int) -> None:
        """Legacy feedback cannot reinstate a root execution ceiling."""
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError('capacity must be a positive integer')

    def shutdown(self, timeout: float = 1.0) -> None:
        """Live tokens remain owned until their execution settles."""

    @property
    def in_flight(self) -> int:
        with self._lock:
            return len(self._held)

    @property
    def capacity(self) -> int:
        return 0

    def stats(self) -> dict:
        return {'in_flight': self.in_flight, 'capacity': 0,
                'available': -1, 'last_refusal_reason': ''}


# Process-global controller used by every headless entry point.
controller = AdmissionController()


# ── Per-task event-driven waiter ────────────────────────────────────


class _Waiter:
    """Loop-bound wakeup flag for one task.

    ``event`` is created on (and belongs to) the loop that registered the
    waiter — the Hypercorn event loop in production. Cross-thread signals
    from ``run_task``'s worker thread go through ``loop.call_soon_threadsafe``.
    """

    __slots__ = ('event', 'loop', 'terminal')

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.event = asyncio.Event()
        self.loop = loop
        self.terminal = False


_waiters: dict[str, _Waiter] = {}
_waiters_lock = threading.Lock()

_TERMINAL_STATES = ('done', 'error', 'aborted')


def register_waiter(task_id: str) -> None:
    """Register a wakeup flag for ``task_id``, bound to the current loop.

    MUST be called from within the async handler (on the event loop)
    BEFORE the task is spawned, so no terminal signal is missed.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Not on a loop (sync caller / tests without a running loop) —
        # the waiter is a no-op; callers fall back to status polling.
        logger.debug('[Admission] register_waiter(%s) with no running loop',
                     task_id[:8])
        return
    with _waiters_lock:
        _waiters[task_id] = _Waiter(loop)


def unregister_waiter(task_id: str) -> None:
    """Drop a task's waiter. Safe to call when absent."""
    with _waiters_lock:
        _waiters.pop(task_id, None)


def notify_task(task_id: str, *, terminal: bool = False) -> None:
    """Wake any coroutine waiting on ``task_id``.

    Thread-safe: callable from the orchestrator worker thread. Schedules
    the ``asyncio.Event.set`` onto the waiter's own loop. No-op when no
    waiter is registered (e.g. fire-and-forget tasks).
    """
    with _waiters_lock:
        w = _waiters.get(task_id)
    if w is not None:
        if terminal:
            w.terminal = True
        loop = w.loop
        ev = w.event
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(ev.set)
            else:
                # Loop not running (single-threaded test path): set directly.
                ev.set()
        except RuntimeError as e:
            # Loop closed between registration and signal — harmless.
            logger.debug('[Admission] notify_task(%s) loop signal failed: %s',
                         task_id[:8], e)
    if terminal:
        # Disposal/cleanup callbacks fire once the task is genuinely done —
        # independent of whether an async handler registered a waiter (a
        # fire-and-forget BYO task still needs its slot disposed).
        fire_terminal_callbacks(task_id)


async def wait_for_event(task_id: str, *, timeout: float) -> bool:
    """Await the next nudge for ``task_id`` (or ``timeout`` seconds).

    Clears the flag BEFORE returning control so the caller drains events,
    then re-arms for the next nudge. Returns True if woken by a signal,
    False on timeout (used to drive SSE heartbeats). The clear-before-drain
    discipline lives in the caller; here we just clear-then-wait.
    """
    with _waiters_lock:
        w = _waiters.get(task_id)
    if w is None:
        # No waiter (shouldn't happen on the async path) — degrade to a
        # short sleep so the caller still makes progress.
        await asyncio.sleep(min(timeout, 0.1))
        return False
    w.event.clear()
    try:
        await asyncio.wait_for(w.event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


# ── Terminal-completion callbacks ───────────────────────────────────
# Replaces the per-request "busy-poll until terminal, then dispose"
# daemon threads (BYO ephemeral-slot + custom-tool-env disposal). The
# callback fires once, on the worker thread, the moment the terminal
# event is appended — no extra thread, no polling, no 1h ceiling timer.

_term_callbacks: dict[str, list] = {}
_term_callbacks_lock = threading.Lock()


def on_terminal(task_id: str, fn) -> None:
    """Register ``fn(task_id)`` to run exactly once when the task ends.

    Multiple callbacks per task run in registration order. A callback
    registered for an ALREADY-terminal task runs immediately (the caller
    holds that race when it registers after spawn).
    """
    with _term_callbacks_lock:
        _term_callbacks.setdefault(task_id, []).append(fn)


def fire_terminal_callbacks(task_id: str) -> None:
    """Invoke + clear all terminal callbacks for ``task_id``.

    Called from :func:`notify_task` on the terminal signal. Each callback
    is isolated: one raising never blocks the others or the caller.
    """
    with _term_callbacks_lock:
        callbacks = _term_callbacks.pop(task_id, None)
    if not callbacks:
        return
    for fn in callbacks:
        try:
            fn(task_id)
        except Exception as e:
            logger.error('[Admission] terminal callback for task=%s failed: %s',
                         task_id[:8], e, exc_info=True)


async def await_terminal(task: dict, *, timeout_s: float) -> bool:
    """Block (async) until ``task`` reaches a terminal state or times out.

    Replaces the per-handler ``_wait_for_terminal`` busy-loop. Returns
    True on terminal, False on timeout. Does NOT raise — the caller
    decides how to surface a timeout (and should log it).
    """
    if task.get('status') in _TERMINAL_STATES:
        return True
    task_id = task.get('id') or ''
    deadline = time.time() + timeout_s
    while task.get('status') not in _TERMINAL_STATES:
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        # Wake on the next event; cap the wait so a missed/coalesced
        # signal still re-checks status within ~1s (defence in depth).
        await wait_for_event(task_id, timeout=min(remaining, 1.0))
    return True


__all__ = [
    'AdmissionController', 'controller',
    'register_waiter', 'unregister_waiter', 'notify_task',
    'wait_for_event', 'await_terminal',
    'on_terminal', 'fire_terminal_callbacks',
]
