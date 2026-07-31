"""orchestrator/_teardown.py — run_task finally-block teardown (slice 5).

**Extraction context** (board epic ``pt_03f4cdf1``, slice 5):

The 44-line ``finally:`` block at the end of ``run_task`` performs 5
discrete teardown steps every task must run at exit, no matter how the
turn ended (normal, exception, abort). It is the symmetric counterpart
to ``_vu_startup.setup_project_context`` (which owns startup): startup
helpers run once at task begin, teardown helpers run once at task end.

Steps (must run IN THIS ORDER, each wrapped in its own try/except so
one failure never blocks the others — this is the "no-escape teardown"
contract every worker thread's finally block must uphold):

  1. ``presence.mark_idle(project_path, conv_id)`` — transition this
     conversation's peer from ACTIVE → IDLE on the "who is working
     here now" feed. Gated on ``project_path AND conv_id``: an
     early-fatal turn before ``cfg`` is bound still runs this
     defensively via ``task.get('config') or {}``.
  2. ``set_req_id('')`` — clear the thread-local request-id
     correlation tag. Pooled worker threads are reused across many
     tasks; a stale ``tid`` would mis-attribute the next task's log
     lines.
  3. ``clear_pinned_provider()`` — drop the hard multi-tenant
     provider pin so it can't bleed into the next task on this
     pooled worker.
  4. ``clear_conv_affinity()`` — drop the soft conv-sticky routing
     preference (per-key prompt-cache warming) for the same reason.
  5. ``get_conversation_store().release_connection()`` — return this
     thread's DB connection to the shared pool. Long-lived worker
     threads would otherwise pin one connection each for their
     entire lifetime, exhausting the connection semaphore under
     high concurrency (the "pool exhausted / tracked_threads ≫
     active" symptom).

Extracted as a single ``finalize_task_lane(task, tid)`` because every
step reads ONLY ``task`` + ``tid`` (and calls out to other lib.*
modules). No captures from any local variable inside run_task's body.
The caller (run_task's ``finally:``) becomes a one-liner.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger, set_req_id

logger = get_logger(__name__)


def finalize_task_lane(task: dict[str, Any], tid: str) -> None:
    """Run the 5-step teardown lane every task's finally block owns.

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
            _presence_mark_idle(_fin_pp, _fin_cid)
    except Exception as _pe:
        logger.debug('[Task:%s] presence mark_idle failed: %s', tid, _pe)

    # ── Clear the per-task request-id correlation tag (pooled threads
    #    are reused; a stale tid would mis-attribute the NEXT task's
    #    logs). ──
    set_req_id('')

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

    # ── Release this worker thread's thread-local DB connection back to
    #    the shared pool.  run_task runs on long-lived threads (the
    #    asyncio.to_thread default pool, or daemon task threads); without
    #    this each one would pin a PG connection for its entire lifetime,
    #    exhausting the connection semaphore under high concurrency (see
    #    the "pool exhausted / tracked_threads ≫ active" symptom). ──
    try:
        from lib.agent_core.store import get_conversation_store
        get_conversation_store().release_connection()
    except Exception as _ctd_err:
        logger.debug('[Task:%s] release_connection on task end failed: %s',
                     tid, _ctd_err)


__all__ = ['finalize_task_lane']
