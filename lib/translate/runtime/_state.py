"""Own the process-wide translation TaskRuntime and its retention sweep.

All translation producers use the runtime's public lifecycle, mutation, event,
and ownership APIs. Registry storage and locks remain private to TaskRuntime.
"""

from lib.log import get_logger
from lib.agent_core.task_runtime import TaskRuntime

logger = get_logger(__name__)


# ── Async translation tasks (survive page reload / tab switch) ──
_translate_runtime = TaskRuntime(
    'translate', ttl=1800,
    push_channel='translate',
    error_source='routes.translate',
)

def _cleanup_translate_tasks():
    """Remove expired translation tasks (delegates to TaskRuntime)."""
    n = _translate_runtime.cleanup_stale()
    if n:
        logger.debug('[Translate] Cleaned up %d expired tasks', n)
