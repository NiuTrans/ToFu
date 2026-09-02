"""Task-lane teardown for presence and thread-local runtime context.

Entry point: :func:`finalize_task_lane`. Every cleanup is independent so one
failure cannot prevent the remaining lifecycle owners from being cleared.
Durable storage is Sidecar-owned and has no worker-thread connection cleanup.
"""

from __future__ import annotations

from typing import Any

from lib.log import clear_log_context, get_logger, set_req_id

logger = get_logger(__name__)


def finalize_task_lane(task: dict[str, Any], tid: str) -> None:
    """Run the no-escape teardown lane every task's finally block owns.

    Each step is wrapped in its own try/except so one failure NEVER
    blocks the others. Debug-logged on failure; never raised. This is
    the "no-escape teardown" contract every worker thread's finally
    block must uphold.

    Args:
        task: the live task dict — read for ``config``, ``convId`` (and
            gated on those being present so a fatal-before-cfg-bound
            turn doesn't skip the whole teardown).
        tid: the 8-char task-id prefix for log correlation.
    """
    # ── Presence: this conversation's turn ended — transition its peer
    #    to IDLE (keep it; the sweep fades it after the idle window, and
    #    an autopilot follow-up turn re-announces the SAME peer to
    #    ACTIVE, so we never flicker gone→active between back-to-back
    #    turns). Reads config defensively (an early fatal may precede
    #    cfg binding).
    try:
        _fin_cfg = task.get('config') or {}
        _fin_pp = _fin_cfg.get('projectPath') or ''
        _fin_cid = task.get('convId') or ''
        if _fin_pp and _fin_cid:
            from lib.presence import mark_idle as _presence_mark_idle
            from lib.tasks_pkg.manager import task_user_id
            _presence_mark_idle(
                _fin_pp,
                _fin_cid,
                user_id=int(task_user_id(task)),
            )
    except Exception as _pe:
        logger.debug('[Task:%s] presence mark_idle failed: %s', tid, _pe)

    # ── Clear the per-task request-id correlation tag (pooled threads
    #    are reused; a stale tid would mis-attribute the NEXT task's
    #    logs). ──
    set_req_id('')
    clear_log_context()

    # ── Clear the hard provider pin so it can't bleed into the NEXT
    #    task that lands on this pooled worker thread. ──
    try:
        from lib.llm_dispatch.provider_pin import clear_pinned_provider
        clear_pinned_provider()
    except Exception as _pp_err:
        logger.debug('[Task:%s] clear_pinned_provider failed: %s', tid, _pp_err)

    # ── Clear the conversation binding (pooled threads are reused). ──
    try:
        from lib.llm_dispatch.conv_affinity import clear_conv_affinity
        clear_conv_affinity()
    except Exception as _ca_err:
        logger.debug('[Task:%s] clear_conv_affinity failed: %s', tid, _ca_err)

__all__ = ['finalize_task_lane']
