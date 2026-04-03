"""
lib.tasks_pkg — Task processing system (decomposed from monolithic tasks.py).

Modules:
  approval      — Write approval system (request/resolve)
  compaction    — Two-layer context compression pipeline
  manager       — Task CRUD, events, persistence, streaming
  executor      — Tool execution dispatch + summary generation
  orchestrator  — run_task main loop
  endpoint      — Fully autonomous work→review→revise loop (endpoint mode)
"""

__all__ = [
    # manager (eager)
    'tasks', 'tasks_lock',
    'create_task', 'append_event', 'persist_task_result', 'cleanup_old_tasks',
    'stream_llm_response',
    # approval (lazy)
    'request_write_approval', 'resolve_write_approval',
    # human guidance (lazy)
    'request_human_guidance', 'resolve_human_guidance', 'cancel_human_guidance',
    # stdin handler (lazy)
    'request_stdin', 'resolve_stdin', 'cancel_stdin',
    # compaction (lazy)
    'run_compaction_pipeline',
    'micro_compact', 'smart_summary_compact', 'cleanup_compaction_data',
    'force_compact_if_needed',
    # executor (lazy)
    '_generate_tool_summary', '_execute_tool_one', 'ToolRegistry', 'tool_registry',
    # orchestrator (lazy)
    'run_task', '_build_search_addendum', '_run_single_turn',
    # endpoint (lazy)
    'run_endpoint_task',
    'run_task_sync',
]

# ── Eagerly import only the lightweight manager (used by routes at import time) ──
from lib.tasks_pkg.manager import (
    append_event,
    cleanup_old_tasks,
    create_task,
    persist_task_result,
    stream_llm_response,
    tasks,
    tasks_lock,
)

# ── Lazy imports for heavy modules (orchestrator, executor, endpoint, etc.) ──
# These are only loaded when first accessed, saving ~600ms at startup.

_LAZY_MAP = {
    # approval
    'request_write_approval': ('lib.tasks_pkg.approval', 'request_write_approval'),
    'resolve_write_approval': ('lib.tasks_pkg.approval', 'resolve_write_approval'),
    # human guidance
    'request_human_guidance': ('lib.tasks_pkg.human_guidance', 'request_human_guidance'),
    'resolve_human_guidance': ('lib.tasks_pkg.human_guidance', 'resolve_human_guidance'),
    'cancel_human_guidance':  ('lib.tasks_pkg.human_guidance', 'cancel_human_guidance'),
    # stdin handler
    'request_stdin':  ('lib.tasks_pkg.stdin_handler', 'request_stdin'),
    'resolve_stdin':  ('lib.tasks_pkg.stdin_handler', 'resolve_stdin'),
    'cancel_stdin':   ('lib.tasks_pkg.stdin_handler', 'cancel_stdin'),
    # compaction
    'run_compaction_pipeline':   ('lib.tasks_pkg.compaction', 'run_compaction_pipeline'),
    'micro_compact':             ('lib.tasks_pkg.compaction', 'micro_compact'),
    'smart_summary_compact':     ('lib.tasks_pkg.compaction', 'smart_summary_compact'),
    'cleanup_compaction_data':   ('lib.tasks_pkg.compaction', 'cleanup_compaction_data'),

    'force_compact_if_needed':   ('lib.tasks_pkg.compaction', 'force_compact_if_needed'),
    # executor
    '_generate_tool_summary': ('lib.tasks_pkg.executor', '_generate_tool_summary'),
    '_execute_tool_one':      ('lib.tasks_pkg.executor', '_execute_tool_one'),
    'ToolRegistry':           ('lib.tasks_pkg.executor', 'ToolRegistry'),
    'tool_registry':          ('lib.tasks_pkg.executor', 'tool_registry'),
    # orchestrator
    'run_task':              ('lib.tasks_pkg.orchestrator', 'run_task'),
    '_build_search_addendum':('lib.tasks_pkg.model_config', '_build_search_addendum'),
    '_run_single_turn':      ('lib.tasks_pkg.orchestrator', '_run_single_turn'),
    # endpoint
    'run_endpoint_task':     ('lib.tasks_pkg.endpoint', 'run_endpoint_task'),
    'run_task_sync':         ('lib.tasks_pkg.endpoint', 'run_task_sync'),
}

def __getattr__(name):
    if name in _LAZY_MAP:
        module_path, attr_name = _LAZY_MAP[name]
        import importlib
        mod = importlib.import_module(module_path)
        val = getattr(mod, attr_name)
        # Cache in module namespace so __getattr__ is only called once
        globals()[name] = val
        return val
    raise AttributeError(f"module 'lib.tasks_pkg' has no attribute {name!r}")
