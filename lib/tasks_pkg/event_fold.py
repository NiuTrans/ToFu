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

The elegant root fix: the ``task_events`` table ALREADY persists every delta on
arrival (``event_log.append_persistent_event`` — no coalescing since 2026-05),
so the server holds a LOSSLESS record of exactly what the client saw. Folding
that log reconstructs the authoritative live text with NO added write cost (the
per-delta write is already paid) and only a single bounded read per cold
reconnect (benchmarked at <=5ms for typical turns, off the event loop). Once the
cold state snapshot is folded, the server's replayable state never trails the
client → the keep-longer belt becomes a provable no-op for cold replay.

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


def fold_cold_state_text(task_id, checkpoint_content='', checkpoint_thinking=''):
    """Return the authoritative ``(content, thinking)`` for a COLD state
    snapshot: the longer of the folded event log and the 5s checkpoint.

    The event log is the primary source (lossless per-delta). The checkpoint is
    the fallback for the ONE residual case the log cannot cover — a best-effort
    per-delta persist that failed (a transient DB blip; logged at WARNING in
    ``append_persistent_event``). Taking the longer of the two on the SERVER is
    the root-side equivalent of the frontend keep-longer belt: it moves the
    "never shrink an in-flight field" invariant to where the authoritative
    record lives, so the client can project the state snapshot VERBATIM.

    Best-effort: never raises — on any failure it returns the checkpoint pair
    unchanged, so a fold problem can never blank a bubble worse than today.
    """
    try:
        from lib.tasks_pkg.event_log import read_events
        events = read_events(task_id, since_event_id=None)
        folded_c, folded_t = fold_text_from_events(events)
    except Exception as e:
        logger.warning('[EventFold] fold failed for task=%s: %s — using checkpoint',
                       (task_id or '')[:8], e)
        return checkpoint_content or '', checkpoint_thinking or ''
    content = folded_c if len(folded_c) >= len(checkpoint_content or '') else checkpoint_content
    thinking = folded_t if len(folded_t) >= len(checkpoint_thinking or '') else checkpoint_thinking
    return content or '', thinking or ''
