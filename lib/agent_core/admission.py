"""lib/agent_core/admission.py — Backpressure + event-driven task waiting.

The headless agent-API surfaces (``/api/v1/agent/run``,
``/api/v1/chat/completions``, and the OpenAI/Anthropic compat adapters)
used to (a) spawn tasks with no concurrency ceiling and (b) block on the
result by busy-waiting with ``time.sleep`` inside a sync route handler —
which pinned a Hypercorn thread-pool worker for the whole task lifetime
and, because ``spawn_task`` runs the worker on that *same* default
executor via ``asyncio.to_thread``, starved the pool under load.

This module provides the two primitives that fix both:

1. :class:`AdmissionController` — a global ``asyncio.Semaphore`` that
   bounds the number of concurrently in-flight agent tasks. Handlers
   call :meth:`AdmissionController.try_acquire` (non-blocking) and return
   ``503 + Retry-After`` when saturated instead of spawning unbounded
   work. :meth:`release` is called when the task terminates.

2. A per-task **waiter registry** that lets an ``async def`` handler
   ``await`` task progress instead of polling. ``run_task`` (on a worker
   thread) calls :func:`notify_task` on every event and
   :func:`notify_task` again — flagged terminal — on the final event; the
   handler's coroutine wakes via a loop-bound ``asyncio.Event``. This is
   the same cross-thread wakeup pattern :class:`lib.agent_core.push.PushHub`
   uses (``loop.call_soon_threadsafe``).

Both are process-global singletons; import the module-level helpers.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import weakref
from typing import Optional

from lib.log import get_logger

logger = get_logger(__name__)

_pressure_warn_lock = threading.Lock()
_last_pressure_warn = 0.0


# ── Admission control ───────────────────────────────────────────────


def _default_max_inflight() -> int:
    """Resolve the concurrent-task ceiling from env, with a safe default.

    The personal-server default is 16, leaving headroom in the dedicated
    agent executor (16 workers by default) for terminalisation and recovery work instead
    of admitting hundreds of tasks that all retain history/model buffers.
    Override via
    ``TOFU_MAX_INFLIGHT_TASKS``; ``0`` disables the ceiling entirely
    (legacy unbounded behaviour — not recommended).
    """
    try:
        n = int(os.environ.get('TOFU_MAX_INFLIGHT_TASKS', '') or '16')
    except (ValueError, TypeError) as e:
        logger.debug('[Admission] TOFU_MAX_INFLIGHT_TASKS parse failed, using default: %s', e)
        n = 16
    return max(0, n)


def _memory_pressure_allows_admission() -> bool:
    """Fail closed before spawning more resident work near cgroup OOM.

    The cgroup is shared with editors, browser renderers, PostgreSQL, and other
    user processes.  A fixed task-count ceiling cannot see that those siblings
    have already consumed the remaining headroom.  Reading two cgroupfs scalar
    files at task admission is cheap and avoids starting a history/model-buffer
    heavy agent while the kernel is already choosing an OOM victim.

    Off-cgroup/unreadable environments fail open. Set
    ``TOFU_ADMISSION_CGROUP_PCT=0`` to disable this independent safety gate.
    """
    # Never let a host's unrelated test-runner cgroup pressure make otherwise
    # deterministic unit tests return 503. Tests that exercise this gate opt in
    # by setting the knob explicitly.
    if ('pytest' in sys.modules
            and 'TOFU_ADMISSION_CGROUP_PCT' not in os.environ):
        return True
    raw = os.environ.get('TOFU_ADMISSION_CGROUP_PCT', '96')
    try:
        threshold = float(raw or '96')
    except (TypeError, ValueError):
        logger.debug('[Admission] invalid TOFU_ADMISSION_CGROUP_PCT=%r; '
                     'using 96', raw)
        threshold = 96.0
    if threshold <= 0:
        return True
    threshold = max(50.0, min(100.0, threshold))
    try:
        from lib.cgroup_guard import pressure
        snap = pressure()
    except Exception as e:
        logger.debug('[Admission] cgroup pressure probe failed open: %s', e)
        return True
    if not snap:
        return True
    try:
        pct = float(snap.get('pct', 0) or 0)
    except (TypeError, ValueError) as e:
        logger.debug('[Admission] malformed cgroup pressure snapshot %r: %s',
                     snap, e)
        return True
    if pct < threshold:
        return True
    global _last_pressure_warn
    now = time.monotonic()
    with _pressure_warn_lock:
        should_log = now - _last_pressure_warn >= 30.0
        if should_log:
            _last_pressure_warn = now
    if should_log:
        logger.warning('[Admission] refusing new agent: shared cgroup %.1f%% full '
                       '(threshold %.0f%%); retry after memory pressure subsides',
                       pct, threshold)
    return False


def _admit_slot_ttl() -> float:
    """Lease TTL for an admission slot (seconds) — the crash-only backstop.

    Generous (default 1h) because a slot is normally released EAGERLY on the
    task's terminal event (``on_terminal`` → :meth:`release`); the TTL only
    reclaims a slot whose owning replica crashed mid-task. Override via
    ``TOFU_ADMIT_SLOT_TTL``.
    """
    try:
        n = float(os.environ.get('TOFU_ADMIT_SLOT_TTL', '') or '3600')
    except (ValueError, TypeError) as e:
        logger.debug('[Admission] TOFU_ADMIT_SLOT_TTL parse failed, using default: %s', e)
        n = 3600.0
    return max(1.0, n)


class AdmissionController:
    """Bounds concurrent in-flight agent tasks via the shared lease store.

    Re-keyed (Build Order step 2) onto ``lib.runtime_state_store`` via the
    ATOMIC bounded ``acquire_slot`` primitive: the in-flight COUNT is
    authoritative in the store, so under ``TOFU_RUNTIME_STATE_BACKEND=redis``
    the ceiling is ``N``-invariant across replicas and a crashed replica's
    slots reclaim by lease TTL; under the default ``inproc`` backend the
    behaviour is byte-equivalent to the old in-process semaphore. The atomic
    acquire means concurrent admits can never overshoot the ceiling.

    The public contract is unchanged — ``try_acquire()`` / ``release()`` take
    no id (6 call sites rely on that). Since ``release()`` has no id, THIS
    replica tracks the slot keys it minted in an in-process LIFO and releases
    one per ``release()`` call. That bookkeeping is intentionally per-replica
    (a replica only releases its OWN slots; a crashed replica's slots reclaim
    by TTL) — it does NOT undermine the cross-replica count, which lives in the
    store. ``max_inflight=0`` means unbounded.
    """

    _KIND = 'admit'

    def __init__(self, max_inflight: Optional[int] = None):
        self.max_inflight = (_default_max_inflight()
                             if max_inflight is None else max(0, max_inflight))
        self._lock = threading.Lock()
        self._held: list[str] = []  # this replica's minted slot keys (LIFO)
        self._ttl = _admit_slot_ttl()
        self._last_refusal_reason = ''
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None

    def _store(self):
        from lib.runtime_state_store import get_store
        return get_store()

    def try_acquire(self) -> bool:
        """Acquire a slot without blocking.

        Returns True if a slot was granted (caller MUST later call
        :meth:`release`), False if the server is at capacity (503). Always
        True when task-count admission is unbounded and the independent memory
        pressure gate is open. Atomic — no check-then-act overshoot.
        """
        if not _memory_pressure_allows_admission():
            with self._lock:
                self._last_refusal_reason = 'memory_pressure'
            return False
        import uuid
        slot_key = uuid.uuid4().hex
        ok = self._store().acquire_slot(
            self._KIND, slot_key, limit=self.max_inflight, ttl=self._ttl,
            count_prefix='')
        if ok:
            with self._lock:
                self._held.append(slot_key)
                self._last_refusal_reason = ''
            self._ensure_heartbeat()
        else:
            with self._lock:
                self._last_refusal_reason = 'capacity'
        return ok

    def _ensure_heartbeat(self) -> None:
        """Start one lease-heartbeat thread for this controller, lazily.

        Admission leases are crash-reclaim metadata, never a task lifetime.
        Refreshing the exact locally-held slot set guarantees that an active
        multi-hour LLM task remains counted no matter how many lease TTL windows
        it crosses.  A weak reference lets short-lived test/controllers vanish
        without a daemon thread retaining them forever.
        """
        with self._lock:
            if (self._heartbeat_thread is not None
                    and self._heartbeat_thread.is_alive()):
                return
            self._heartbeat_stop.clear()
            interval = min(30.0, max(0.25, self._ttl / 3.0))
            controller_ref = weakref.ref(self)
            stop_event = self._heartbeat_stop

            def _loop():
                while not stop_event.wait(interval):
                    ctrl = controller_ref()
                    if ctrl is None:
                        return
                    ctrl.refresh_held_slots()
                    # Do not retain the controller across the blocking wait.
                    del ctrl

            self._heartbeat_thread = threading.Thread(
                target=_loop, name='tofu-admission-heartbeat', daemon=True)
            self._heartbeat_thread.start()

    def refresh_held_slots(self) -> None:
        """Re-arm leases for every task this living replica still accounts for."""
        with self._lock:
            held = tuple(self._held)
        if not held:
            return
        try:
            self._store().refresh_slots(
                self._KIND, held, ttl=self._ttl, count_prefix='')
        except Exception as e:
            # Admission remains fail-open. A later heartbeat retries, while the
            # tasks themselves continue completely unaffected.
            logger.warning('[Admission] slot heartbeat failed: %s', e)

    def shutdown(self, timeout: float = 1.0) -> None:
        """Stop the heartbeat helper; does not abort or release live tasks."""
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def release(self) -> None:
        """Return a slot. Idempotent-safe against over-release (pops one of
        THIS replica's minted slot keys and deletes it from the store)."""
        with self._lock:
            slot_key = self._held.pop() if self._held else None
        if slot_key is None:
            # Over-release (more releases than acquires on this replica) — the
            # store count is already correct; nothing to delete. Log at debug.
            logger.debug('[Admission] release with no held slot — over-release')
            return
        try:
            self._store().release_slot(self._KIND, slot_key, '')
        except Exception as e:
            logger.warning('[Admission] slot release failed: %s', e)

    @property
    def in_flight(self) -> int:
        # Authoritative cross-replica count (store); fail-open → 0.
        return self._store().count_slots(self._KIND, '')

    @property
    def capacity(self) -> int:
        return self.max_inflight

    def stats(self) -> dict:
        inflight = self.in_flight
        with self._lock:
            refusal_reason = self._last_refusal_reason
        return {
            'in_flight': inflight,
            'capacity': self.max_inflight,
            'available': (max(0, self.max_inflight - inflight)
                          if self.max_inflight > 0 else -1),
            'last_refusal_reason': refusal_reason,
        }


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
