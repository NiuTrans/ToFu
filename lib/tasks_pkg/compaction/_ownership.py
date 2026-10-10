"""Compaction ownership key shared by stages and invocation settlement.

Children retain convId as their authorization link, not their working-state
identity. Callers must use this key for usage, caches, receipts and cleanup;
storage adapters remain responsible for admitting task-owned archive scopes.
"""
from lib.tasks_pkg.invocation import is_child_invocation


def compaction_owner_key(task: dict | None, conv_id: str = '') -> str:
    """Preserve root/direct-call keys; isolate children even with explicit convId."""
    if task is not None and is_child_invocation(task):
        task_id = task['id']
        if not isinstance(task_id, str) or not task_id:
            raise ValueError('Child compaction requires a nonempty task id')
        return f'invocation:{task["_userId"]}:{task_id}'
    return conv_id or ((task or {}).get('convId') or '')
