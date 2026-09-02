"""Public chat-task lifecycle API.

Concrete modules own runtime state, events, persistence, terminal policy,
recovery, maintenance, and streaming.  This package exposes only the stable
service operations used across domains; implementation helpers must be
imported from their concrete owner.  Imports are explicit so static analysis
and language models can resolve every symbol without PEP 562 indirection.
"""

from lib.tasks_pkg.manager._events import (
    append_event,
    find_message_by_id,
    reset_task_text,
    snapshot_task_text,
)
from lib.tasks_pkg.manager._maintenance import (
    cleanup_old_tasks,
    reap_stuck_running_tasks,
    shed_memory_under_pressure,
)
from lib.tasks_pkg.manager._persist import build_result_meta, persist_task_result
from lib.tasks_pkg.manager._recovery import recover_stale_tasks_on_startup
from lib.tasks_pkg.manager._registry import (
    abort_running_tasks_for_conv,
    create_task,
    discard_task,
    has_abort_tombstone,
    is_carrier_task,
    list_running_tasks,
    make_task_abort_check,
    notify_terminal_conversation_change,
    plant_abort_tombstone,
    plant_abort_tombstones_for_conv,
    quiesce_running_tasks,
    task_user_id,
    write_carrier_terminal_row,
)
from lib.tasks_pkg.manager._sync import checkpoint_task_partial
from lib.tasks_pkg.manager._terminal import (
    finalize_chat_task_error,
    stamp_chat_task_terminal,
)
from lib.tasks_pkg.manager.runtime import chat_task_runtime


def stream_llm_response(task, body, tag='', on_tool_call_ready=None,
                        *, pool_wide=False, exclude_models=None):
    """Load model transport only when a task actually starts streaming."""
    from lib.tasks_pkg.manager._stream import (
        stream_llm_response as implementation,
    )

    return implementation(
        task,
        body,
        tag=tag,
        on_tool_call_ready=on_tool_call_ready,
        pool_wide=pool_wide,
        exclude_models=exclude_models,
    )


__all__ = [
    'abort_running_tasks_for_conv',
    'append_event',
    'build_result_meta',
    'chat_task_runtime',
    'checkpoint_task_partial',
    'cleanup_old_tasks',
    'create_task',
    'discard_task',
    'finalize_chat_task_error',
    'find_message_by_id',
    'has_abort_tombstone',
    'is_carrier_task',
    'list_running_tasks',
    'make_task_abort_check',
    'notify_terminal_conversation_change',
    'persist_task_result',
    'plant_abort_tombstone',
    'plant_abort_tombstones_for_conv',
    'quiesce_running_tasks',
    'reap_stuck_running_tasks',
    'recover_stale_tasks_on_startup',
    'reset_task_text',
    'shed_memory_under_pressure',
    'snapshot_task_text',
    'stamp_chat_task_terminal',
    'stream_llm_response',
    'task_user_id',
    'write_carrier_terminal_row',
]
