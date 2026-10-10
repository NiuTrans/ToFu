"""Application callbacks delegate checkpoint transitions to the selected Rust port.

Entry: NativeCheckpointPolicy. Callers supply storage, task, owned executor,
immutable model/tool/account binding and financial/time recovery. No database
open, journal state machine, history cache, worker or automatic takeover.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import Executor
import time

from lib.agent_core.native_process import CheckpointRequest, CheckpointTerminalRequest, _joined
from lib.identity import PrincipalContext
from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES


class NativeCheckpointPolicy:
    def __init__(self, *, principal: PrincipalContext, task: dict, storage,
                 executor: Executor, epoch: str, binding: str, token: str,
                 header_bytes: int, snapshot_bytes: int, identity_bytes: int,
                 io_timeout_ms: int, restore: Callable, suspend: bool = False):
        self.owner = principal.require_owner(context='native checkpoint policy')
        if (type(task.get('_userId')) is not int or task['_userId'] != self.owner
                or not isinstance(task.get('id'), str) or not task['id']
                or task.get('_transientRuntime') or not isinstance(executor, Executor)
                or any(not isinstance(value, str) or not value for value in (epoch, binding, token))
                or any(type(value) is not int or value <= 0 for value in (
                    header_bytes, snapshot_bytes, identity_bytes, io_timeout_ms))
                or not callable(restore) or type(suspend) is not bool
                or not callable(getattr(storage, 'native_checkpoint_control', None))
                or not callable(getattr(storage, 'native_checkpoint_available', None))
                or not storage.native_checkpoint_available(user_id=self.owner)):
            raise ValueError('native checkpoints require owned selected storage, executor and recovery')
        self.task = task
        self.task_id = task['id']
        self.storage = storage
        self.executor = executor
        self.epoch = epoch
        self.binding = binding
        self.suspend = suspend
        self.restore = restore
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

    def _check(self, request, *, terminal=False):
        if (self.closed or request.principal.require_owner(context='native checkpoint callback') != self.owner
                or request.task_id != self.task_id or request.epoch != self.epoch or request.binding != self.binding
                or self.task.get('id') != self.task_id or type(self.task.get('_userId')) is not int
                or self.task['_userId'] != self.owner):
            raise ValueError('native checkpoint callback ownership changed')
        if not terminal and request.cancelled.is_set():
            raise asyncio.CancelledError

    async def _call(self, operation, **fields):
        def invoke():
            if (self.task.get('id') != self.task_id or type(self.task.get('_userId')) is not int
                    or self.task['_userId'] != self.owner):
                raise ValueError('native checkpoint storage ownership changed')
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
        # Join acquisition on cancellation and retain its returned capability
        # before terminal reconciliation. Never acquire a second lease on retry.
        work = asyncio.create_task(self._call('create', parameters=self.parameters))
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
        await self._call('complete', capability_id=self.capability, state=request.state)
        return 'suspend' if self.suspend else 'continue'

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
            self.storage = None
