"""Fold the persisted per-delta event log into authoritative live state.

Why this exists
---------------
An SSE reconnect that lands COLD (the task was evicted from ``TaskRuntime`` by
``cleanup_old_tasks`` or a restart) and carries NO usable ``Last-Event-ID``
cursor is bootstrapped from a ``state`` snapshot. Historically that snapshot's
``content`` / ``thinking`` were read from the ``task_results`` row — which is
checkpointed only every ``_STREAM_CHECKPOINT_INTERVAL`` (5s) by
``checkpoint_task_partial``. A reconnect that lands BETWEEN two 5s ticks
therefore replayed a checkpoint SHORTER than the deltas the client already
rendered, blanking the in-progress bubble ("generating then GONE"). The
frontend masked this with a keep-longer belt (``_snapshotLonger`` at the 5
state-snapshot sites) — a legitimate transport merge, but one that keeps a
second source of truth alive in the client.

The elegant root fix: the ``task_events`` table persists every client-visible
delta before delivery. Provider microchunks may be losslessly merged before
sequence assignment (first chunk immediate, then <=100 ms / 256 characters),
but the durable log still contains exactly the bytes the client saw in exactly
the same event order. Folding that log reconstructs authoritative live text
with no additional write and only one bounded read per cold reconnect
(benchmarked at <=5ms for typical turns, off the event loop). Once folded, the
server's replayable state never trails the client, so the keep-longer belt is a
provable no-op for cold replay.

The fold mirrors the frontend's own accumulation semantics EXACTLY:
  * ``delta``       → append ``content`` / ``thinking`` deltas.
  * ``delta_reset`` → clear accumulated CONTENT+THINKING (inter-round narration
                      before a tool call was not the final answer). Mirrors
                      sse_pipeline.js DELTA_RESET handling.
  * ``retry_reset`` → clear accumulated content+thinking (a transient-error
                      turn is being re-run from scratch). Mirrors the frontend.
Tool rounds are NOT folded here — the caller already has an authoritative
``toolRounds`` list (from ``task_results.tool_rounds`` or the conversation);
this module reconstructs only the free-text the 5s checkpoint under-captured.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def fold_text_from_events(events):
    """Reconstruct ``(content, thinking)`` from an ordered event list.

    Args:
        events: list of ``{'event_id': int, 'payload': dict}`` (the shape
            ``event_log.read_events`` returns) OR a list of raw event dicts
            (each ``{'type': ..., 'content': ...}``). Both are accepted so the
            hot path (in-memory ``task['events']``) and the cold path
            (``read_events``) can share one fold.

    Returns:
        ``(content, thinking)`` — the accumulated assistant text and reasoning
        text, with ``delta_reset`` / ``retry_reset`` boundaries applied exactly
        as the frontend applies them.
    """
    content_parts = []
    thinking_parts = []
    for ev in events or []:
        payload = ev.get('payload', ev) if isinstance(ev, dict) else None
        if not isinstance(payload, dict):
            continue
        etype = payload.get('type')
        if etype == 'delta':
            c = payload.get('content')
            if c:
                content_parts.append(c)
            th = payload.get('thinking')
            if th:
                thinking_parts.append(th)
        elif etype in ('delta_reset', 'retry_reset'):
            # Inter-round narration (delta_reset) or a from-scratch re-run
            # (retry_reset): the frontend clears the live bubble's text here,
            # so the authoritative accumulation restarts too.
            content_parts.clear()
            thinking_parts.clear()
    return ''.join(content_parts), ''.join(thinking_parts)


def fold_cold_state(task_id, checkpoint_content='', checkpoint_thinking='',
                    checkpoint_epoch=0):
    """Return authoritative cold text plus its effective generation."""
    checkpoint_epoch = int(checkpoint_epoch or 0)
    event_limit = 10000
    try:
        from lib.tasks_pkg.event_log import read_events
        events = read_events(
            task_id, since_event_id=None, limit=event_limit)
        folded_c, folded_t = fold_text_from_events(events)
        event_epoch = 0
        event_ids = []
        for ev in events or []:
            payload = ev.get('payload', ev) if isinstance(ev, dict) else None
            if isinstance(payload, dict):
                event_epoch = max(
                    event_epoch, int(payload.get('contentEpoch') or 0))
            if isinstance(ev, dict) and ev.get('event_id') is not None:
                event_ids.append(int(ev['event_id']))
    except Exception as e:
        logger.warning('[EventFold] fold failed for task=%s: %s — using checkpoint',
                       (task_id or '')[:8], e)
        return (checkpoint_content or '', checkpoint_thinking or '',
                checkpoint_epoch)
    log_complete = (
        len(events or []) < event_limit
        and len(event_ids) == len(events or [])
        and event_ids == list(range(len(event_ids)))
    )
    if event_epoch > checkpoint_epoch:
        if log_complete:
            return folded_c or '', folded_t or '', event_epoch
        logger.warning(
            '[EventFold] newer content epoch ignored for task=%s because the '
            'event log is incomplete (checkpoint_epoch=%d event_epoch=%d '
            'rows=%d first=%s last=%s)',
            (task_id or '')[:8], checkpoint_epoch, event_epoch,
            len(events or []), event_ids[0] if event_ids else None,
            event_ids[-1] if event_ids else None)
        return (checkpoint_content or '', checkpoint_thinking or '',
                checkpoint_epoch)
    content = folded_c if len(folded_c) >= len(checkpoint_content or '') else checkpoint_content
    thinking = folded_t if len(folded_t) >= len(checkpoint_thinking or '') else checkpoint_thinking
    return content or '', thinking or '', max(checkpoint_epoch, event_epoch)


def fold_cold_state_text(task_id, checkpoint_content='', checkpoint_thinking='',
                         checkpoint_epoch=0):
    """Compatibility facade returning only ``(content, thinking)``."""
    content, thinking, _epoch = fold_cold_state(
        task_id, checkpoint_content, checkpoint_thinking, checkpoint_epoch)
    return content, thinking
