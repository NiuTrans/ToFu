"""Exact file-operation adapter for the existing native checkpoint callbacks.

Entry: NativeFileToolRecovery, bound by run_native_task to its owned gateway.
The gateway owns prepared occurrences; Turn/file authorities own proof; the
shared budget owns consumption. This adapter adds no journal or execution loop.
Automatic recovery admits only complete write_file batches. Trusted composition
may allow other live batches; their interrupted outcome remains unknown and
refuses replay. Unknown or partial proof never authorizes a tool execution.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
import json
import threading

from lib.agent_core.native_process import checkpoint_state_digest, _joined
from lib.identity import PrincipalContext


class NativeFileToolRecovery:
    def __init__(self, *, principal: PrincipalContext, task: dict, executor: Executor,
                 registered_child: bool = False, storage=None,
                 allow_unreconciled_batches: bool = False):
        from lib.conversation_sync.attempt_identity import is_conversation_attempt

        self.owner = principal.require_owner(context="native file recovery")
        if (
            type(task.get("_userId")) is not int
            or task["_userId"] != self.owner
            or not task.get("id")
            or not task.get("convId")
            or type(registered_child) is not bool
            or type(allow_unreconciled_batches) is not bool
            or (not registered_child and not is_conversation_attempt(task))
            or (registered_child and (not task.get('_transientRuntime')
                or task.get('_invocationStorageFree') is not False or not task.get('_archiveScope')))
            or not isinstance(executor, Executor)
        ):
            raise ValueError(
                "native file recovery requires an owned durable ROOT attempt"
            )
        self.task, self.task_id, self.executor = task, task["id"], executor
        self.registered_child = registered_child
        self.allow_unreconciled_batches = allow_unreconciled_batches
        self.storage = storage
        self.conversation_id, self.turn_id = task["convId"], task.get("_turnId")
        self.gateway = None
        self.batch = None
        self.capture_digest = None
        self.reconciliation_started = False
        self.consumption_proved = False
        self.lock = threading.Lock()
        self.effect_origin = None

    def bind_effect_origin(self, origin):
        """Bind native-acknowledged provenance before the child policy is loaded."""
        if (not self.registered_child or self.effect_origin is not None
                or self.reconciliation_started or not isinstance(origin, dict)
                or set(origin) != {'task_id', 'parent_task_id'}
                or any(not isinstance(value, str) or not value for value in origin.values())):
            raise ValueError('Recovered effect origin requires an unused registered adapter')
        self.effect_origin = dict(origin)

    def bind_checkpoint_storage(self, storage):
        """Use the exact checkpoint authority supplied by native composition."""
        if self.storage is not None and self.storage is not storage:
            raise ValueError('native file recovery checkpoint authority changed')
        self.storage = storage

    def bind_gateway(self, gateway):
        if (
            self.gateway is not None
            or gateway.task is not self.task
            or gateway.executor is not self.executor
            or gateway.owner != self.owner
            or gateway._closed
            or "write_file" not in gateway.permissions
        ):
            raise ValueError(
                "native file recovery requires its exact write_file gateway"
            )
        self.gateway = gateway

    def _check(self, request):
        if (
            self.gateway is None
            or self.gateway._closed
            or request.principal.require_owner(context="native file recovery callback")
            != self.owner
            or request.task_id != self.task_id
            or self.task.get("id") != self.task_id
            or type(self.task.get("_userId")) is not int
            or self.task["_userId"] != self.owner
            or self.task.get("status") not in {"pending", "running"}
            or self.task.get("convId") != self.conversation_id
            or self.task.get("_turnId") != self.turn_id
        ):
            raise ValueError("native file recovery ownership changed")
        if request.cancelled.is_set() or self.task["abort_event"].is_set():
            raise asyncio.CancelledError

    async def _call(self, request, function):
        def invoke():
            self._check(request)
            result = function()
            self._check(request)
            return result

        work = asyncio.ensure_future(
            asyncio.get_running_loop().run_in_executor(self.executor, invoke)
        )
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            await _joined(work)
            raise

    def _calls(self, state):
        from lib.tasks_pkg.native_file_evidence import file_recovery_calls
        return file_recovery_calls(state)

    def _account(self, state):
        account = self.task.get("_executionBudget")
        if account is not None:
            saved = state.get("shared_model_account")
            if (not isinstance(saved, dict)
                    or saved.get('actor_task_id', saved.get("root_task_id")) != self.task_id
                    or ('actor_task_id' in saved) != self.registered_child
                    or (self.registered_child and saved['root_task_id'] != account.root_id)):
                raise ValueError(
                    "native file recovery requires explicit quiescent shared tool accounting"
                )
            account.validate_pending_tool_checkpoint(
                saved, owner=self.owner, task=self.task
            )
        return account

    async def capture(self, request):
        def capture():
            from lib.conversation_sync.validation import decode
            from lib.conversation_sync.turn_images import turn_image_owner_scope

            self._account(request.state)
            if self.allow_unreconciled_batches:
                from lib.tasks_pkg.native_file_evidence import pending_tool_calls
                calls = pending_tool_calls(request.state)
                if any(call['function']['name'] != 'write_file' for call in calls):
                    # Preserve the paid frontier without inventing domain proof.
                    # Normal gateway authorization still owns every live effect.
                    self.batch = None
                    self.capture_digest = checkpoint_state_digest(request.state)
                    return dict(request.state)
            calls = self._calls(request.state)
            self.batch = self.gateway.prepare_effects(request.round, calls)
            records = []
            for _, call_id, number, operation in self.batch.identities():
                reference = decode(
                    "InvocationToolEffectInspectionReference" if self.registered_child else "ToolEffectInspectionReference",
                    dict(
                        conversationId=self.conversation_id,
                        **({'invocationId': self.task_id} if self.registered_child else {'turnId': self.turn_id}),
                        operationId=operation,
                        ownerScope=turn_image_owner_scope(
                            self.owner, self.conversation_id
                        ),
                    ),
                )
                records.append(dict(call_id=call_id, inspection=reference, tool_round=number))
            metadata = dict(
                format="tofu.native-file-tool-recovery/v1",
                producer_task_id=self.task_id,
                calls=records,
            )
            if (
                len(json.dumps(metadata, separators=(",", ":")).encode())
                + self.gateway.response_retained_bytes
                > self.gateway.pending_bytes
            ):
                raise ValueError(
                    "native file recovery exceeds the owned pending-byte budget"
                )
            state = dict(request.state, tool_recovery=metadata)
            self.capture_digest = checkpoint_state_digest(state)
            return state

        return await self._call(request, capture)

    async def committed(self, request):
        def arm():
            if (
                (self.batch is None and not self.allow_unreconciled_batches)
                or self.capture_digest is None
                or checkpoint_state_digest(request.state) != self.capture_digest
            ):
                raise ValueError(
                    "native file operation binding changed before acknowledgement"
                )
            if self.batch is not None:
                self.batch.arm()
            self.capture_digest = None

        await self._call(request, arm)

    async def reconcile(self, request):
        def reconcile():
            with self.lock:
                if self.reconciliation_started:
                    raise ValueError("native file batch reconciliation is one-shot")
                self.reconciliation_started = True
            try:
                return self._reconcile_batch(request)
            except BaseException as failure:
                account = self.task.get("_executionBudget")
                if account is not None and not self.consumption_proved:
                    try:
                        if self.registered_child:
                            account.refuse_recovered_actor_tools(owner=self.owner, task=self.task)
                        else:
                            account.refuse_pending_tool_recovery(
                                request.state["shared_model_account"],
                                owner=self.owner,
                                task=self.task,
                            )
                    except Exception as marking_error:
                        failure.add_note(
                            "Pending tool accounting refusal failed: "
                            + type(marking_error).__name__
                        )
                raise

        return await self._call(request, reconcile)

    def _reconcile_batch(self, request):
        if self.allow_unreconciled_batches:
            from lib.tasks_pkg.native_file_evidence import pending_tool_calls
            if any(call['function']['name'] != 'write_file'
                   for call in pending_tool_calls(request.state)):
                raise ValueError('Native tool batch outcome is unknown; automatic replay is not permitted')
        if self.registered_child:
            return self._reconcile_child_batch(request)
        from lib.tasks_pkg.native_file_evidence import file_recovery_batch
        from lib.conversation_sync.repository import SidecarConversationSyncRepository
        from lib.conversation_sync.service import ConversationSyncService

        metadata = request.state.get("tool_recovery") or {}
        producer = metadata.get("producer_task_id")
        calls, references = file_recovery_batch(request.state, owner=self.owner,
            conversation_id=self.conversation_id, producer_task_id=producer, turn_id=self.turn_id)
        account = self._account(request.state)
        producer = metadata["producer_task_id"]
        if producer != self.task_id:
            from lib.conversation_sync.dispatch_contract import validate_resume_producer

            validate_resume_producer(
                self.task,
                producer,
                byte_limit=self.gateway.pending_bytes,
                check_cancelled=lambda: self._check(request),
            )
        service = ConversationSyncService(SidecarConversationSyncRepository())
        observations = []
        for call, reference in zip(calls, references):
            self._check(request)
            result = service.inspect_tool_effect(
                self.conversation_id,
                self.turn_id,
                self.owner,
                operation_id=reference["operationId"],
                producer_task_id=producer,
            )
            if result["outcome"] not in {"file_published", "file_not_published"}:
                raise ValueError(
                    "File operation remains unresolved: " + reference["operationId"]
                )
            observations.append(
                dict(
                    call_id=call["id"],
                    content=json.dumps(
                        result, ensure_ascii=False, separators=(",", ":")
                    ),
                )
            )
        self._check(request)
        if account is not None:
            account.reconcile_pending_tools(
                request.state["shared_model_account"],
                owner=self.owner,
                task=self.task,
                count=len(calls),
            )
        return self._finish_reconciliation(request, calls, observations)

    def _reconcile_child_batch(self, request):
        from lib.tasks_pkg.native_file_evidence import file_recovery_batch, file_recovery_occurrences
        from lib.tasks_pkg.invocation_effects import inspect_child_effect_batch
        state = request.state
        if state.get('tools_pending') is not True:
            raise ValueError('child file reconciliation requires a pending checkpoint')
        producer = (state.get('tool_recovery') or {}).get('producer_task_id')
        calls, references = file_recovery_batch(state, owner=self.owner,
            conversation_id=self.conversation_id, producer_task_id=producer)
        account = self.task.get('_executionBudget')
        if account is None or self.storage is None:
            raise ValueError('child reconciliation requires its selected authority and recovered account')
        saved = state['shared_model_account']
        source = account.recovered_actor_source(self.task, owner=self.owner)
        origin = self.effect_origin or source
        if producer != origin['task_id']:
            raise ValueError('Recovered file producer differs from its acknowledged origin')
        proof = account.verify_recovered_actor_pending_tools(saved, owner=self.owner, task=self.task,
            source_task_id=source['task_id'], count=len(calls), counter_policy=saved['counter_policy'])
        self._check(request)
        observed = inspect_child_effect_batch(storage=self.storage, user_id=self.owner,
            conversation_id=self.conversation_id, invocation_id=producer,
            registration_version=None, expected_parent_task_id=origin['parent_task_id'],
            checkpoint_batch_unentered=proof['charged_tool_calls']==0,
            occurrences=file_recovery_occurrences(state, references))
        if any(row['result']['outcome'] not in {'file_published', 'file_not_published'} for row in observed):
            raise ValueError('recovered child file operation remains unresolved')
        self._check(request)
        account.reconcile_recovered_actor_tools(saved, owner=self.owner, task=self.task,
            source_task_id=source['task_id'], count=len(calls))
        observations = [dict(call_id=row['call_id'],content=json.dumps(row['result'],
            ensure_ascii=False,separators=(',', ':'))) for row in observed]
        return self._finish_reconciliation(request, calls, observations)

    def _finish_reconciliation(self, request, calls, observations):
        self.consumption_proved = True
        if self.gateway._progress is not None:
            for call, observation in zip(calls, observations):
                self.gateway._progress.record(
                    request.round, call, observation["content"]
                )
        return observations
