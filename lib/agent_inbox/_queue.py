"""lib/agent_inbox/_queue.py — thread-safe queue operations over the shared state.

All functions here mutate the single-home objects imported from
:mod:`lib.agent_inbox._state` (``_inboxes`` / ``_lock`` / ``_tombstones`` /
``_PRIORITY``).  They are imported by
reference so every caller touches the SAME objects — this is what preserves the
exactly-once delivery semantics.

Concurrency: every public function is thread-safe. The orchestrator runs
``drain()`` on the main task thread; ``enqueue()`` is called from producer
threads (swarm scheduler workers, the background-command daemon, sync
request handlers).
"""

from __future__ import annotations

import threading  # noqa: F401 — carried per repo convention (batch-18 lesson)
import time
from typing import Any, Callable

from lib.log import get_logger

from ._state import (
    _PRIORITY,
    _consumed_queue_ids,
    _inboxes,
    _lock,
    _tombstones,
)

logger = get_logger(__name__)


def _append_item_locked(task_id: str, item: dict[str, Any]) -> None:
    """Append to the task bucket while the caller owns ``_lock``."""
    bucket = _inboxes.setdefault(task_id, [])
    bucket.append(item)


# ═══════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════

def enqueue(task_id: str, value: str, *,
            mode: str,
            priority: str = 'later',
            agent_id: str = '',
            extra: dict | None = None) -> bool:
    """Enqueue a notification for *task_id*.

    Thread-safe. Logs and drops the item if the per-task cap is exceeded.

    Args:
        task_id: The owning task (matches ``task['id']`` in orchestrator).
        value: The string to inject as a ``user`` message body — the lane's
            XML payload (``<swarm-update>``, ``<background-command>``, …).
        mode: REQUIRED lane tag identifying the producer / delivery owner
            (``'swarm-update'`` / ``'background-command'`` / ``'user-steer'``
            / ``'peer-msg'``). The bucket is shared, so every item must
            carry its lane: the scoped filters (``drain(modes=…)``,
            ``clear(modes=…)``, ``has_pending``/``pending_items``) key off
            it, and the orchestrator may render it as a UI chip.
        priority: ``'now' | 'next' | 'later'`` (default ``'later'``).
        agent_id: Optional originating agent id for diagnostics.
        extra: Optional dict merged into the item for downstream consumers.
    """
    if not task_id:
        logger.warning('[Inbox] enqueue with empty task_id, dropping')
        return False
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
        # Refuse to (re-)create an inbox for a key whose task has already
        # ended. Late-arriving producers (e.g. a sub-agent that finishes
        # after its parent task closed) would otherwise leak memory —
        # nobody will drain the bucket; the orchestrator has stopped looking.
        if task_id in _tombstones:
            logger.debug(
                '[Inbox:%s] enqueue (mode=%s agent=%s) dropped — task ended',
                task_id, mode, agent_id or '?')
            return False
        if str(item.get('queueId') or '') in _consumed_queue_ids.get(task_id, ()):
            return False
        queue_id = item.get('queueId')
        if queue_id and any(existing.get('queueId') == queue_id
                            for existing in _inboxes.get(task_id, ())):
            return False
        _append_item_locked(task_id, item)

    logger.debug('[Inbox:%s] enqueued mode=%s priority=%s agent=%s len=%d (depth=%d)',
                 task_id, mode, priority, agent_id or '?', len(value),
                 len(_inboxes.get(task_id, [])))
    return True


def enqueue_after_durable_commit(
    task_id: str,
    value: str,
    commit: Callable[[], Any],
    *,
    mode: str,
    priority: str = 'later',
    agent_id: str = '',
    extra: dict | None = None,
) -> tuple[bool, Any]:
    """Commit durable authority, then expose its wakeup item to the worker.

    The inbox lifecycle lock prevents a finalizer from tombstoning the slot
    between the drainability decision and insertion. The callback runs before
    the item is visible, which gives operator steer its durable-before-wakeup
    ordering without teaching this process-local queue about storage.
    """
    if not task_id:
        return False, None
    normalized_priority = priority if priority in _PRIORITY else 'later'
    item: dict[str, Any] = {
        'value': value,
        'priority': normalized_priority,
        'mode': mode,
        'agent_id': agent_id,
        'enqueued': time.time(),
    }
    if extra:
        item.update(extra)
    with _lock:
        if task_id in _tombstones:
            return False, None
        if str(item.get('queueId') or '') in _consumed_queue_ids.get(task_id, ()):
            return False, None
        committed = commit()
        _append_item_locked(task_id, item)
    return True, committed


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

    IDs are fenced even when their inbox twin has not been published yet.
    Every consumed identity survives clear and revival until process exit.
    Unique durable IDs and the existing conversation-key owner
    boundary apply. This process-local fence is not durable delivery history.
    Items whose ``queueId`` is not in *queue_ids* (and any swarm items, which
    carry no ``queueId``) are left untouched. Returns the number removed.
    """
    if not task_id or not queue_ids:
        return 0
    ids = {str(q) for q in queue_ids if q}
    if not ids:
        return 0
    with _lock:
        # Dispatch can win before the producer publishes its twin. Record even
        # an absent item; clear/untombstone must not reopen that publication.
        consumed = _consumed_queue_ids.get(task_id)
        if consumed is None:
            # ids is a fresh owned set; transfer it instead of copying it.
            _consumed_queue_ids[task_id] = ids
        else:
            consumed.update(ids)
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


def has_pending(task_id: str, *, modes: list[str] | None = None) -> bool:
    """Return whether the inbox has an item matching ``modes``.

    The unfiltered form preserves the original cheap depth probe. A filtered
    probe lets synchronous work notice an operator steer without consuming it;
    the round-boundary injector remains the sole delivery owner.
    """
    if not task_id:
        return False
    if modes is None:
        return peek(task_id) > 0
    wanted = set(modes)
    with _lock:
        return any(
            item.get('mode', '') in wanted
            for item in _inboxes.get(task_id, ())
        )


def pending_items(task_id: str, *,
                  modes: list[str] | None = None) -> list[dict[str, Any]]:
    """Read-only snapshot (shallow copies) of pending items for policy gates.

    Swarm auto-continuation inspects per-agent outcomes WITHOUT consuming
    delivery — the round-boundary injector remains the sole owner of
    ``drain``. Items carry their originator's ``extra`` fields (e.g. the
    sub-agent terminal ``status`` stamped by the swarm master).
    """
    if not task_id:
        return []
    wanted = set(modes) if modes is not None else None
    with _lock:
        return [
            dict(item)
            for item in _inboxes.get(task_id, ())
            if wanted is None or item.get('mode', '') in wanted
        ]


def is_tombstoned(task_id: str) -> bool:
    """Return True if *task_id*'s inbox slot has been tombstoned (task ended).

    A tombstoned slot silently drops any :func:`enqueue` — the orchestrator has
    stopped draining it. The human-steer send path probes this to decide
    whether a mid-turn steer can still be injected into the live turn, or must
    instead fall back to the durable ``message_queue`` as a fresh next turn
    (the exactly-once "never-zero" guarantee).
    """
    if not task_id:
        return False
    with _lock:
        return task_id in _tombstones


def clear(task_id: str, *, modes: list[str] | None = None) -> int:
    """Discard queued items for *task_id* AND tombstone the slot.

    Call this when the main task ends so:
      * any unread items are dropped now,
      * any LATE sub-agent completion (one that races the task end) is
        also dropped instead of being silently kept around forever.

    ``modes=None`` (default) drops every unread item — the task-end form.
    ``modes=[...]`` drops only items whose ``mode`` is in the list; other
    lanes' items survive. Swarm lifecycle teardown (session recycle, TTL
    reaper, capacity eviction) uses the filtered form because the
    conversation-keyed inbox is SHARED with lanes the swarm does not own
    (``background-command`` / ``user-steer`` / ``peer-msg``) — wiping those
    mid-turn destroys their fast path and leaves their durable rows waiting
    for settlement instead of the next round-boundary drain.

    The slot is tombstoned in both forms so late sub-agent completions can
    never recreate the bucket. Non-swarm lanes tolerate the brief tombstone
    window: their producers probe ``is_tombstoned`` / treat a refused
    enqueue as harmless and fall back to the durable row.

    Returns the number of items discarded.
    """
    if not task_id:
        return 0
    with _lock:
        if modes is None:
            bucket = _inboxes.pop(task_id, None)
            n = len(bucket) if bucket else 0
        else:
            bucket = _inboxes.get(task_id)
            if not bucket:
                n = 0
            else:
                wanted = set(modes)
                kept = [it for it in bucket if it.get('mode', '') not in wanted]
                n = len(bucket) - len(kept)
                if kept:
                    _inboxes[task_id] = kept
                else:
                    _inboxes.pop(task_id, None)
        # Ended slots remain closed until explicit revival; other traffic
        # cannot revoke their late-producer protection.
        _tombstones.add(task_id)
    if n:
        logger.info('[Inbox:%s] cleared %d unread item(s) (modes=%s)',
                    task_id, n, modes if modes is not None else 'ALL')
    return n


def untombstone(key: str) -> None:
    """Remove *key* from the tombstone set so its inbox can be re-created.

    Called when a new live turn (a fresh ``spawn_agents`` wave, or a revived
    conversation) starts on a key whose slot was tombstoned by a prior
    ``clear``. Without this, the new owner's ``enqueue`` calls would be
    silently dropped because the slot is still tombstoned.
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
            _consumed_queue_ids.pop(task_id, None)
        else:
            _inboxes.clear()
            _tombstones.clear()
            _consumed_queue_ids.clear()


def stats() -> dict[str, int]:
    """Return ``{task_id: queue_depth}`` snapshot for diagnostics."""
    with _lock:
        return {tid: len(items) for tid, items in _inboxes.items()}
