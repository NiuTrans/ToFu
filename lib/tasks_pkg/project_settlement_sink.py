"""ChatUI adapter for the agent-core post-settlement port."""

from __future__ import annotations


def settle_project_task(task: dict, project_path: str, *, user_id: int) -> None:
    """Delegate a settled task to the project coordination owner."""
    from lib.conversations.project_settlement import on_project_task_settled

    on_project_task_settled(task, project_path, user_id=int(user_id))


__all__ = ["settle_project_task"]
