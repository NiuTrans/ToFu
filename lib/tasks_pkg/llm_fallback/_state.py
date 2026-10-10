"""Shared reactive-compaction retry state for the LLM-fallback package.

The ``_reactive_compact_attempts`` dict is the SINGLE process-wide object
that tracks how many reactive-compaction retries a task has consumed. It
MUST be imported by reference everywhere (never re-assigned) so that
``cleanup_reactive_compact_state`` mutates the same object a test or the
orchestrator holds a reference to.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# Track reactive compact attempts per task to avoid infinite loops
_reactive_compact_attempts: dict[str, int] = {}
_REACTIVE_COMPACT_MAX_RETRIES = 2


def cleanup_reactive_compact_state(task_id: str):
    """Remove reactive compact tracking for a finished task.

    Called from orchestrator._finalize_and_emit_done to prevent memory leak.
    """
    _reactive_compact_attempts.pop(task_id, None)

