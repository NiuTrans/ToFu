"""Durable task checkpoints and scheduler terminal bookkeeping.

Conversation transcripts are not synchronized here.  A conversation-backed
executor is bound to one durable attempt, and ``manager._events.append_event``
records every visible update through ``turn.event.record`` before publishing
it.  Keeping a second messages-array writer in the task manager previously
created the stale-tail, placeholder, translation-regraft, and whole-document
CAS failure families.

Entry points:
  * ``checkpoint_task_partial`` persists executor recovery state.
  * ``_update_proactive_execution_status`` settles scheduler bookkeeping.

Dependencies are deliberately one-way: this module may persist task rows and
call the scheduler repository, but it never reads or writes a conversation.
"""

from __future__ import annotations

import json
from datetime import datetime

from lib.conversation_sync.attempt_identity import is_conversation_attempt
from lib.error_envelope import to_json as _error_to_json
from lib.log import get_logger
from lib.tasks_pkg.manager._events import snapshot_task_text
from lib.tasks_pkg.manager._persist import (
    _merge_tool_rounds,
    _task_result_segments_json,
    _tool_rounds_have_dedicated_home,
    _upsert_task_row,
    terminal_state_log_summary,
)
from lib.tasks_pkg.manager.runtime import chat_task_runtime


logger = get_logger(__name__)


def _update_proactive_execution_status(task: dict) -> None:
    """Settle the scheduler record that launched ``task``.

    This callback is owner-scoped and updates a record only when its current
    ``last_execution_task_id`` still names this executor.  A later execution
    therefore cannot be overwritten by a delayed terminal callback.
    """
    task_id = str(task.get("id") or "")
    if not task_id:
        return
    try:
        from lib.scheduler.manager import get_scheduler
        from lib.tasks_pkg.manager._registry import task_user_id

        owner_user_id = int(task_user_id(task))
        scheduler = get_scheduler()
        scheduled_tasks = scheduler.list_tasks(
            user_id=owner_user_id, include_disabled=True
        )
        matching = [
            item
            for item in scheduled_tasks
            if item.get("task_type") == "agent"
            and item.get("last_execution_task_id") == task_id
        ]
        if not matching:
            return
        execution_status = (
            "ok"
            if task.get("status") == "done" and not task.get("error")
            else "error"
        )
        now = datetime.now().isoformat()
        for scheduled_task in matching:
            scheduler.update_task(
                scheduled_task["id"],
                user_id=owner_user_id,
                last_execution_status=execution_status,
                updated_at=now,
            )
    except Exception:
        logger.warning(
            "[Scheduler] Failed to settle execution task=%s",
            task_id[:8],
            exc_info=True,
        )


def _has_inflight_round(task: dict) -> bool:
    """Return whether a tool round is still live and needs a checkpoint."""
    rounds = (task.get("_checkpointToolRounds") or []) + (
        task.get("toolRounds") or []
    )
    live_statuses = {
        "searching",
        "executing",
        "pending_approval",
        "awaiting_human",
        "awaiting_stdin",
    }
    return any(
        isinstance(round_record, dict)
        and round_record.get("status") in live_statuses
        for round_record in rounds
    )


def checkpoint_task_partial(task: dict, force: bool = False):
    """Persist executor recovery state without mutating the transcript.

    The turn event bridge owns the visible projection.  This checkpoint is a
    separate execution-recovery record used by polling and crash diagnostics.
    ``force`` preserves tool-only work before any prose has been produced.
    """
    content, thinking, content_epoch = snapshot_task_text(task)
    if (
        not content
        and not thinking
        and not force
        and not _has_inflight_round(task)
    ):
        return None

    task_id = str(task.get("id") or "")
    conversation_id = str(task.get("convId") or "")
    merged_tool_rounds = _merge_tool_rounds(task)

    try:
        from lib.tasks_pkg.segments import assemble_segments

        task["segments"] = assemble_segments(task, merged=merged_tool_rounds)
    except Exception:
        logger.warning(
            "[Checkpoint %s] Segment assembly failed",
            task_id[:8],
            exc_info=True,
        )

    checkpoint_owned = True
    try:
        tool_rounds_json = (
            None
            if _tool_rounds_have_dedicated_home(task)
            else json.dumps(merged_tool_rounds, ensure_ascii=False)
        )
        metadata = {"contentEpoch": content_epoch}
        if is_conversation_attempt(task):
            metadata.update(
                {
                    "turnId": task.get("_turnId") or "",
                    "attemptId": task.get("_attemptId") or "",
                }
            )
        for key in ("model", "preset", "thinkingDepth"):
            if task.get(key):
                metadata[key] = task[key]
        if task.get("_todoState"):
            from lib.tools.todo import public_todo_state

            metadata["todoState"] = public_todo_state(task["_todoState"])

        segments_json = _task_result_segments_json(task)
        error_json = (
            _error_to_json(task["error"])
            if task.get("error") is not None
            else None
        )
        checkpoint_owned = _upsert_task_row(
            task,
            conversation_id,
            content=content,
            thinking=thinking,
            status="running",
            error_json=error_json,
            tr_json=tool_rounds_json,
            meta_json=json.dumps(metadata, ensure_ascii=False),
            segments_json=segments_json,
        )
        logger.debug(
            "[Checkpoint %s] Saved executor state: content=%d thinking=%d",
            task_id[:8],
            len(content),
            len(thinking),
        )
    except Exception:
        logger.warning(
            "[Checkpoint %s] Failed to persist executor state",
            task_id[:8],
            exc_info=True,
        )
        if task.get("finishReason"):
            logger.warning(
                "[Checkpoint %s] Terminal metadata was not persisted: %s",
                task_id[:8],
                terminal_state_log_summary(task, persisted=False),
            )

    if checkpoint_owned is False:
        logger.warning(
            "[Checkpoint %s] Rejected by the recovery/terminal ownership fence",
            task_id[:8],
        )
        return False

    concurrent_tasks = [
        (str(other.get("id") or "")[:8], str(other.get("convId") or "")[:8])
        for other in chat_task_runtime.snapshot()
        if other.get("status") in ("pending", "running")
        and other.get("id") != task_id
    ]
    if concurrent_tasks:
        logger.debug(
            "[Checkpoint %s] %d other executor(s) are live "
            "(pending/running): %s",
            task_id[:8],
            len(concurrent_tasks),
            concurrent_tasks,
        )
    return True


__all__ = ["checkpoint_task_partial", "_update_proactive_execution_status"]
