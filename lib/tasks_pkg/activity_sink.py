"""lib/tasks_pkg/activity_sink.py — Default project-activity sink adapter.

This is the **host side** of the agent base's activity-feed seam (see
:mod:`lib.agent_core.activity`).  The reusable agent base (orchestrator,
endpoint, compaction — the ``CORE_MODULES`` in ``lib/agent_core_manifest.py``)
must NOT import ``lib.conversations`` directly; it emits a project-brain
Activity Feed pulse through :func:`lib.agent_core.activity.emit_activity_event`,
which — absent a host override — routes here.

Because this adapter binds the concrete ``lib.conversations.project_feed``
implementation, it lives OUTSIDE ``lib/agent_core/`` (a ``CORE_MODULES``
location, forbidden from importing ``lib.conversations``) — exactly mirroring
how :mod:`lib.tasks_pkg.persistence_store` is the DB-bound adapter behind the
``ConversationStore`` seam.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['emit_project_activity']


def emit_project_activity(project_path: str, conv_id: str, kind: str,
                          summary: str, *, user_id: int,
                          task_id: str = '', title: str = '',
                          payload: dict | None = None) -> dict | None:
    """Emit one project Activity Feed event via ``lib.conversations.project_feed``.

    Lazily imports the feed module so this adapter stays import-light and the
    conversations layer is only touched when an event is actually emitted.
    Best-effort — ``emit_project_event`` already swallows its own failures and
    never raises; this wrapper adds nothing beyond the lazy bind.
    """
    from lib.conversations.project_feed import emit_project_event
    return emit_project_event(project_path, conv_id, kind, summary,
                              user_id=user_id, task_id=task_id,
                              title=title, payload=payload)
