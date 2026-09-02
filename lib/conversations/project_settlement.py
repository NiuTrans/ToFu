"""Post-settlement project hooks.

This is the only task-lifecycle entry point for work that becomes valid after
a conversation turn is durable and idle. It schedules cached sibling-summary
refresh and gives the dispatch engine an opportunity to route queued project
work. Both operations receive the task's explicit owner.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def on_project_task_settled(task: dict, project_path: str, *, user_id: int) -> None:
    """Run best-effort project hooks after one task reaches terminal state."""
    conv_id = str((task or {}).get('convId') or '').strip()
    project_path = str(project_path or '').strip()
    if not (conv_id and project_path):
        return

    try:
        from lib.conversations.project_summary import ensure_summary

        ensure_summary(conv_id, user_id=user_id, blocking=False)
    except Exception as exc:
        logger.debug(
            '[ProjectSettlement] summary scheduling failed conv=%s: %s',
            conv_id[:8],
            exc,
        )

    # An explicit Stop or failed turn must leave the conversation idle for the
    # human; it must not immediately replace itself with automatic Board work.
    if task.get('aborted') or task.get('error'):
        return
    try:
        from lib.conversations.project_dispatch import on_conv_idle

        on_conv_idle(project_path, conv_id, user_id=user_id)
    except Exception as exc:
        logger.debug(
            '[ProjectSettlement] idle dispatch failed conv=%s: %s',
            conv_id[:8],
            exc,
        )


__all__ = ['on_project_task_settled']
