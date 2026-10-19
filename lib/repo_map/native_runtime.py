"""Explicit application owner for native repository scans.

NativeRepositoryRuntime lazily opens installed immutable code, lends task/root
scopes and binds their cleanup to the existing ExecutionSession. It owns no
repository cache or resident child. Grammars select a trusted external directory
or the explicit installed bundle; byte budgets consume trusted launch capacity.
Product defaults and remaining performance admission are separate.
"""
import os
from threading import RLock

from lib.identity import require_user_id
from ._native_limits import StreamLimits


class NativeRepositoryRuntime:
    def __init__(self, grammar_directory, limits, *, timeout=60.0):
        self.grammar_directory = os.path.abspath(os.fsdecode(grammar_directory)) if grammar_directory is not None else None
        self.limits = limits if isinstance(limits, StreamLimits) else StreamLimits(*limits)
        self.timeout = timeout
        self._binding_lock = RLock()
        self._runtime_image = None
        self._closed = False

    def retained_image_bytes(self):
        with self._binding_lock:
            image = self._runtime_image
        return image.retained_image_bytes() if image is not None else 0

    def scanner_for_task(self, task, root):
        from lib.tasks_pkg.manager import task_user_id
        from lib.agent_core.execution_session import bind_repository_scanner, execution_session_for_task
        owner = require_user_id(task_user_id(task), context='native repository task')
        session = execution_session_for_task(task)
        if session.owner_user_id != owner or session.execution_id != task.get('id'):
            raise PermissionError('native repository scan requires its owned execution session')
        root = os.path.abspath(os.fsdecode(root))
        with self._binding_lock:
            if self._closed or task.get('_repository_scanner_closed') or session.is_terminal:
                raise RuntimeError('native repository execution is closed')
            previous = task.get('_repository_scanner_binding')
            if previous is not None:
                if previous.runtime is not self or previous.owner != owner or previous.root != root:
                    raise PermissionError('native repository scanner belongs to another execution root or owner')
                return previous
            from ._native_owned import OwnedStreamScope, RuntimeStreamImage
            if self._runtime_image is None:
                self._runtime_image = RuntimeStreamImage.installed()
            scope = OwnedStreamScope(root, owner, None, self.grammar_directory,
                expected_sha256=None, limits=self.limits,
                cancellation=task['abort_event'], runtime_image=self._runtime_image,
                strict=False)  # Canonical grammar failure is local to one file.
            scanner = _TaskScanner(self, scope, owner, root)
            # Publish under the binding lock before registering. Terminal release
            # never takes this lock (ExecutionSession holds its own lock during
            # callbacks), so it can remove the exact binding without inversion.
            task['_repository_scanner_binding'] = scanner
            def release():
                try:
                    scope.close(owner)
                finally:
                    if scope._closed:
                        task['_repository_scanner_closed'] = True
                        if task.get('_repository_scanner_binding') is scanner:
                            task.pop('_repository_scanner_binding', None)
            bind_repository_scanner(session, release)
            return scanner

    def close(self):
        with self._binding_lock:
            self._closed = True
            image = self._runtime_image
        # Wait without the binding lock. Failed disposal keeps the same owner so
        # a later close can retry; completed task records cannot prolong the fd.
        if image is not None:
            image.close()


class _TaskScanner:
    def __init__(self, runtime, scope, owner, root):
        self.runtime, self.scope, self.owner, self.root = runtime, scope, owner, root

    def __call__(self, root, **options):
        if os.path.abspath(os.fsdecode(root)) != self.root:
            raise PermissionError('native repository scanner belongs to another project root')
        return self.scope.scan(self.owner, timeout=self.runtime.timeout, **options)


def scanner_for_task(task, root):
    """Resolve only a trusted server-composed owner, never model arguments."""
    runtime = task.get('_repository_runtime')
    if runtime is None:
        return None
    if not isinstance(runtime, NativeRepositoryRuntime):
        raise TypeError('native repository runtime must be server composed')
    return runtime.scanner_for_task(task, root)
