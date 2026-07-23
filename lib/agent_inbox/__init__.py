"""lib/agent_inbox — Per-task model-facing inbox for async swarm updates.

This is the **model's** inbox (distinct from ``lib/push.py`` which is the UI's
push channel). When a sub-agent finishes in async swarm mode, its summary is
formatted as ``<swarm-update>...</swarm-update>`` XML and enqueued here. The
main task's orchestrator drains the inbox **between rounds** and prepends each
entry as a synthetic ``user`` message, so the main agent sees sub-agent results
naturally on its next turn — no polling, no busy-waiting.

Design borrows from Claude Code's ``messageQueueManager.ts`` (priority-sorted
single queue) but is scoped per task_id to avoid cross-task contamination.

This package was split from a single ``lib/agent_inbox.py`` module while keeping
the public import path byte-identical — ``from lib.agent_inbox import X`` and
``from lib import agent_inbox`` both work exactly as before.  Internally:

  * :mod:`lib.agent_inbox._state` — the SINGLE process-wide home of the shared
    exactly-once delivery registry (``_inboxes`` / ``_lock`` / ``_tombstones`` /
    ``_PRIORITY`` / ``MAX_PER_TASK`` / ``_TOMBSTONE_MAX``).
  * :mod:`lib.agent_inbox._queue` — the thread-safe queue operations.
  * :mod:`lib.agent_inbox._format` — the pure ``<swarm-update>`` XML formatter.

Usage::

    # 1) Sub-agent finishes — enqueue a notification
    from lib.agent_inbox import enqueue
    enqueue(task_id, payload, priority='later', mode='swarm-update')

    # 2) Orchestrator's between-round hook (coalesce all drained items
    #    into a single user-role message — no _isMeta flag, since
    #    <swarm-update> is factual data, not a system reminder).
    from lib.agent_inbox import drain
    items = drain(task_id)
    if items:
        messages.append({
            'role': 'user',
            'content': '\n\n'.join(it['value'] for it in items),
        })

    # 3) Task completes — clear the inbox AND tombstone it so late
    #    sub-agents can't recreate the bucket.
    from lib.agent_inbox import clear
    clear(task_id)

Concurrency: every public function is thread-safe. The orchestrator runs
``drain()`` on the main task thread; ``enqueue()`` is called from the swarm
scheduler's worker threads.
"""

from __future__ import annotations

from lib.log import get_logger

# ── Shared state (single-home, re-exported BY REFERENCE) ─────
from ._state import (
    MAX_PER_TASK,
    _PRIORITY,
    _TOMBSTONE_MAX,
    _inboxes,
    _lock,
    _tombstones,
)

# ── Queue operations ─────────────────────────────────────────
from ._queue import (
    clear,
    consume,
    consume_peer,
    drain,
    enqueue,
    has_pending,
    peek,
    reset_for_test,
    stats,
    untombstone,
)

# ── Pure formatter ───────────────────────────────────────────
from ._format import (
    _escape,
    format_swarm_update,
)

logger = get_logger(__name__)

__all__ = [
    # queue ops
    'enqueue',
    'drain',
    'consume',
    'consume_peer',
    'peek',
    'has_pending',
    'clear',
    'untombstone',
    'reset_for_test',
    'stats',
    # formatter
    'format_swarm_update',
    '_escape',
    # shared state / constants (tests + callers reach for these)
    'MAX_PER_TASK',
    '_PRIORITY',
    '_inboxes',
    '_lock',
    '_tombstones',
    '_TOMBSTONE_MAX',
]
