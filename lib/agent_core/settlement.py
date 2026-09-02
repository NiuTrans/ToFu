"""Host port for work triggered after a task becomes durably settled.

The reusable agent loop owns the lifecycle moment but not project-specific
effects. A host adapter receives the settled task, explicit project path and
owner identity. Missing or failed optional project behavior cannot change the
task's terminal result.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from lib.log import get_logger

logger = get_logger(__name__)

_SettlementSink = Callable[..., None]
_sink: _SettlementSink | None = None
_lock = threading.Lock()
_default_missing = False


def set_settlement_sink(sink: _SettlementSink | None) -> None:
    """Install the host settlement adapter; ``None`` resets lazy binding."""
    global _sink, _default_missing
    with _lock:
        _sink = sink
        _default_missing = False


def notify_project_task_settled(
    task: dict,
    project_path: str,
    *,
    user_id: int,
) -> None:
    """Notify the host after terminal persistence without importing it."""
    global _sink, _default_missing
    sink = _sink
    if sink is None and not _default_missing:
        with _lock:
            if _sink is None and not _default_missing:
                try:
                    from lib.tasks_pkg.project_settlement_sink import (
                        settle_project_task,
                    )

                    _sink = settle_project_task
                except Exception as exc:
                    _default_missing = True
                    logger.debug(
                        "[Settlement] no host adapter available: %s", exc
                    )
            sink = _sink
    if sink is None:
        return
    try:
        sink(task, project_path, user_id=int(user_id))
    except Exception as exc:
        logger.debug("[Settlement] host adapter failed: %s", exc)


__all__ = ["notify_project_task_settled", "set_settlement_sink"]
