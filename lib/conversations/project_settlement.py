"""Post-settlement signal-driven Project Brain hook."""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def on_project_task_settled(task: dict, project_path: str, *, user_id: int) -> None:
    """Finish this task's derived work item without dispatch or handoff."""
    conv_id = str((task or {}).get('convId') or '').strip()
    project_path = str(project_path or '').strip()
    if not (conv_id and project_path):
        return

    try:
        from lib.conversations.project_brain import settle_work_item
        settle_work_item(task, project_path)
    except Exception as exc:
        logger.debug(
            '[ProjectSettlement] work settlement failed conv=%s: %s',
            conv_id[:8],
            exc,
        )


__all__ = ['on_project_task_settled']
