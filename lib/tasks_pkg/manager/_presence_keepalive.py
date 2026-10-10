"""Task-registry-driven presence keepalive for project-attached chat tasks.

Responsibility: keep a conversation's presence peer ACTIVE for the whole
lifetime of its live task, not only while the model streams text. Presence
liveness (``lib.presence.registry.ACTIVE_TTL_SEC``) was previously refreshed
only by stream-sampled checkpoints, so any tool-execution window longer than
the TTL (test suites, builds, MCP calls) let a genuinely running conversation
flip to idle and vanish from ``presence.snapshot()`` — and with it from
``build_peer_status`` / ``build_brain_summary`` — while the sidebar's
task-status badge kept (correctly) showing it as responding.

Entry points:
  * ``ensure_started()`` — idempotent lazy start, called from
    ``orchestrator/_vu_startup.py`` right after ``presence.announce``.
  * ``stop_keepalive(timeout)`` — bounded join for server shutdown/tests.
  * ``_tick_once(tasks=None)`` — one refresh pass (tests inject ``tasks``).

Dependencies: ``lib.tasks_pkg.manager.runtime.chat_task_runtime`` (the live
task authority — the same registry abort consults) and
``lib.presence.registry.heartbeat``. Both imports are lazy so this leaf never
participates in an import cycle.

Lifecycle: at most one daemon thread (``presence-keepalive``) per process. It
retires itself when no live project-attached task remains (mirroring the
presence sweeper's empty-batch retirement) and is restarted by the next
project task's announce. A task that leaves the registry simply stops being
refreshed; crash semantics are unchanged — if the process dies the thread
dies and the TTL sweeps.
"""

from __future__ import annotations

import threading

from lib.log import get_logger

logger = get_logger(__name__)

# Refresh cadence. MUST stay comfortably below
# lib.presence.registry.ACTIVE_TTL_SEC (25s) so a peer's lastBeatTs never ages
# past the TTL between ticks even with scheduling jitter.
KEEPALIVE_INTERVAL_SEC = 10.0

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()


def _is_live_project_task(task: dict) -> bool:
    """True when a registry record represents live project-attached work."""
    if not isinstance(task, dict):
        return False
    if task.get('status') not in ('pending', 'running') or task.get('aborted'):
        return False
    if not (task.get('convId') or ''):
        return False
    cfg = task.get('config') or {}
    return bool(cfg.get('projectPath') or '')


def _heartbeat_task(task: dict) -> bool:
    """Refresh one task's presence liveness. Never raises."""
    try:
        from lib.presence import heartbeat as _presence_heartbeat
        from lib.tasks_pkg.manager._registry import task_user_id
        _presence_heartbeat(
            (task.get('config') or {}).get('projectPath') or '',
            task.get('convId') or '',
            user_id=int(task_user_id(task)),
        )
        return True
    except Exception as exc:
        logger.debug('[PresenceKeepalive] heartbeat failed task=%s: %s',
                     str(task.get('id') or '')[:8], exc)
        return False


def _tick_once(tasks=None) -> tuple[int, int]:
    """Refresh presence liveness for every live project-attached chat task.

    Returns ``(eligible, refreshed)``: ``eligible`` counts live
    project-attached tasks seen; ``refreshed`` counts heartbeats delivered.
    Tests pass ``tasks`` explicitly; production reads the live registry.
    """
    if tasks is None:
        try:
            from lib.tasks_pkg.manager.runtime import chat_task_runtime
            tasks = chat_task_runtime.snapshot()
        except Exception as exc:
            logger.debug('[PresenceKeepalive] registry snapshot failed: %s',
                         exc)
            return 0, 0
    eligible = 0
    refreshed = 0
    for task in tasks or []:
        if not _is_live_project_task(task):
            continue
        eligible += 1
        refreshed += int(_heartbeat_task(task))
    return eligible, refreshed


def _live_project_task_count() -> int:
    """Count live project-attached tasks, or -1 when the probe fails.

    -1 fails safe: a transient registry error must not retire the keepalive
    while work may still be running; the next tick re-probes.
    """
    try:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        return sum(
            1 for task in chat_task_runtime.snapshot()
            if _is_live_project_task(task))
    except Exception as exc:
        logger.debug('[PresenceKeepalive] live-count probe failed: %s', exc)
        return -1


def _retire_if_idle(thread: threading.Thread) -> bool:
    """Detach exactly ``thread`` when no live project task remains.

    The eligibility re-check runs under the lifecycle lock so an
    ``ensure_started`` racing a retiring thread always sees a consistent
    state: a task that announced before the re-check is counted (no
    retirement), and one that announces after detachment starts a fresh
    thread.
    """
    global _thread
    with _lock:
        if _thread is not thread or _live_project_task_count() != 0:
            return False
        _thread = None
        return True


def _loop(interval: float) -> None:
    current = threading.current_thread()
    while not _stop.wait(interval):
        try:
            _tick_once()
        except Exception as exc:
            logger.debug('[PresenceKeepalive] tick failed: %s', exc)
        try:
            if _retire_if_idle(current):
                logger.info('[PresenceKeepalive] retired (no live tasks)')
                return
        except Exception as exc:
            logger.debug('[PresenceKeepalive] retire check failed: %s', exc)


def ensure_started(interval: float = KEEPALIVE_INTERVAL_SEC) -> bool:
    """Start the single keepalive daemon iff none is alive. Never twice."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        try:
            bounded = max(0.1, float(interval))
        except (TypeError, ValueError, OverflowError):
            bounded = KEEPALIVE_INTERVAL_SEC
        thread = threading.Thread(
            target=_loop, args=(bounded,), name='presence-keepalive',
            daemon=True)
        _thread = thread
        try:
            thread.start()
        except Exception:
            if _thread is thread:
                _thread = None
            raise
    logger.info('[PresenceKeepalive] started (interval=%.1fs)', bounded)
    return True


def stop_keepalive(timeout: float = 2.0) -> bool:
    """Signal and bounded-join the keepalive daemon."""
    global _thread
    _stop.set()
    with _lock:
        thread = _thread
    if thread is None:
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[PresenceKeepalive] invalid stop timeout; using 2.0: %s',
                     exc)
        wait_seconds = 2.0
    if thread is not threading.current_thread():
        thread.join(timeout=wait_seconds)
    if thread.is_alive():
        return False
    with _lock:
        if _thread is thread:
            _thread = None
    return True


__all__ = [
    'KEEPALIVE_INTERVAL_SEC',
    'ensure_started',
    'stop_keepalive',
]
