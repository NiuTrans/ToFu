"""Task open — the run_task preamble's kick / snapshot / open-log cluster.

Extracted 2026-08-01 ( slice 35) from ``run_task``'s preamble.
The id-shape check + ``set_req_id`` stay in the spine (prologue); these
three helpers cover everything between that and the try body.
"""

from __future__ import annotations

import time

from lib.log import get_logger

logger = get_logger(__name__)


def check_autopilot_kick(task) -> bool:
    """Settle retired carriers without invoking a model or scheduling a successor."""
    if not task.get('_autopilot_kick'):
        return False
    from lib.tasks_pkg.manager import persist_task_result, append_event
    task['status'] = 'error'
    from lib.error_envelope import make_envelope
    task['error'] = make_envelope(
        'bad_request',
        message='Legacy Autopilot is retired; start Goal mode through its '
                'current admission API.',
        retryable=False)
    task['_abort_reason'] = 'legacy_autopilot_retired'
    persist_task_result(task)
    append_event(task, {'type': 'done', 'finishReason': 'error',
                        'error': {'message': task['error']['message'], 'code': 'legacy_autopilot_retired'}})
    return True


def snapshot_turn_input(task) -> None:
    """Capture the pristine turn-input snapshot for turn-level auto-retry.

    run_task mutates a LOCAL copy of messages (system-context injection,
    tool-history rebuild, completed tool rounds) and writes it back to
    ``task['messages']`` on exit — so on a transient-error re-run we must
    restore the ORIGINAL input first, or the re-run would double-inject
    system blocks and replay a half-finished round. Captured ONCE and
    preserved across every retry attempt (see _maybe_auto_retry_turn).
    Skipped for Flow-managed tasks (the Flow executor owns its turn boundary).
    """
    # ATIF must not confuse historical input tools with this run's receipts.
    # The post-loop owner flips this only after writing its working transcript.
    task['_messages_include_current_run'] = False
    if not task.get('_flow_managed') and '_turn_input_messages' not in task:
        task['_turn_input_messages'] = list(task.get('messages') or [])


def log_task_open(task, tid) -> float:
    """Emit the queue-wait timing line + the ▶ START bracket.

    queue_wait compares ``_t_created`` (set in create_task) against the
    moment a thread picked the task up — thread-pool / queue latency.
    The START bracket logs the FULL task id (not the 8-char prefix) so
    a user can copy the id from the cost popover and grep the whole
    turn's lifecycle; it pairs with the '[Task:%s] ■ DONE' summary.

    Returns:
        ``_t_run_start`` — the caller's later ``_t_prep_done`` anchor.
    """
    _t_run_start = time.time()
    _t_created = task.get('_t_created')
    if _t_created:
        _queue_wait = max(0.0, _t_run_start - _t_created)
        logger.info('[Timing:%s] queue_wait=%.3fs (create→run_task)',
                    tid, _queue_wait)
        try:
            from lib.observability import record_task_queue_wait
            record_task_queue_wait(task.get('kind') or 'chat', _queue_wait)
        except Exception as exc:
            logger.debug('[Timing:%s] queue-wait metric skipped: %s', tid, exc)
    logger.info('[Task:%s] ▶ START conv=%s msgs=%d',
                task['id'], task.get('convId', '') or '-',
                len(task.get('messages') or []))
    return _t_run_start
