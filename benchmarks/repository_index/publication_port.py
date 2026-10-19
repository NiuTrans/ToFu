"""Canonical transient chat registration and public repository event publication.

Uses real manager task creation, registry lookup, handler and finalizer. No global
binding changes. Durable storage, gateway admission and full agent remain gaps.
"""
import os

from lib.repo_map import outline
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers import project
from lib.tasks_pkg.manager import create_task, task_user_id
from lib.tasks_pkg.manager.runtime import chat_task_runtime


class TransientChatRuntime:
    """Own only tasks created here; never sweep another runtime consumer."""
    def __init__(self, *args, **kwargs):
        self.owned = set()

    def create(self, *, user_id, task_id=""):
        # Canonical manager admission mints the actual task identity; the
        # reused worker's synthetic label has no authority.
        task = create_task('', [], {}, user_id=user_id, transient=True, supersede=False)
        self.owned.add(task['id'])
        return task

    def mark_running(self, task_id):
        assert task_id in self.owned
        return chat_task_runtime.mark_running(task_id)

    def finish(self, task_id, **options):
        assert task_id in self.owned
        return chat_task_runtime.finish(task_id, **options)

    def cleanup_stale(self, *, max_age):
        assert max_age == 0
        for task_id in tuple(self.owned):
            task = chat_task_runtime.get(task_id)
            assert task is not None and task['status'] in ('done', 'error', 'aborted')
            chat_task_runtime.discard(task_id)
            self.owned.remove(task_id)

    @property
    def task_count(self):
        return len(self.owned)


class PublishedRepositoryToolPort:
    def __init__(self, root, scope=None, *, template=None):
        assert template is None
        self.root = os.path.abspath(root)
        self.scope = scope
        self.calls = 0

    def invoke(self, task, arguments, finalize, *, project_root=None):
        owner = task_user_id(task)
        if not task.get('_transientRuntime'):
            raise ValueError('publication benchmark requires explicit transient chat task')
        def scanner(root, **options):
            if os.path.abspath(root) != self.root:
                raise PermissionError('repository index belongs to another project root')
            return outline.scan(root, **options) if self.scope is None else self.scope.scan(owner, **options)
        handler = tool_registry.lookup('repo_map')
        assert handler is project._handle_project_tool
        self.calls += 1
        call_id = f'repository-port-call-{self.calls}'
        entry = {'query': 'repository publication benchmark', 'toolName': 'repo_map',
                 'toolCallId': call_id}
        result = handler(task, {}, 'repo_map', call_id, arguments,
                         self.calls, entry, {}, self.root if project_root is None else project_root,
                         True, repository_scanner=scanner)
        assert entry['status'] in ('done', 'error')
        finalize(task, self.calls, entry, entry['results'], status=entry['status'])
        return result
