"""lib/agent_inbox.py — Per-task model-facing inbox for async swarm updates.

This is the **model's** inbox (distinct from ``lib/push.py`` which is the UI's
push channel). When a sub-agent finishes in async swarm mode, its summary is
formatted as ``<swarm-update>...</swarm-update>`` XML and enqueued here. The
main task's orchestrator drains the inbox **between rounds** and prepends each
entry as a synthetic ``user`` message, so the main agent sees sub-agent results
naturally on its next turn — no polling, no busy-waiting.

Design borrows from Claude Code's ``messageQueueManager.ts`` (priority-sorted
single queue) but is scoped per task_id to avoid cross-task contamination.

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

import threading
import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Priority order (lower number = higher priority)
# ═══════════════════════════════════════════════════════════

_PRIORITY: dict[str, int] = {
    'now':   0,   # urgent — drained before user-typed input
    'next':  1,   # default — drained alongside user input
    'later': 2,   # background — system notifications, never starves user
}


# ═══════════════════════════════════════════════════════════
#  Per-task storage
# ═══════════════════════════════════════════════════════════

# task_id → list[InboxItem]; never grows beyond MAX_PER_TASK
_inboxes: dict[str, list[dict[str, Any]]] = {}
_lock = threading.Lock()

#: Hard cap per task to prevent runaway memory if orchestrator stops draining.
#: Items beyond this are dropped with a warning — the main agent will see fewer
#: notifications, but at least the process won't OOM. Calibrated to ~500 KB
#: assuming 2KB per swarm-update.
MAX_PER_TASK = 256

#: Tombstone: task_ids whose owning task has ended.  Late-arriving sub-agents
#: (a swarm worker that finished after ``clear()`` was called) will be
#: prevented from re-creating the inbox.  Bounded to avoid unbounded growth.
_tombstones: set[str] = set()
_TOMBSTONE_MAX = 1024


# ═══════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════

def enqueue(task_id: str, value: str, *,
            priority: str = 'later',
            mode: str = 'swarm-update',
            agent_id: str = '',
            extra: dict | None = None) -> None:
    """Enqueue a notification for *task_id*.

    Thread-safe. Logs and drops the item if the per-task cap is exceeded.

    Args:
        task_id: The owning task (matches ``task['id']`` in orchestrator).
        value: The string to inject as a ``user`` message body. Typically a
            ``<swarm-update>...</swarm-update>`` XML payload.
        priority: ``'now' | 'next' | 'later'`` (default ``'later'``).
        mode: A free-form string for grep / debug; orchestrator may also
            use it to render a UI chip ("📨 swarm-update").
        agent_id: Optional originating agent id for diagnostics.
        extra: Optional dict merged into the item for downstream consumers.
    """
    if not task_id:
        logger.warning('[Inbox] enqueue with empty task_id, dropping')
        return
    if priority not in _PRIORITY:
        logger.warning('[Inbox:%s] unknown priority %r, falling back to later',
                       task_id, priority)
        priority = 'later'

    item: dict[str, Any] = {
        'value':     value,
        'priority':  priority,
        'mode':      mode,
        'agent_id':  agent_id,
        'enqueued':  time.time(),
    }
    if extra:
        item.update(extra)

    with _lock:
        # Refuse to (re-)create an inbox for a task that has already ended.
        # Late-arriving sub-agents (the parent main task closed before the
        # swarm finished) would otherwise leak memory — nobody will drain
        # the bucket because the orchestrator has stopped looking.
        if task_id in _tombstones:
            logger.debug(
                '[Inbox:%s] enqueue (mode=%s agent=%s) dropped — task ended',
                task_id, mode, agent_id or '?')
            return
        bucket = _inboxes.setdefault(task_id, [])
        if len(bucket) >= MAX_PER_TASK:
            logger.warning(
                '[Inbox:%s] cap %d reached — dropping oldest item (mode=%s agent=%s)',
                task_id, MAX_PER_TASK, item['mode'], item.get('agent_id', '?'))
            # Drop the oldest 'later'-priority item, falling back to oldest overall
            drop_idx = -1
            for i, it in enumerate(bucket):
                if it.get('priority') == 'later':
                    drop_idx = i
                    break
            if drop_idx == -1:
                drop_idx = 0
            bucket.pop(drop_idx)
        bucket.append(item)

    logger.debug('[Inbox:%s] enqueued mode=%s priority=%s agent=%s len=%d (depth=%d)',
                 task_id, mode, priority, agent_id or '?', len(value),
                 len(_inboxes.get(task_id, [])))


def drain(task_id: str, *,
          max_items: int = 0) -> list[dict[str, Any]]:
    """Remove and return all queued items for *task_id*, sorted by priority.

    Within a priority bucket items keep FIFO order (the order in which the
    sub-agents completed).

    Args:
        task_id: The owning task.
        max_items: If >0, drain at most this many; the rest stay queued for
            the next round. 0 (default) drains everything.

    Returns:
        List of items, oldest-and-most-urgent first. Empty list if nothing
        was queued or the task has no inbox.
    """
    if not task_id:
        return []

    with _lock:
        bucket = _inboxes.get(task_id)
        if not bucket:
            return []

        sorted_items = sorted(
            enumerate(bucket),
            key=lambda pair: (_PRIORITY.get(pair[1].get('priority', 'later'), 2), pair[0]),
        )

        if max_items > 0:
            sorted_items = sorted_items[:max_items]
            drained_indices = {idx for idx, _ in sorted_items}
            _inboxes[task_id] = [
                it for i, it in enumerate(bucket) if i not in drained_indices
            ]
            if not _inboxes[task_id]:
                del _inboxes[task_id]
        else:
            del _inboxes[task_id]

        result = [it for _, it in sorted_items]

    if result:
        logger.info('[Inbox:%s] drained %d item(s): %s',
                    task_id, len(result),
                    [(it.get('mode'), it.get('agent_id', '?')) for it in result])
    return result


def consume(task_id: str, agent_ids) -> int:
    """Drop queued items whose ``agent_id`` is in *agent_ids*.

    Used to de-duplicate the two delivery channels: when ``await_agents``
    (or ``get_agent_result``) hands the model an agent's result directly in
    the tool return value, the pending ``<swarm-update>`` for that same agent
    would otherwise be injected AGAIN on the next round.  Calling this right
    after a synchronous return removes the now-redundant inbox item so the
    model sees each completion exactly once.

    Items for agents NOT in *agent_ids* are left untouched (they're still
    pending and will be delivered later).

    Returns the number of items removed.
    """
    if not task_id or not agent_ids:
        return 0
    ids = {str(a) for a in agent_ids}
    with _lock:
        bucket = _inboxes.get(task_id)
        if not bucket:
            return 0
        kept = [it for it in bucket if it.get('agent_id') not in ids]
        removed = len(bucket) - len(kept)
        if kept:
            _inboxes[task_id] = kept
        else:
            _inboxes.pop(task_id, None)
    if removed:
        logger.info('[Inbox:%s] consumed %d already-delivered item(s) for agents=%s',
                    task_id, removed, sorted(ids))
    return removed


def consume_peer(task_id: str, queue_ids) -> int:
    """Drop queued peer-message items whose ``queueId`` is in *queue_ids*.

    The Pillar #6 peer-message REVERSE-race de-dup twin of :func:`consume`. A
    live-target peer message is written to BOTH the durable ``message_queue``
    row AND a fast-path inbox item tagged with that row's ``queueId`` (in the
    item's ``extra``). Two delivery races exist and BOTH must collapse to a
    single delivery:

      • FORWARD (inbox drains first): the orchestrator drain hook injects the
        inbox item, then deletes the matching ``message_queue`` row by
        ``queueId`` so it can never be popped later.
      • REVERSE (task ends first): ``dispatch_next_queued`` pops the durable
        row as a fresh turn BEFORE the next drain — it calls THIS to drop the
        now-redundant inbox twin so it isn't re-injected on that fresh turn.

    Items whose ``queueId`` is not in *queue_ids* (and any swarm items, which
    carry no ``queueId``) are left untouched. Returns the number removed.
    """
    if not task_id or not queue_ids:
        return 0
    ids = {str(q) for q in queue_ids if q}
    if not ids:
        return 0
    with _lock:
        bucket = _inboxes.get(task_id)
        if not bucket:
            return 0
        kept = [it for it in bucket if str(it.get('queueId') or '') not in ids]
        removed = len(bucket) - len(kept)
        if kept:
            _inboxes[task_id] = kept
        else:
            _inboxes.pop(task_id, None)
    if removed:
        logger.info('[Inbox:%s] consumed %d peer item(s) already dispatched via '
                    'the durable queue lane (queueIds=%s)',
                    task_id, removed, sorted(ids))
    return removed


def peek(task_id: str) -> int:
    """Return the number of pending items for *task_id* without consuming them."""
    if not task_id:
        return 0
    with _lock:
        return len(_inboxes.get(task_id, []))


def has_pending(task_id: str) -> bool:
    """Return True if the inbox has at least one item."""
    return peek(task_id) > 0


def clear(task_id: str) -> int:
    """Discard all queued items for *task_id* AND tombstone the slot.

    Call this when the main task ends so:
      * any unread items are dropped now,
      * any LATE sub-agent completion (one that races the task end) is
        also dropped instead of being silently kept around forever.

    Returns the number of items that were unread when the task ended.
    """
    if not task_id:
        return 0
    with _lock:
        bucket = _inboxes.pop(task_id, None)
        # Tombstone — bounded LRU-ish: when the cap is hit, drop the
        # oldest tombstone to avoid unbounded growth.
        _tombstones.add(task_id)
        if len(_tombstones) > _TOMBSTONE_MAX:
            try:
                _tombstones.pop()  # set.pop = arbitrary element — fine here
            except KeyError as e:
                logger.debug('[Inbox] tombstone evict on empty set: %s', e)
    n = len(bucket) if bucket else 0
    if n:
        logger.info('[Inbox:%s] cleared %d unread item(s) on task end', task_id, n)
    return n


def untombstone(key: str) -> None:
    """Remove *key* from the tombstone set so its inbox can be re-created.

    Called when a fresh ``spawn_agents`` wave starts on a conversation whose
    previous swarm was explicitly aborted (which tombstoned the slot). Without
    this, the new wave's ``enqueue`` calls would be silently dropped because
    the slot is still tombstoned from the abort.
    """
    if not key:
        return
    with _lock:
        _tombstones.discard(key)


def reset_for_test(task_id: str = '') -> None:
    """Wipe all state (or just one task_id's state). Test-only helper.

    Production code should never call this — it bypasses the tombstone
    mechanism that prevents late-arriving sub-agents from leaking.
    """
    with _lock:
        if task_id:
            _inboxes.pop(task_id, None)
            _tombstones.discard(task_id)
        else:
            _inboxes.clear()
            _tombstones.clear()


def stats() -> dict[str, int]:
    """Return ``{task_id: queue_depth}`` snapshot for diagnostics."""
    with _lock:
        return {tid: len(items) for tid, items in _inboxes.items()}


# ═══════════════════════════════════════════════════════════
#  XML payload helpers — keep formatting consistent across callers
# ═══════════════════════════════════════════════════════════

def format_swarm_update(*,
                         agent_id: str,
                         role: str,
                         status: str,
                         elapsed_seconds: float,
                         tokens: int,
                         preview: str,
                         output_file: str = '',
                         remaining_running: int = 0,
                         remaining_pending: int = 0,
                         error: str = '') -> str:
    """Build a ``<swarm-update>`` XML payload for a single agent completion.

    Mirrors Claude Code's ``<task-notification>`` shape. The 200-char preview
    cap matches what we agreed in the design doc.
    """
    preview_clean = (preview or '').replace('\r', ' ').strip()
    if len(preview_clean) > 200:
        preview_clean = preview_clean[:200].rstrip() + '…'

    parts = [
        '<swarm-update>',
        f'  <agent-id>{_escape(agent_id)}</agent-id>',
        f'  <role>{_escape(role)}</role>',
        f'  <status>{_escape(status)}</status>',
        f'  <elapsed-seconds>{elapsed_seconds:.1f}</elapsed-seconds>',
        f'  <tokens>{int(tokens)}</tokens>',
    ]
    if output_file:
        parts.append(f'  <output-file>{_escape(output_file)}</output-file>')
    if error:
        parts.append(f'  <error>{_escape(error[:300])}</error>')
    if preview_clean:
        parts.append(f'  <preview>{_escape(preview_clean)}</preview>')
    if remaining_running or remaining_pending:
        parts.append(
            f'  <remaining running="{remaining_running}" '
            f'pending="{remaining_pending}"/>'
        )
    parts.append('</swarm-update>')
    return '\n'.join(parts)


def _escape(text: str) -> str:
    """Minimal XML escape for inline values."""
    if not text:
        return ''
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))
