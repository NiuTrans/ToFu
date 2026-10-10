"""Native tool callbacks over the canonical application dispatch pipeline.

Entry: NativeGateway. Explicit task/principal, prepared catalog/config, model
round source and owned executor are borrowed. Existing parser, approval gate,
hooks, resolver, result budgeting and settlement remain their current owners.
No direct handler lookup, second executor, model loop or storage path is created.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Executor
import json
import time
import uuid

from lib.agent_core.native_process import ApprovalRequest, ApprovalPolicy, ToolRequest
from lib.identity import PrincipalContext


class NativeGateway:
    def __init__(self, *, principal: PrincipalContext, task: dict, executor: Executor,
                 configuration: dict, tool_list: list, permissions: dict,
                 project_path: str | None, project_enabled: bool, model: str,
                 round_source, decision_policy: ApprovalPolicy | None,
                 pending_calls: int, pending_bytes: int, multimodal: bool = False):
        if type(multimodal) is not bool:
            raise ValueError('native gateway media admission must be explicit')
        self.multimodal=multimodal
        self.owner = principal.require_owner(context='native application gateway')
        if (type(task.get('_userId')) is not int or task['_userId'] != self.owner
                or not isinstance(task.get('id'), str) or not task['id']
                or not isinstance(executor, Executor) or (round_source is not None and not callable(round_source))
                or type(pending_calls) is not int or pending_calls <= 0
                or type(pending_bytes) is not int or pending_bytes <= 0):
            raise ValueError('native gateway requires explicit owned bounded composition')
        if any(permission not in {'read', 'write', 'approval'} for permission in permissions.values()):
            raise ValueError('native gateway permission is not declared')
        declared = {item.get('function', item).get('name') for item in tool_list if isinstance(item, dict)}
        if not set(permissions).issubset(declared):
            raise ValueError('native gateway names are not in the prepared application catalog')
        if any(permission != 'read' for permission in permissions.values()) and decision_policy is None:
            raise ValueError('native gateway non-read tools require existing decision policy')
        self.task, self.task_id, self.executor = task, task['id'], executor
        # Freeze trusted policy/catalog input without retaining caller containers.
        self.configuration = json.dumps(configuration, allow_nan=False)
        self.tool_list = json.dumps(tool_list, allow_nan=False)
        self.permissions = dict(permissions)
        self.project_path, self.project_enabled, self.model = project_path, project_enabled, model
        self.round_source, self.decision_policy = round_source, decision_policy
        self.pending_calls, self.pending_bytes = pending_calls, pending_bytes
        self._pending, self._bytes = {}, 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._progress = None

    def enable_progress(self, limits, feedback_bytes):
        if self._closed or self._progress is not None:
            raise ValueError('native gateway progress already bound or closed')
        from lib.tasks_pkg.native_progress import NativeGatewayProgress
        self._progress = NativeGatewayProgress(self, limits, feedback_bytes)
        return self._progress

    def callbacks(self):
        return {name: self.execute for name in self.permissions}

    def _identity(self, request):
        if (self._closed or request.principal.require_owner(context='native application tool') != self.owner
                or request.task_id != self.task_id or self.task.get('id') != self.task_id
                or type(self.task.get('_userId')) is not int or self.task['_userId'] != self.owner
                or self.task.get('status') not in {'pending', 'running'}
                or request.name not in self.permissions):
            raise ValueError('native application tool ownership changed')

    @staticmethod
    def _proof(name, arguments, caller):
        return json.dumps([name, arguments, caller], ensure_ascii=False,
                          sort_keys=True, allow_nan=False, separators=(',', ':')).encode()

    async def decide(self, request: ApprovalRequest):
        self._identity(request)
        if self.permissions[request.name] == 'read' or self.decision_policy is None:
            raise ValueError('native approval is not an application write')
        proof = self._proof(request.name, request.arguments, request.caller)
        if (request.call_id in self._pending or len(self._pending) >= self.pending_calls
                or self._bytes + len(proof) > self.pending_bytes):
            raise ValueError('native application approval budget exhausted')
        # Reserve before awaiting policy so concurrent cold callers cannot
        # overbook retained approval payloads.
        self._pending[request.call_id] = (proof, None, 0)
        self._bytes += len(proof)
        try:
            decision = await self.decision_policy.decide(request)
            self._identity(request)
            if request.cancelled.is_set():
                decision = 'denied'
            if decision == 'denied':
                return decision
            from lib.tasks_pkg.tool_dispatch._approval import _TOOL_APPROVAL_RECEIPT_AUTHORITIES
            if decision not in _TOOL_APPROVAL_RECEIPT_AUTHORITIES:
                raise ValueError('application approval authority is unknown')
            if self._pending.get(request.call_id) != (proof, None, 0):
                raise ValueError('native application approval reservation changed')
            self._pending[request.call_id] = (proof, decision, time.time())
            return decision
        finally:
            reservation = self._pending.get(request.call_id)
            if reservation is not None and reservation[1] is None:
                self._pending.pop(request.call_id)
                self._bytes -= len(proof)

    async def execute(self, request: ToolRequest):
        self._identity(request)
        async with self._lock:
            self._identity(request)
            approved = None
            if self.permissions[request.name] != 'read':
                approved = self._pending.pop(request.call_id, None)
                if approved is not None:
                    self._bytes -= len(approved[0])
                if (approved is None or approved[1] is None
                        or approved[0] != self._proof(request.name, request.arguments, request.caller)):
                    raise ValueError('application gateway has no exact unused approval')
            # The native host owns callback cancellation/result joining. This
            # future owns the actual canonical pipeline work through completion.
            work = asyncio.get_running_loop().run_in_executor(
                self.executor, self._execute, request, approved)
            stop = asyncio.create_task(request.cancelled.wait()) if request.cancelled is not None else None
            cancelled = False
            try:
                if stop is not None:
                    done, _ = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
                    if stop in done:
                        self._stop()
                while True:
                    try:
                        result = await asyncio.shield(work)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                        self._stop()
                        if work.done():
                            result = work.result()
                            break
                if cancelled:
                    raise asyncio.CancelledError
                return result
            finally:
                if stop is not None:
                    stop.cancel()
                if not work.done():
                    self._stop()
                    while not work.done():
                        try:
                            await asyncio.shield(work)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                if stop is not None:
                    await asyncio.gather(stop, return_exceptions=True)

    def _stop(self):
        # Task/tool contexts already observe these existing owner stop fields.
        self.task['aborted'] = True
        self.task.setdefault('_abort_timestamp', time.time())
        self.task.setdefault('_abort_source', 'native_parent_stop')
        event = self.task.get('abort_event')
        if event is not None:
            event.set()

    def close(self):
        """Dispose ephemeral proofs after the native client has joined callbacks."""
        self._closed = True
        self._pending.clear()
        self._bytes = 0
        if self._progress is not None:
            self._progress.close()

    def _execute(self, request, approved):
        self._identity(request)
        from lib.tasks_pkg.tool_dispatch.api import parse_tool_calls, execute_tool_pipeline
        from lib.tasks_pkg.tool_dispatch._approval import mint_approval_receipt

        round_num = request.model_round
        if round_num is None and self.round_source is not None:
            round_num = self.round_source()
        if type(round_num) is not int or round_num < 0:
            raise ValueError('native gateway requires factual model round identity')
        identity = f'native-{uuid.uuid4().hex}'
        call = {'id': identity, 'type': 'function', 'function': {
            'name': request.name, 'arguments': json.dumps(dict(request.arguments), allow_nan=False),
        }}
        if request.caller is not None:
            call['caller'] = request.caller
        assistant = {'role': 'assistant', 'content': '', 'tool_calls': [call]}
        parsed, _ = parse_tool_calls(assistant, self.task, round_num,
                                    len(self.task.get('toolRounds') or []), self.project_enabled)
        if len(parsed) != 1:
            raise RuntimeError('native gateway parser did not retain the occurrence')
        parsed_id = parsed[0][2]
        if approved is not None:
            if not mint_approval_receipt(self.task, request.name, parsed_id,
                                         dict(request.arguments), authority=approved[1]):
                raise RuntimeError('application approval receipt could not bind')
            self.task['_tool_approval_receipts'][parsed_id]['minted_at'] = approved[2]
        messages = [assistant]
        execute_tool_pipeline(self.task, parsed, json.loads(self.configuration),
            self.project_path, self.project_enabled, json.loads(self.tool_list),
            messages, [], round_num, self.model, publish_direct_gateway_siblings=False)
        results = [item for item in messages if item.get('role') == 'tool']
        if len(results) != 1 or results[0].get('tool_call_id') != parsed_id:
            raise RuntimeError('canonical gateway did not settle one exact tool result')
        content = results[0].get('content')
        if not isinstance(content, str):
            if not self.multimodal:
                raise ValueError('native feedback requires a declared multimodal adapter')
            from lib.agent_core.native_process import validate_model_content
            validate_model_content(content)
        if self._progress is not None:
            self._progress.record(round_num, call, content)
        return content
