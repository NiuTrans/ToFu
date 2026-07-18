"""Async translation TaskRuntime singleton + shared-state aliases + TTL cleanup.

This is the SINGLE HOME for the translate TaskRuntime instance and its two
legacy compatibility aliases. Every other submodule (and every external
caller via the package facade) imports these names from here, so
``_translate_tasks`` / ``_translate_tasks_lock`` are guaranteed to be the
SAME objects backing the one ``TaskRuntime`` singleton — a divergent runtime
would strand in-flight translate tasks.
"""

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)


# ── Async translation tasks (survive page reload / tab switch) ──
_translate_runtime = TaskRuntime(
    'translate', ttl=1800,
    push_channel='translate',
    error_source='routes.translate',
)

# Compatibility shims for legacy code paths:
#   _translate_tasks      → registry-as-dict (read-only access for ID lookups)
#   _translate_tasks_lock → kept as a per-task multi-write lock (use task['events_lock']
#                            for new code; this name exists only for diff minimisation)
_translate_tasks_lock = _translate_runtime._lock      # type: ignore[attr-defined]
_translate_tasks = _translate_runtime._tasks          # type: ignore[attr-defined]


def _cleanup_translate_tasks():
    """Remove expired translation tasks (delegates to TaskRuntime)."""
    n = _translate_runtime.cleanup_stale()
    if n:
        logger.debug('[Translate] Cleaned up %d expired tasks', n)
