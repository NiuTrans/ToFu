"""Per-round DB-connection checkpoint release (pt_03f4cdf1 slice 27).

Extracted 2026-07-31 from ``lib/tasks_pkg/orchestrator/_run.py``
run_task's stream loop, where it ran inline right before the per-round
LLM call. Byte-identical behaviour.

Why this checkpoint exists:

    run_task runs on a long-lived pooled worker thread whose
    thread-local PG connection holds a _conn_semaphore slot from its
    first DB op until close_thread_db() runs. That release otherwise
    lives ONLY in the terminal finally, so if the LLM call below spins
    (e.g. a total gateway-5xx outage rotating slots), the stuck task
    pins a connection slot for the WHOLE outage — and that semaphore
    is shared with the frontend's data endpoints
    (/api/v1/conversations, /api/health SELECT 1), which then can't
    acquire and hang ("backend alive, frontend dead").

    The connection is provably DB-idle at this point: all per-round
    writes above committed (db_execute_with_retry commit=True), and
    the streaming-tool pool runs NO DB, so nothing spans the stream.
    Releasing here caps connection-hold at one round; the next DB op
    transparently re-acquires via get_thread_db. Best-effort — a
    release failure must never break an otherwise-healthy task.
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def release_db_conn_checkpoint(*, round_num, tid):
    """Release the thread's pooled DB connection between stream rounds.

    Keyword-only ``round_num`` + ``tid`` exist solely for the debug log
    on failure — the helper needs no task state (the store lookup is
    internal and thread-local).

    Best-effort contract: a release failure is swallowed and logged at
    debug level; it must never break an otherwise-healthy task. The
    store import stays deferred (function-scope) exactly as in the
    inline original — module-scope would pull lib.agent_core.store into
    every orchestrator import.
    """
    try:
        from lib.agent_core.store import get_conversation_store
        get_conversation_store().release_connection()
    except Exception as _rel_err:
        logger.debug('[Task:%s] per-round release_connection failed at '
                     'round %d: %s', tid, round_num, _rel_err)
