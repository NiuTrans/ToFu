"""lib/agent_core/activity.py — Accessor for the project-activity feed seam.

The reusable agent base emits project-brain "Activity Feed" lifecycle pulses
(a turn ``started`` / ``completed`` / ``aborted``) through this seam rather
than importing ``lib.conversations.project_feed`` directly.  This module is
part of the agent base (``lib.agent_core`` is in ``CORE_MODULES``), so it MUST
NOT import ``lib.conversations`` — it only names the *default adapter module*
(:mod:`lib.tasks_pkg.activity_sink`), which is itself non-core and free to bind
the conversations layer.

Mirrors :mod:`lib.agent_core.store` (the ``ConversationStore`` seam) exactly:
a host embedding the agent base against a different — or absent — activity
backend calls :func:`set_activity_sink` once at startup.  When no override is
installed the chatui default (:func:`lib.tasks_pkg.activity_sink.emit_project_activity`)
is lazily bound on first use.  If even that import fails (a standalone
``tofu-agent`` deployment with no conversations layer at all), emission
degrades to a no-op — a missing activity feed must NEVER break a task.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['emit_activity_event', 'set_activity_sink']

# A sink is any callable with emit_project_event's keyword signature.
_ActivitySink = Callable[..., Optional[dict]]

_sink: Optional[_ActivitySink] = None
_lock = threading.Lock()
_default_missing = False  # latched True if the default adapter can't be imported


def set_activity_sink(sink: Optional[_ActivitySink]) -> None:
    """Install the host's activity-feed sink.

    Call once at startup before any task runs.  Passing ``None`` resets to the
    lazily-bound chatui default (useful in tests).
    """
    global _sink, _default_missing
    with _lock:
        _sink = sink
        _default_missing = False
    logger.info('[Activity] project-activity sink set to %s',
                getattr(sink, '__name__', type(sink).__name__)
                if sink is not None else 'default (reset)')


def emit_activity_event(project_path: str, conv_id: str, kind: str,
                        summary: str, *, user_id: int,
                        task_id: str = '', title: str = '',
                        payload: dict | None = None) -> dict | None:
    """Emit one project Activity Feed event through the active sink.

    Resolves the default adapter lazily so this core module never pulls in the
    conversations layer at import time, and so a host can override before first
    use.  Best-effort: if no sink is available (default import failed on a
    conversations-less deployment) or the sink raises, the failure is logged
    and swallowed — a project-feed emit must never break the task that
    triggered it.
    """
    global _sink, _default_missing
    sink = _sink
    if sink is None and not _default_missing:
        with _lock:
            if _sink is None and not _default_missing:
                try:
                    from lib.tasks_pkg.activity_sink import emit_project_activity
                    _sink = emit_project_activity
                except Exception as e:
                    _default_missing = True
                    logger.debug('[Activity] no default activity sink available '
                                 '(feed disabled): %s', e)
            sink = _sink
    if sink is None:
        return None
    try:
        return sink(project_path, conv_id, kind, summary,
                    user_id=user_id, task_id=task_id, title=title,
                    payload=payload)
    except Exception as e:
        logger.debug('[Activity] sink emit failed (swallowed): %s', e)
        return None
