"""lib/agent_inbox — Per-key model-facing inbox for async notifications.

This is the **model's** inbox (distinct from ``lib/push.py`` which is the UI's
push channel). Producers enqueue XML payloads under a lane tag (``mode``);
the main task's orchestrator drains the inbox **between rounds** and coalesces
the entries into one synthetic ``user`` message, so the main agent sees async
results naturally on its next turn — no polling, no busy-waiting.

The bucket is SHARED across lanes, each with its own producer and delivery
discipline:

  * ``swarm-update`` — sub-agent completions (``<swarm-update>`` XML),
    produced by the swarm master's worker threads.
  * ``background-command`` — detached ``run_command`` completions; the inbox
    item is the volatile fast-path twin of a durable ``message_queue`` row.
  * ``user-steer`` — operator interjections into a live turn (committed
    durable-before-wakeup via :func:`enqueue_after_durable_commit`).
  * ``peer-msg`` — Pillar #6 peer messages (dual-written with the durable
    queue; de-duped via :func:`consume_peer`).

Lane tags let every lifecycle owner drain/clear ONLY the lane it owns (see
the ``modes=`` / ``exclude_modes=`` filters on ``drain`` / ``clear`` /
``has_pending`` / ``pending_items``).

The queue is priority-sorted and scoped per key to avoid cross-task
contamination.

This package was split from a single ``lib/agent_inbox.py`` module while keeping
the public import path byte-identical — ``from lib.agent_inbox import X`` and
``from lib import agent_inbox`` both work exactly as before.  Internally:

  * :mod:`lib.agent_inbox._state` — the SINGLE process-wide home of the shared
    exactly-once delivery registry (``_inboxes`` / ``_lock`` / ``_tombstones`` /
    ``_PRIORITY``).
  * :mod:`lib.agent_inbox._queue` — the thread-safe queue operations.
  * :mod:`lib.agent_inbox._format` — the pure ``<swarm-update>`` XML formatter.

Usage::

    # 1) Sub-agent finishes — enqueue a notification
    from lib.agent_inbox import enqueue
    enqueue(task_id, payload, priority='later', mode='swarm-update')

    # 2) Orchestrator's per-round hook. The real seam
    #    (``lib.tasks_pkg.orchestrator._swarm_inbox.drain_and_inject_inbox``)
    #    drains every lane and routes the coalesced payload through the
    #    registered writer so the carrier stays registry-attributed:
    from lib.tasks_pkg.context_composer import append_injection
    append_injection(messages, 'inbox_delivery', coalesced_payload,
                     task=task, round_num=round_num,
                     contains_human_steer=bool(steer_items))
    # Do NOT hand-append a bare ``{'role': 'user', ...}`` dict here — that
    # bypasses registry attribution and the context manifest (see tests:
    # test_runtime_prompt_writers_cannot_bypass). Display stays with the
    # ``_inboxInjects`` chip (the ``<swarm-update>`` card); the carrier does
    # NOT also emit a ``system_note`` context-injection row — that rendered
    # the same payload a second time.

    # 3) Task completes — clear the inbox AND tombstone it so late
    #    sub-agents can't recreate the bucket.
    from lib.agent_inbox import clear
    clear(task_id)

Concurrency: every public function is thread-safe. The orchestrator runs
``drain()`` on the main task thread; ``enqueue()`` is called from producer
threads (swarm scheduler workers, the background-command daemon, sync
request handlers).
"""

from __future__ import annotations

from lib.log import get_logger

# ── Shared state (single-home, re-exported BY REFERENCE) ─────
from ._state import (
    _PRIORITY,
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
    enqueue_after_durable_commit,
    has_pending,
    is_tombstoned,
    peek,
    pending_items,
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
    'enqueue_after_durable_commit',
    'drain',
    'consume',
    'consume_peer',
    'peek',
    'pending_items',
    'has_pending',
    'is_tombstoned',
    'clear',
    'untombstone',
    'reset_for_test',
    'stats',
    # formatter
    'format_swarm_update',
    '_escape',
    # shared state / constants (tests + callers reach for these)
    '_PRIORITY',
    '_inboxes',
    '_lock',
    '_tombstones',
]
