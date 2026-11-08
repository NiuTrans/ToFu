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

    def __init__(self, runtime=chat_task_runtime):
        self._runtime = runtime

    def __getitem__(self, task_id: str) -> dict:
        task = self._runtime.get(task_id)
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
        self._runtime.discard(task_id)
        if not self._runtime.adopt(task):
            raise ValueError(f'chat task fixture could not be adopted: {task_id}')
        task['status'] = requested_status

    def __delitem__(self, task_id: str) -> None:
        if self._runtime.discard(task_id) is None:
            raise KeyError(task_id)

    def __iter__(self) -> Iterator[str]:
        return iter(self._runtime.task_ids())

    def __len__(self) -> int:
        return self._runtime.task_count()

    def get(self, task_id: str, default=None):
        task = self._runtime.get(task_id)
        return default if task is None else task

    def pop(self, task_id: str, default=None):
        task = self._runtime.discard(task_id)
        return default if task is None else task

    def clear(self) -> None:
        for task_id in self._runtime.task_ids():
            self._runtime.discard(task_id)

    def values(self):
        return self._runtime.snapshot()

    def items(self):
        return [
            (str(task.get('id') or ''), task)
            for task in self._runtime.snapshot()
        ]


chat_task_registry = ChatTaskTestRegistry()


def register_local_executor(task_id: str, *, conv_id: str = '', attempt_id: str = '',
                            turn_id: str = '', user_id: int = 1) -> dict:
    """Model a fake executor as the live local executor of its attempt.

    Tests replace ``start_conversation_attempt_executor`` with a stub that only
    binds an attempt's task id. A steer publishes its wakeup into this
    process's inbox only for an attempt this process executes, so such a stub
    must also register here; otherwise the steer is committed for the other
    server process. Call ``chat_task_registry.pop(task_id)`` on teardown.
    """
    chat_task_registry[task_id] = {'id': task_id, 'convId': conv_id,
        '_attemptId': attempt_id, '_turnId': turn_id, '_userId': user_id,
        'status': 'running', 'aborted': False}
    return chat_task_registry[task_id]


# Fixture setup is normally single-threaded.  The guard preserves multi-line
# setup readability without exposing or pretending to own TaskRuntime's lock;
# each mapping operation above remains independently synchronized by runtime.
chat_task_fixture_guard = threading.RLock()


def isolated_reaper_registry(monkeypatch) -> ChatTaskTestRegistry:
    """Point the stuck-task reaper at a registry holding only this test's tasks.

    ``reap_stuck_running_tasks`` sweeps the whole process registry, which in a
    shared worker also holds tasks that earlier tests left running; a reaped
    count or verdict must not include them. Insert the test's own task dicts
    into the returned view; they may also stay in ``chat_task_registry`` for
    reads, because both registries hold the same dict.
    """
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.tasks_pkg.manager import _maintenance

    runtime = TaskRuntime('chat', ttl=3600, push_channel=None)
    monkeypatch.setattr(_maintenance, 'chat_task_runtime', runtime, raising=True)
    return ChatTaskTestRegistry(runtime)


__all__ = ['chat_task_fixture_guard', 'chat_task_registry', 'isolated_reaper_registry',
           'register_local_executor']
