"""Test-only mapping view backed exclusively by TaskRuntime's public API.

Older tests inserted partial dictionaries into the chat runtime's private
``_tasks`` map while holding its private lock.  That made the tests preserve
the very architecture the application was trying to remove.  This adapter
keeps concise fixture setup while normalizing every record through
``TaskRuntime.adopt`` and unregistering it through ``TaskRuntime.discard``.
Application modules must use owner-scoped runtime methods directly.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, MutableMapping

from lib.identity import PrincipalContext
from lib.tasks_pkg.manager.runtime import chat_task_runtime


class ChatTaskTestRegistry(MutableMapping[str, dict]):
    """Mutable test-fixture view over the public chat runtime operations."""

    def __getitem__(self, task_id: str) -> dict:
        task = chat_task_runtime.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def __setitem__(self, task_id: str, task: dict) -> None:
        if not isinstance(task, dict):
            raise TypeError('chat task fixture must be a dict')
        if task.get('id') not in (None, '', task_id):
            raise ValueError('chat task fixture key/id mismatch')
        task['id'] = task_id
        owner_user_id = int(task.get('_userId') or 1)
        task['_userId'] = owner_user_id
        task['_principalContext'] = PrincipalContext.user(
            subject_id=f'test-user:{owner_user_id}',
            owner_user_id=owner_user_id,
        ).to_payload()
        requested_status = str(task.get('status') or 'running')
        task['status'] = 'running'
        chat_task_runtime.discard(task_id)
        if not chat_task_runtime.adopt(task):
            raise ValueError(f'chat task fixture could not be adopted: {task_id}')
        task['status'] = requested_status

    def __delitem__(self, task_id: str) -> None:
        if chat_task_runtime.discard(task_id) is None:
            raise KeyError(task_id)

    def __iter__(self) -> Iterator[str]:
        return iter(chat_task_runtime.task_ids())

    def __len__(self) -> int:
        return chat_task_runtime.task_count()

    def get(self, task_id: str, default=None):
        task = chat_task_runtime.get(task_id)
        return default if task is None else task

    def pop(self, task_id: str, default=None):
        task = chat_task_runtime.discard(task_id)
        return default if task is None else task

    def clear(self) -> None:
        for task_id in chat_task_runtime.task_ids():
            chat_task_runtime.discard(task_id)

    def values(self):
        return chat_task_runtime.snapshot()

    def items(self):
        return [
            (str(task.get('id') or ''), task)
            for task in chat_task_runtime.snapshot()
        ]


chat_task_registry = ChatTaskTestRegistry()

# Fixture setup is normally single-threaded.  The guard preserves multi-line
# setup readability without exposing or pretending to own TaskRuntime's lock;
# each mapping operation above remains independently synchronized by runtime.
chat_task_fixture_guard = threading.RLock()


__all__ = ['chat_task_fixture_guard', 'chat_task_registry']
