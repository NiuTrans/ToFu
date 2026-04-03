"""lib/tools/compact.py — Context compaction constants.

The context_compact tool is NOT exposed to models — it is only used
internally by the orchestrator's force-compact mechanism.
This module is kept for backward compatibility but exports nothing
useful for tool registration.
"""

# Legacy — kept for any residual imports but not registered in any tool list.
# The orchestrator uses _COMPACT_TOOL_NAME from lib.tasks_pkg.compaction directly.

__all__: list[str] = []
