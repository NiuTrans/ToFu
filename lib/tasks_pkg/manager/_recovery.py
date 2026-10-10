"""Restart settlement for durable task and turn state.

Conversation projections belong exclusively to the turn lifecycle. Task
results are a separate inspection/replay read model. Recovery settles both
authorities without copying task snapshots back into a transcript and never
starts billable work.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)


def _interruption_reason(previous_shutdown: Any) -> str:
    if not isinstance(previous_shutdown, dict):
        return "server_restart"
    verdict = str(previous_shutdown.get("verdict") or "")
    if verdict == "unclean":
        return "process_killed"
    if verdict == "clean":
        return "manual_restart"
    return "server_restart"


def _has_orphaned_recovery_work(client: Any) -> bool:
    """Cheap pre-pass: is there ANY pending/running turn or task snapshot?

    The two recovery scans below walk unindexed status columns
    (``storage_generation_attempts.status`` / the ``task_results`` keyspace)
    even when the answer is zero — on a large or network-resident authority
    that full scan overruns the writer's transaction watchdog, so EVERY boot
    with no orphans used to pay a ~5s interrupt plus the boot-recovery retry.
    A LIMIT-1 existence probe returns in milliseconds and short-circuits the
    entire settle when (the overwhelmingly common case) nothing is orphaned.
    """
    try:
        row = client.query("task_results.recovery_pending", {}, deadline=10.0)
    except Exception as exc:
        # Assume there MAY be work so the authoritative (retrying) settle path
        # still runs — never skip a real orphan on a probe hiccup.
        logger.warning(
            "[Startup] recovery pre-pass probe failed; running full settle: %s", exc)
        return True
    return bool((row or {}).get("pending"))


def recover_stale_tasks_on_startup(prev_shutdown: Any = None) -> dict[str, Any]:
    """Settle orphaned turns and task snapshots; never redispatch work."""
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import recover_running_attempts

    client = get_storage_client(write=True)
    if not _has_orphaned_recovery_work(client):
        return {
            "recoveredAttemptCount": 0,
            "recoveredTaskCount": 0,
            "conversationIds": [],
            "interruptedReason": _interruption_reason(prev_shutdown),
        }
    recovered_attempts = recover_running_attempts()
    reason = _interruption_reason(prev_shutdown)
    recovered_tasks = 0
    conversation_ids: list[str] = []
    seen_conversations: set[str] = set()
    cursor = ""

    # Each storage command is a bounded transaction. The hard round cap is a
    # corruption guard, not a normal pagination limit.
    for _round in range(256):
        payload = {"interrupted_reason": reason}
        if cursor:
            payload["after_key"] = cursor
        result = client.command(
            "task_results.recover_running",
            payload,
            None,
            deadline=30.0,
        )
        rows = list((result or {}).get("recovered") or [])
        recovered_tasks += len(rows)
        for row in rows:
            conversation_id = str(row.get("conversationId") or "")
            if conversation_id and conversation_id not in seen_conversations:
                seen_conversations.add(conversation_id)
                conversation_ids.append(conversation_id)
        if not (result or {}).get("remaining"):
            break
        next_cursor = str((result or {}).get("nextKey") or "")
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("Task-result recovery cursor did not advance")
        cursor = next_cursor
    else:
        raise RuntimeError("Task-result recovery exceeded its bounded round cap")

    if recovered_attempts or recovered_tasks:
        logger.warning(
            "[Startup] recovered %d orphaned attempt(s) and %d task snapshot(s)",
            recovered_attempts,
            recovered_tasks,
        )
    return {
        "recoveredAttemptCount": recovered_attempts,
        "recoveredTaskCount": recovered_tasks,
        "conversationIds": conversation_ids,
        "interruptedReason": reason,
    }


__all__ = ["recover_stale_tasks_on_startup"]
