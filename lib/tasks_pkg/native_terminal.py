"""Application terminal persistence for the owned native process host.

Entry: NativeTaskTerminalPolicy. The application supplies its existing task,
projection callback and lifecycle-owned executor. The canonical task/Turn
writer retains storage authority, recovery fences and required delivery.
No model/tool execution, event publication, pool or storage backend is created.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from contextlib import nullcontext
import hashlib
import json
from typing import Callable

from lib.agent_core.native_process import TerminalRequest
from lib.identity import PrincipalContext


def _persist(task: dict) -> bool:
    from lib.tasks_pkg.manager._persist import persist_task_result

    return persist_task_result(task, _defer_heavy_release=True)


class NativeTaskTerminalPolicy:
    """ACK only after the application's owned terminal writer acknowledges.

    ``project`` adapts the frozen native result into the existing task/Turn
    projection once, including text, round evidence and canonical accounting.
    It must use existing domain owners, return None and publish no speculative
    success. Projection failure is terminal for this policy; commit debt retries
    never repeat projection, provider execution or tool effects. The caller
    retains the task and releases heavy state after final native publication.
    """

    def __init__(
        self,
        *,
        principal: PrincipalContext,
        task: dict,
        operation_id: str,
        executor: Executor,
        project: Callable[[dict, TerminalRequest], None],
        candidate_bytes: int,
    ):
        self.owner = principal.require_owner(context="native task terminal")
        if (type(task.get('_userId')) is not int
                or task['_userId'] != self.owner
                or not isinstance(task.get('id'), str) or not task['id']
                or task.get('_transientRuntime')
                or task.get('status') not in {'pending', 'running'}):
            raise ValueError('native terminal requires an owned durable live task')
        if (not isinstance(operation_id, str) or not operation_id
                or not isinstance(executor, Executor) or not callable(project)
                or type(candidate_bytes) is not int or candidate_bytes <= 0):
            raise ValueError('native terminal requires explicit bounded composition')
        self.task = task
        self.task_id = task['id']
        self.operation_id = operation_id
        self.executor = executor
        self.project = project
        self.candidate_bytes = candidate_bytes
        self._lock = asyncio.Lock()
        self._candidate = None
        self._projection = None
        self._projection_error = None
        self._accepted = False
        self._rejected = False

    def _check_owner(self):
        if (self.task.get('id') != self.task_id
                or type(self.task.get('_userId')) is not int
                or self.task['_userId'] != self.owner
                or self.task.get('_transientRuntime')):
            raise ValueError('native terminal task ownership changed')

    @property
    def accepted(self) -> bool:
        """True only after the canonical terminal writer acknowledged durability."""
        return self._accepted

    def _projection_identity(self):
        # Detect a competing finalizer changing the projected terminal outcome.
        fields = ('status', 'content', 'thinking', 'finishReason', 'error',
                  '_contentEpoch', '_attemptId')
        with self.task.get('content_lock') or nullcontext():
            encoded = json.dumps(
                {key: self.task.get(key) for key in fields},
                ensure_ascii=False, allow_nan=False, separators=(',', ':'),
                sort_keys=True,
            ).encode()
        return hashlib.sha256(encoded).digest()

    def _commit(self, request, status):
        self._check_owner()
        if self._projection_error is not None:
            raise RuntimeError('native terminal projection previously failed') from self._projection_error
        if self._projection is None:
            try:
                if self.project(self.task, request) is not None:
                    raise TypeError('native terminal projection must return None')
                self._check_owner()
                if self.task.get('status') != status:
                    raise ValueError('native terminal projection changed outcome')
                self._projection = self._projection_identity()
            except BaseException as error:
                self._projection_error = error
                self.task['_terminalPersistencePending'] = True
                self.task['_terminalPersistenceRetryReady'] = False
                raise
        if self._projection_identity() != self._projection:
            raise ValueError('native terminal projected outcome changed')
        if self._accepted:
            return 'accepted'
        if self._rejected:
            return 'rejected'
        self.task['_terminalPersistencePending'] = True
        try:
            acknowledged = _persist(self.task)
        except BaseException:
            self.task['_terminalPersistencePending'] = True
            self.task['_terminalPersistenceRetryReady'] = True
            raise
        if acknowledged is True:
            self._accepted = True
            return 'accepted'
        if acknowledged is not False:
            raise TypeError('native terminal writer must return an acknowledgement')
        if self.task.get('_workspaceDeliveryOwnershipRejected'):
            self._rejected = True
            return 'rejected'
        self.task['_terminalPersistencePending'] = True
        self.task['_terminalPersistenceRetryReady'] = True
        return 'retry'

    async def persist(self, request: TerminalRequest) -> str:
        if (request.principal.require_owner(context='native task terminal') != self.owner
                or request.task_id != self.task_id
                or request.operation_id != self.operation_id
                or type(request.attempt) is not int or request.attempt < 1):
            raise ValueError('native terminal request identity mismatch')
        if len(request.event_json) + len(request.receipt_json) > self.candidate_bytes:
            raise ValueError('native terminal candidate exceeds bound')
        event, receipt = request.event, request.receipt
        if (not isinstance(event, dict) or not isinstance(receipt, dict)
                or event.get('status') not in {'done', 'error', 'aborted'}
                or event.get('type') != event['status']
                or type(receipt.get('invariants_satisfied')) is not bool
                or (not receipt['invariants_satisfied'] and event['status'] != 'error')):
            raise ValueError('native terminal candidate has no settled outcome')
        digest = hashlib.sha256(request.event_json)
        digest.update(b'\x00')
        digest.update(request.receipt_json)
        candidate = digest.digest()
        async with self._lock:
            if self._candidate is not None and self._candidate != candidate:
                raise ValueError('native terminal candidate changed')
            self._candidate = candidate
            work = asyncio.get_running_loop().run_in_executor(
                self.executor, self._commit, request, event['status'],
            )
            cancelled = False
            while True:
                try:
                    decision = await asyncio.shield(work)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if work.done():
                        decision = work.result()
                        break
            if cancelled:
                raise asyncio.CancelledError
            return decision
