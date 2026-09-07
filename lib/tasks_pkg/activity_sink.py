"""Host adapter for important signal-driven Project Brain results.

This is the **host side** of the agent base's activity-feed seam (see
:mod:`lib.agent_core.activity`).  The reusable agent base (orchestrator,
endpoint, compaction — the ``CORE_MODULES`` in ``lib/agent_core_manifest.py``)
must NOT import ``lib.conversations`` directly; it emits a project-brain
Activity Feed pulse through :func:`lib.agent_core.activity.emit_activity_event`,
which — absent a host override — routes here.

Because this adapter binds the concrete signal projection implementation, it
lives OUTSIDE ``lib/agent_core/`` (a ``CORE_MODULES``
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
    """Persist only meaningful run outcomes; suppress lifecycle telemetry."""
    del title, payload
    if kind != 'run_concluded':
        return None
    try:
        from lib.conversations.project_brain import add_narrative
        return add_narrative(
            project_path, kind=kind, text=summary, user_id=user_id,
            work_id=task_id, conversation_id=conv_id,
            command_id=f'run-concluded:{task_id or conv_id}',
        )
    except Exception as exc:
        logger.debug('[ProjectBrain] important activity skipped: %s', exc)
        return None
