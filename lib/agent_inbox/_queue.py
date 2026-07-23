"""lib/agent_inbox/_queue.py — thread-safe queue operations over the shared state.

All functions here mutate the single-home objects imported from
:mod:`lib.agent_inbox._state` (``_inboxes`` / ``_lock`` / ``_tombstones`` /
``_PRIORITY`` / ``MAX_PER_TASK`` / ``_TOMBSTONE_MAX``).  They are imported by
reference so every caller touches the SAME objects — this is what preserves the
exactly-once delivery semantics.

Concurrency: every public function is thread-safe. The orchestrator runs
``drain()`` on the main task thread; ``enqueue()`` is called from the swarm
scheduler's worker threads.
"""

from __future__ import annotations

import threading  # noqa: F401 — carried per repo convention (batch-18 lesson)
import time
from typing import Any

from lib.log import get_logger

from ._state import (
    MAX_PER_TASK,
    _PRIORITY,
    _TOMBSTONE_MAX,
    _inboxes,
    _lock,
    _tombstones,
)

logger = get_logger(__name__)


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
          max_items: int = 0,
          modes: list[str] | None = None,
          exclude_modes: list[str] | None = None) -> list[dict[str, Any]]:
    """Remove and return queued items for *task_id*, sorted by priority.

    Within a priority bucket items keep FIFO order (the order in which the
    sub-agents completed).

    Args:
        task_id: The owning task.
        max_items: If >0, drain at most this many; the rest stay queued for
            the next round. 0 (default) drains everything (that matches the
            mode filter).
        modes: If given, drain ONLY items whose ``mode`` is in this list.
            Non-matching items stay queued. Used by the endpoint/VU driver
            loops to drain only ``peer-msg`` items while leaving swarm updates
            for the main loop (and vice-versa).
        exclude_modes: If given, drain everything EXCEPT items whose ``mode``
            is in this list. Mutually usable with a bare call. The main
            orchestrator drains swarm items with ``exclude_modes=['peer-msg']``
            so peer items are left for whichever party owns peer delivery.

    Returns:
        List of items, oldest-and-most-urgent first. Empty list if nothing
        matched. Items filtered OUT by ``modes`` / ``exclude_modes`` remain in
        the inbox.
    """
    if not task_id:
        return []

    def _match(it: dict) -> bool:
        m = it.get('mode', '')
        if modes is not None and m not in modes:
            return False
        if exclude_modes is not None and m in exclude_modes:
            return False
        return True

    with _lock:
        bucket = _inboxes.get(task_id)
        if not bucket:
            return []

        # Only consider items that pass the mode filter; the rest are retained.
        candidates = [(i, it) for i, it in enumerate(bucket) if _match(it)]
        sorted_items = sorted(
            candidates,
            key=lambda pair: (_PRIORITY.get(pair[1].get('priority', 'later'), 2), pair[0]),
        )

        if max_items > 0:
            sorted_items = sorted_items[:max_items]

        drained_indices = {idx for idx, _ in sorted_items}
        if drained_indices:
            _inboxes[task_id] = [
                it for i, it in enumerate(bucket) if i not in drained_indices
            ]
            if not _inboxes[task_id]:
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
