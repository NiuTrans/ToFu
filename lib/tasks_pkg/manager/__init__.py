"""Task lifecycle management — creation, events, persistence, cleanup, streaming.

★ Migrated to ``lib.task_runtime.TaskRuntime`` 2026-05-22 (last of the
five legacy registries). The module-level ``tasks`` / ``tasks_lock``
names remain exported because 47 import sites across routes/, lib/, and
tests/ reference them directly. They now alias the runtime's internal
storage. All custom behaviour (phase tracking, persistent event log,
freshness-guard `_conv_latest_task` index, content_lock, etc.) is
preserved on top of the runtime by augmenting the task dict after
``runtime.create()``.

────────────────────────────────────────────────────────────────────────
This module is a FACADE PACKAGE. The implementation was split out of the
former single-file ``lib/tasks_pkg/manager.py`` into cohesive sub-modules,
but the import path is UNCHANGED — every ``from lib.tasks_pkg.manager import
X`` call site keeps working byte-identically.

Sub-modules:
  * ``._state``       — shared singletons: the backing TaskRuntime, the
                        ``tasks`` / ``tasks_lock`` ALIASES, the conv→latest-task
                        freshness index, and its accessors. SINGLE HOME.
  * ``._events``      — ``append_event`` (monkeypatched by many tests) +
                        stable per-message id helpers.
  * ``._persist``     — result-meta build, sanitizers, tool-round merge/trim,
                        ``_upsert_task_row``, heavy-state release,
                        ``persist_task_result``, conversation recovery readers.
  * ``._sync``        — conversation sync (result + partial), settle-time
                        reconcile, and post-terminal fan-out helpers.
  * ``._registry``    — create / discard / list / abort / quiesce lifecycle.
  * ``._recovery``    — startup stale-task recovery + deferred boot dispatch.
  * ``._maintenance`` — TTL cleanup, memory shed, stuck-task reaper.
  * ``._stream``      — ``stream_llm_response`` + ``_display_model_name``.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Shared singletons — ``tasks`` / ``tasks_lock`` alias _chat_runtime internals
#  (MUST be re-exported so ``from lib.tasks_pkg.manager import tasks`` returns
#  THE SAME object 47 call sites already hold). ``_chat_runtime`` +
#  CHECKPOINT_MIN_DELTA_CHARS + the conv-latest-task index/accessors too.
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.manager._state import (  # noqa: E402,F401
    _chat_runtime,
    tasks,
    tasks_lock,
    _conv_latest_task,
    _conv_latest_task_lock,
    _LATEST_KIND,
    _LATEST_TTL,
    CHECKPOINT_MIN_DELTA_CHARS,
    _record_latest_task,
    _latest_task_for_conv,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Auto-translate re-exports (were imported at the top of the old manager.py;
#  other code imports these names via ``lib.tasks_pkg.manager``).
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.auto_translate import (  # noqa: E402,F401
    _maybe_auto_translate_assistant,
    _maybe_auto_translate_critic,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Events + message-id helpers
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.manager._events import (  # noqa: E402,F401
    append_event,
    find_message_by_id,
    _assign_message_ids,
    _new_assistant_slot,
    _strip_base64_for_snapshot,
)

# The pre-split ``manager.py`` imported these at module level (``from
# lib.agent_core.events import EventType, build_event``), so call sites and
# tests reach them via ``lib.tasks_pkg.manager`` (e.g.
# ``mgr.build_event(mgr.EventType.DELTA, ...)``). Re-export to preserve that
# facade — dropping them in the split was silent drift.
from lib.agent_core.events import EventType, build_event, emit  # noqa: E402,F401


# ═══════════════════════════════════════════════════════════════════════════
#  Persistence (result-meta build, sanitizers, tool-round merge, upsert,
#  heavy-state release, persist_task_result, conversation recovery readers)
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.manager._persist import (  # noqa: E402,F401
    build_result_meta,
    persist_task_result,
    load_tool_rounds_from_conversation,
    load_endpoint_turns_from_conversation,
    _tool_rounds_have_dedicated_home,
    _sanitize_usage_for_persist,
    _sanitize_api_rounds_for_persist,
    _trim_round_for_persist,
    _merge_tool_rounds,
    _conv_row_exists,
    _upsert_task_row,
    _release_heavy_task_state,
    _TASK_RESULTS_COLS,
    _USAGE_TRANSIENT_KEYS,
    _HEAVY_TERMINAL_FIELDS,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Conversation sync + settle reconcile + post-terminal fan-out
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.manager._sync import (  # noqa: E402,F401
    checkpoint_task_partial,
    _sync_result_to_conversation,
    _sync_partial_to_conversation,
    _reconcile_orphan_placeholder_on_settle,
    _maybe_refresh_project_summary,
    _update_proactive_execution_status,
    _dispatch_queued_message,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Registry lifecycle
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.manager._registry import (  # noqa: E402,F401
    create_task,
    discard_task,
    list_running_tasks,
    abort_running_tasks_for_conv,
    quiesce_running_tasks,
    _write_aborted_terminal_floor,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Startup recovery + deferred boot dispatch
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.manager._recovery import (  # noqa: E402,F401
    recover_stale_tasks_on_startup,
    run_deferred_boot_dispatch,
    _boot_auto_dispatch_enabled,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Maintenance — cleanup, memory shed, stuck-task reaper
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.manager._maintenance import (  # noqa: E402,F401
    cleanup_old_tasks,
    shed_memory_under_pressure,
    reap_stuck_running_tasks,
    _malloc_trim,
    _stuck_task_max_silent_secs,
    _write_stuck_terminal_floor,
    _finalize_reaped_stuck_task,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Streaming
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.manager._stream import (  # noqa: E402,F401
    stream_llm_response,
    _display_model_name,
    _GATEWAY_PREFIXES,
    _STREAM_CHECKPOINT_INTERVAL,
    dispatch_stream,
)


__all__ = [
    # shared state
    'tasks', 'tasks_lock',
    # registry
    'create_task', 'discard_task', 'list_running_tasks',
    'abort_running_tasks_for_conv', 'quiesce_running_tasks',
    # events
    'append_event', 'find_message_by_id',
    'build_event', 'EventType', 'emit',
    # persistence
    'build_result_meta', 'persist_task_result',
    'load_tool_rounds_from_conversation', 'load_endpoint_turns_from_conversation',
    # sync
    'checkpoint_task_partial',
    # recovery
    'recover_stale_tasks_on_startup', 'run_deferred_boot_dispatch',
    # maintenance
    'cleanup_old_tasks', 'shed_memory_under_pressure', 'reap_stuck_running_tasks',
    # streaming
    'stream_llm_response',
]
