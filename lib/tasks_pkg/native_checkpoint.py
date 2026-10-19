"""Application callbacks delegate checkpoint transitions to the selected Rust port.

Entry: NativeCheckpointPolicy. Callers supply storage, task, owned executor,
immutable model/tool/account binding and financial/time recovery. No database
open, journal state machine, history cache, worker or automatic takeover.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import Executor
from dataclasses import replace
import json
import time
from typing import Protocol

from lib.agent_core.native_process import (
    CheckpointRequest, CheckpointTerminalRequest, checkpoint_state_digest,
    reconciled_tool_observations, _joined,
)
from lib.identity import PrincipalContext
from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES


class PendingToolRecovery(Protocol):
    """Domain-owned occurrence capture and queries, with an exact commit barrier."""
    async def capture(self, request: CheckpointRequest) -> Mapping: ...
    async def committed(self, request: CheckpointRequest) -> None: ...
    async def reconcile(self, request: CheckpointRequest) -> list[Mapping]: ...


def checkpoint_lifetime_matches(task, policy, configuration, transient):
    """Registered child observations are explicit; headless carriers stay transient."""
    registered = isinstance(policy, NativeCheckpointPolicy) and policy.registered_child
    if transient:
        if registered:
            return (policy.task is task and configuration.get('checkpoint_actor_account') is True
                    and configuration.get('checkpoint_shared_account') is True)
        return policy is None and not any(key.startswith('checkpoint_') for key in configuration)
    return not registered and configuration.get('checkpoint_actor_account') is not True


class NativeCheckpointPolicy:
    def __init__(self, *, principal: PrincipalContext, task: dict, storage,
                 executor: Executor, epoch: str, binding: str, token: str,
                 header_bytes: int, snapshot_bytes: int, identity_bytes: int,
                 io_timeout_ms: int, restore: Callable, suspend: bool = False,
                 recover_abandoned: bool = False, handoff_from_accepted: bool = False,
                 tool_recovery: PendingToolRecovery | None = None, registered_child: bool = False):
        self.owner = principal.require_owner(context='native checkpoint policy')
        from tofu_db.generated_checkpoint_control import CHECKPOINT_DOCUMENT_MAX_BYTES
        if any(type(value) is not int or not 0 < value <= CHECKPOINT_DOCUMENT_MAX_BYTES
               for value in (header_bytes, snapshot_bytes)):
            raise ValueError('native checkpoint exceeds document capacity')
        if (type(task.get('_userId')) is not int or task['_userId'] != self.owner
                or not isinstance(task.get('id'), str) or not task['id']
                or (task.get('_transientRuntime') and not registered_child) or not isinstance(executor, Executor)
                or type(registered_child) is not bool
                or (registered_child and (not task.get('_transientRuntime') or recover_abandoned or handoff_from_accepted))
                or any(not isinstance(value, str) or not value for value in (epoch, binding, token))
                or any(type(value) is not int or value <= 0 for value in (
                    header_bytes, snapshot_bytes, identity_bytes, io_timeout_ms))
                or not callable(restore) or type(suspend) is not bool or type(recover_abandoned) is not bool
                or type(handoff_from_accepted) is not bool or (handoff_from_accepted and recover_abandoned)
                or (tool_recovery is not None and not all(callable(getattr(tool_recovery, action, None))
                    for action in ('capture', 'committed', 'reconcile')))
                or not callable(getattr(storage, 'native_checkpoint_control', None))
                or not callable(getattr(storage, 'native_checkpoint_available', None))
                or not storage.native_checkpoint_available(user_id=self.owner)):
            raise ValueError('native checkpoints require owned selected storage, executor and recovery')
        self.task = task
        self.registered_child = registered_child
        self.child_identity = None
        if registered_child:
            from lib.agent_core.execution_budget import ExecutionBudget
            budget = task.get('_executionBudget')
            if (task.get('_invocationStorageFree') is not False or not isinstance(budget, ExecutionBudget)
                    or task['id'] == budget.root_id or not budget.authorizes_model_owner(task, self.owner)):
                raise ValueError('native child checkpoint requires registered shared-account authority')
            self.child_identity = dict(user_id=self.owner, conversation_id=task.get('convId'),
                parent_task_id=task.get('_parentTaskId'), child_task_id=task['id'],
                archive_scope=task.get('_archiveScope'))
            self._verify_child(storage)
        self.task_id = task['id']
        self.storage = storage
        self.executor = executor
        self.epoch = epoch
        self.binding = binding
        self.suspend = suspend
        self.recover_abandoned = recover_abandoned
        self.handoff_from_accepted = handoff_from_accepted
        self.restore = restore
        self.tool_recovery = tool_recovery
        self.supports_pending_tools = tool_recovery is not None
        self.pending_metadata = None
        self.recovery_digest = None
        self.parameters = dict(
            owner_user_id=self.owner, task_id=self.task_id, epoch=epoch,
            binding=binding, token=token, header_bytes=header_bytes,
            snapshot_bytes=snapshot_bytes, identity_bytes=identity_bytes,
        )
        self.response_bytes = snapshot_bytes + RESPONSE_HEADROOM_BYTES
        self.io_timeout_ms = io_timeout_ms
        self.capability = None
        self.creation_started = False
        self.sealed = False
        self.paused = False
        self.closed = False

    def prepare_recovered_epoch(self, *, epoch, binding):
        """Select the claimed source epoch without changing the factory's binding.

        This grants no lease: the native whole-set transaction still revalidates
        source identity, version and policy binding before capability adoption.
        """
        if (not self.registered_child or self.closed or self.creation_started
                or self.capability is not None or not isinstance(epoch, str) or not epoch
                or binding != self.binding or self.parameters['binding'] != binding):
            raise ValueError('recovered epoch requires a pristine matching checkpoint policy')
        self.epoch = epoch
        self.parameters['epoch'] = epoch

    def adopt_recovered_capability(self, capability, *, effect_origin=None):
        """Bind an acknowledged whole-set lease to this pristine child policy.

        The budget prefix must already be acknowledged. Native store admission
        owns the capability; this method never creates or recovers a journal.
        """
        if (type(capability) is not int or capability <= 0 or not self.registered_child
                or self.closed or self.creation_started or self.capability is not None):
            raise ValueError('recovered actor requires an unused registered checkpoint policy')
        self._verify_child(self.storage)
        if self.task['_executionBudget'].recovered_actor_prefix_tokens(self.task, owner=self.owner) is None:
            raise ValueError('recovered actor has no acknowledged account prefix')
        if effect_origin is not None:
            self.tool_recovery.bind_effect_origin(effect_origin)
        self.capability = capability

    def _verify_child(self, storage):
        from lib.tasks_pkg.invocation_registration import owned_child_record, INVOCATION_COMPLETION_FIELD
        identity = self.child_identity
        record = owned_child_record(storage, user_id=self.owner,
            conversation_id=identity['conversation_id'], invocation_id=identity['child_task_id'])
        if (record['value'].get('child_invocation') != identity
                or INVOCATION_COMPLETION_FIELD in record['value']):
            raise ValueError('native child checkpoint registration changed or completed')

    def _check(self, request, *, terminal=False):
        if (self.closed or request.principal.require_owner(context='native checkpoint callback') != self.owner
                or request.task_id != self.task_id or request.epoch != self.epoch or request.binding != self.binding
                or self.task.get('id') != self.task_id or type(self.task.get('_userId')) is not int
                or self.task['_userId'] != self.owner):
            raise ValueError('native checkpoint callback ownership changed')
        if self.child_identity is not None and any(self.task.get(key) != self.child_identity[field]
                for key, field in (('convId', 'conversation_id'), ('_parentTaskId', 'parent_task_id'),
                                   ('_archiveScope', 'archive_scope'))):
            raise ValueError('native child checkpoint lineage changed')
        if not terminal and request.cancelled.is_set():
            raise asyncio.CancelledError

    async def _call(self, operation, **fields):
        def invoke():
            if (self.task.get('id') != self.task_id or type(self.task.get('_userId')) is not int
                    or self.task['_userId'] != self.owner):
                raise ValueError('native checkpoint storage ownership changed')
            if self.registered_child:
                self._verify_child(self.storage)
            if operation in {'pending', 'complete'}:
                # A recoverable executable frontier must not outrun accepted
                # checklist/tool presentation facts in the selected authority.
                from lib.tasks_pkg.event_delivery import flush_task_events
                flush_task_events(self.task)
            response = self.storage.native_checkpoint_control(
                operation, dict(format=FORMAT, **fields), user_id=self.owner,
                deadline_unix_ms=time.time_ns() // 1000000 + self.io_timeout_ms,
                response_bytes=self.response_bytes,
            )
            if response.get('accepted') is not True:
                raise ValueError('native checkpoint journal refused: ' + str(response.get('error')))
            return response

        work = asyncio.ensure_future(asyncio.get_running_loop().run_in_executor(self.executor, invoke))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            await _joined(work)
            raise

    async def load(self, request: CheckpointRequest):
        self._check(request)
        if self.creation_started:
            raise ValueError('native checkpoint acquisition is one-shot')
        self.creation_started = True
        if self.capability is None:
            # Join acquisition on cancellation and retain its returned capability
            # before terminal reconciliation. Never acquire a second lease on retry.
            operation = 'recover' if self.recover_abandoned else 'create'
            payload = dict(parameters=self.parameters)
            if self.handoff_from_accepted:
                from lib.conversation_sync.dispatch_contract import read_accepted_resume_lineage
                lineage = await _joined(asyncio.get_running_loop().run_in_executor(
                    self.executor, read_accepted_resume_lineage, self.task))
                self._check(request)
                reserved = self.task.get('_billing_reservation_micro', 0)
                if type(reserved) is not int or not 0 <= reserved <= 9223372036854775807:
                    raise ValueError('native checkpoint handoff reservation invalid')
                operation = 'handoff'
                payload['resume'] = dict(conversation_id=lineage.conversation_id, turn_id=lineage.turn_id,
                    source_attempt_id=lineage.source_attempt_id, target_attempt_id=lineage.attempt_id,
                    source_task_id=lineage.source_task_id, reserved_micro=reserved)
            work = asyncio.create_task(self._call(operation, **payload))
            cancelled = False
            try:
                response = await asyncio.shield(work)
            except asyncio.CancelledError:
                cancelled = True
                response = await _joined(work)
            capability = response.get('capability_id')
            if type(capability) is not int or capability <= 0:
                raise ValueError('native checkpoint acquisition capability invalid')
            self.capability = capability
            if cancelled or request.cancelled.is_set():
                raise asyncio.CancelledError
        response = await self._call('load', capability_id=self.capability)
        state = response['state']
        if state is not None:
            if state.get('tools_pending') is True:
                if not self.supports_pending_tools:
                    raise ValueError('pending native tools require a domain reconciler')
                self.recovery_digest = checkpoint_state_digest(state)
                self.pending_metadata = json.loads(json.dumps(state.get('tool_recovery'), allow_nan=False))
            await self.restore(request, state)
        return state

    async def begin(self, request: CheckpointRequest):
        self._check(request)
        if self.capability is None:
            raise ValueError('native checkpoint lease missing')
        await self._call('begin', capability_id=self.capability, round=request.round)

    async def complete(self, request: CheckpointRequest):
        self._check(request)
        if self.capability is None or not isinstance(request.state, Mapping):
            raise ValueError('native checkpoint completion missing')
        if self.recovery_digest is not None or 'tool_recovery' in request.state:
            raise ValueError('native completion has unresolved or invented tool evidence')
        state = dict(request.state)
        if self.pending_metadata is not None:
            state['tool_recovery'] = self.pending_metadata
        await self._call('complete', capability_id=self.capability, state=state)
        self.pending_metadata = None
        return 'suspend' if self.suspend else 'continue'

    async def pending(self, request: CheckpointRequest):
        self._check(request)
        if (not self.supports_pending_tools or self.capability is None or not isinstance(request.state, Mapping)
                or request.state.get('tools_pending') is not True or 'tool_recovery' in request.state):
            raise ValueError('native pending checkpoint requires a fresh paid tool batch')
        original = checkpoint_state_digest(request.state)
        captured = await self.tool_recovery.capture(request)
        self._check(request)
        if not isinstance(captured, Mapping) or checkpoint_state_digest({
                key: value for key, value in captured.items() if key != 'tool_recovery'}) != original:
            raise ValueError('native tool capture changed paid checkpoint authority')
        state = json.loads(json.dumps(captured, allow_nan=False))
        metadata = state.get('tool_recovery')
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError('native tool recovery metadata must be an object')
        await self._call('pending', capability_id=self.capability, state=state)
        self.pending_metadata = json.loads(json.dumps(metadata, allow_nan=False))
        self._check(request)
        await self.tool_recovery.committed(replace(request, state=state))
        self._check(request)

    async def reconcile(self, request: CheckpointRequest):
        self._check(request)
        if (not self.supports_pending_tools or self.capability is None or self.recovery_digest is None
                or not isinstance(request.state, Mapping)
                or checkpoint_state_digest(request.state) != self.recovery_digest):
            raise ValueError('native tool reconciliation has no exact restored checkpoint')
        result = await self.tool_recovery.reconcile(request)
        self._check(request)
        if checkpoint_state_digest(request.state) != self.recovery_digest:
            raise ValueError('native reconciler changed its restored checkpoint')
        result = reconciled_tool_observations(request.state, result)
        self.recovery_digest = None
        return result

    async def seal(self, request: CheckpointTerminalRequest):
        self._check(request, terminal=True)
        if self.capability is None:
            if self.creation_started:
                raise ValueError('native checkpoint acquisition delivery unknown')
            return 'accepted'  # No acquisition or effects have entered.
        status = request.terminal.event['status']
        await self._call('finish', capability_id=self.capability,
                         status=status, suspended=request.suspended)
        self.sealed = True
        self.paused = request.suspended and status == 'aborted'
        return 'accepted'

    async def close(self, *, terminal_saved: bool = False):
        """After joined execution, release a pause only with canonical commit ACK."""
        if self.closed:
            return
        try:
            if self.capability is not None:
                if self.sealed and self.paused and terminal_saved:
                    await self._call('release', capability_id=self.capability)
                await self._call('forget', capability_id=self.capability)
        finally:
            self.closed = True
            self.restore = None
            self.tool_recovery = None
            self.pending_metadata = None
            self.recovery_digest = None
            self.storage = None
