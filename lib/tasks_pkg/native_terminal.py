"""Owned durable or transient terminal delivery for the native process host.

Entry: NativeTaskTerminalPolicy. The application supplies its existing task,
projection callback and lifecycle-owned executor. The canonical task/Turn
writer retains storage authority, recovery fences and required delivery. An explicit
transient composition reuses the storage-free receiver; it confers no durability.
No model/tool execution, event publication, pool or storage backend is created.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from contextlib import nullcontext
import hashlib
import json
from functools import lru_cache
from typing import Callable

from lib.agent_core.native_process import TerminalRequest
from lib.identity import PrincipalContext


def task_lifetime_matches(task: dict, transient: bool) -> bool:
    """An explicit borrowed lifetime cannot acquire Turn, wallet or recovery authority."""
    if type(transient) is not bool:
        return False
    if transient:
        configuration = task.get('config') or {}
        if not isinstance(configuration, dict):
            return False
        return (task.get('_transientRuntime') is True
                and not any(task.get(key) for key in
                    ('_attemptId', '_turnId', '_billingUserId', '_nativeBillingSettlement'))
                and not configuration.get('_resumeSource'))
    return not task.get('_transientRuntime')


def _persist(task: dict) -> bool:
    from lib.tasks_pkg.manager._persist import persist_task_result

    return persist_task_result(task, _defer_heavy_release=True)


@lru_cache(maxsize=1)
def _execution_history_validator():
    from jsonschema import Draft202012Validator
    from lib.agent_core.native_content_contract import EXECUTION_HISTORY_SCHEMA
    return Draft202012Validator(EXECUTION_HISTORY_SCHEMA)


def project_native_terminal(task: dict, request: TerminalRequest) -> None:
    """Adapt the settled Rust result; the existing writer owns Turn publication."""
    if (request.task_id != task.get('id') or type(task.get('_userId')) is not int
            or request.principal.require_owner(context='native terminal projection') != task['_userId']
            or task.get('status') not in {'pending', 'running'}):
        raise ValueError('native terminal projection requires an owned live task')
    event = request.event
    status = event.get('status')
    if event.get('type') != status or status not in {'done', 'error', 'aborted'}:
        raise ValueError('native terminal projection outcome invalid')
    if request.usage_summary_json is not None and status != 'done':
        raise ValueError('native terminal usage summary requires success')
    execution=event.get('execution')
    if 'execution' in event and (status=='done' or
            next(_execution_history_validator().iter_errors(execution),None) is not None):
        raise ValueError('native terminal observed execution invalid')
    reason = 'stop' if status == 'done' else status
    if status == 'done':
        result = event.get('result')
        if not isinstance(result, dict):
            raise ValueError('native terminal result invalid')
        messages, usages = result.get('messages'), result.get('usage')
        if (not isinstance(messages, list) or not messages
                or any(not isinstance(row, dict) for row in messages)
                or messages[-1].get('role') != 'assistant'
                or messages[-1].get('tool_calls')
                or not isinstance(usages, list) or any(not isinstance(row, dict) for row in usages)):
            raise ValueError('native terminal history or usage invalid')
        final = messages[-1]
        content, thinking = final.get('content'), final.get('reasoning_content')
        if (content is not None and not isinstance(content, str)) or (thinking is not None and not isinstance(thinking, str)):
            raise ValueError('native terminal text invalid')
        usage = None
        if request.usage_summary_json is not None:
            from lib.agent_core.native_process import validate_usage_summary
            usage = validate_usage_summary(json.loads(request.usage_summary_json))
        elif task.get('_nativeFrozenCostProjection') is not True:
            from lib.cost import accumulate_usage_into
            usage = {}
            for row in usages:
                accumulate_usage_into(usage, row)
        from lib.tasks_pkg.manager._events import reset_task_text
        reset_task_text(task, content=content or '', thinking=(
            task.get('thinking') or '' if thinking is None else thinking))
        task['messages'] = messages
        task['_messages_include_current_run'] = True
        # Native paid rounds already accumulated this usage before delivery.
        if task.get('_nativeFrozenCostProjection') is not True:
            task['usage'] = usage
    elif status == 'error':
        if not isinstance(event.get('error'), dict) or not event['error']:
            raise ValueError('native terminal error evidence missing')
        from lib.error_envelope import normalize_envelope
        error_envelope = normalize_envelope(event.get('error'), context='native-execution', source='native')
        if not error_envelope:
            raise ValueError('native terminal error evidence missing')
        task['error'] = error_envelope
    else:
        from lib.agent_core.execution_limits import execution_limit_reason
        reason = execution_limit_reason(task) or 'aborted'
        signal = task.get('abort_event')
        if not callable(getattr(signal, 'set', None)):
            raise ValueError('native terminal abort signal missing')
        task['aborted'] = True
        signal.set()
    if 'execution' in event:
        task['messages']=execution['messages']
        task['_messages_include_current_run']=True
    if task.get('_todoState'):
        from lib.tools.todo import public_todo_state
        task['todoState'] = public_todo_state(task['_todoState'])
    from lib.agent_core.execution_budget import ExecutionBudget
    account = task.get('_executionBudget')
    if isinstance(account, ExecutionBudget):
        task['_executionActualsComplete'] = (account.participant_actuals_complete(task)
            if task.get('_parentTaskId') else account.actuals_complete())
    from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal
    if not stamp_chat_task_terminal(task, status=status, finish_reason=reason):
        raise ValueError('native terminal projection transition refused')


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
        transient: bool = False,
    ):
        self.owner = principal.require_owner(context="native task terminal")
        if type(transient) is not bool:
            raise ValueError('native terminal lifetime must be explicit')
        self._transient = transient
        if (type(task.get('_userId')) is not int
                or task['_userId'] != self.owner
                or not isinstance(task.get('id'), str) or not task['id']
                or not task_lifetime_matches(task, transient)
                or task.get('status') not in {'pending', 'running'}):
            raise ValueError('native terminal requires an owned live task with its declared lifetime')
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
        self._resource_refusal = False

    def _check_owner(self):
        if (self.task.get('id') != self.task_id
                or type(self.task.get('_userId')) is not int
                or self.task['_userId'] != self.owner
                or not task_lifetime_matches(self.task, self._transient)):
            raise ValueError('native terminal task ownership changed')

    @property
    def accepted(self) -> bool:
        """True after the selected durable writer or transient receiver acknowledges."""
        return self._accepted

    def _projection_identity(self):
        # Detect a competing finalizer changing the projected terminal outcome.
        fields = ('status', 'content', 'thinking', 'finishReason', 'error',
                  '_contentEpoch', '_attemptId', 'usage', 'cost', 'apiRounds',
                  '_nativePricingRequired', '_billingUserId', '_billing_reservation_micro',
                  '_nativeBillingSettlement')
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
                if self.task.get('_nativePricingRequired') is True and self.task.get('_billingUserId'):
                    from lib.billing.native_settlement import freeze
                    freeze(self.task)
                from lib.agent_core.execution_session import (
                    ExecutionSession,execution_outcome_for_task,settle_task_execution)
                session=self.task.get('_executionSession')
                if isinstance(session,ExecutionSession):
                    if session.owner_user_id!=self.owner or session.execution_id!=self.task_id:
                        raise ValueError('native terminal execution session owner differs')
                    requested=execution_outcome_for_task(self.task)
                    receipt=settle_task_execution(self.task)
                    if not receipt.invariants_satisfied or receipt.outcome!=requested:
                        from lib.error_envelope import make_envelope
                        self.task.update(status='error',finishReason='error',error=make_envelope(
                            'internal',context='execution-settlement',source='native-task-terminal'))
                        self._resource_refusal=True
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
            if self._resource_refusal:
                self._rejected=True
                return 'rejected'
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
        if len(request.event_json) + len(request.receipt_json) + len(request.usage_summary_json or b'') > self.candidate_bytes:
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
        digest.update(b'\x00')
        digest.update(request.usage_summary_json or b'')
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
