"""Explicit action ports over the canonical worker and atomic run repository.

Entry points: ActionAdapter, ExecutionRegistry, AutomationExecutionHandler.
Composition injects adapters, storage, observation time and wait interval. This
module starts no pool or model loop. Dispatch intent precedes every adapter;
unknown writes reconcile under their original effect key rather than replaying.
"""
from copy import deepcopy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from lib.durable_worker import (WorkerDomainSettlementUnknown, WorkerExecutionContext, WorkerJobOutcome,
    WorkerJobSuspension, WorkerKindRegistration, WorkerKindSafety, WorkerLeaseLost,
    WorkerProtocolError)
from .contract import LIMITS, RUNTIME, definition_schema
from .errors import AutomationConflict, AutomationError
from .execution_repository import AutomationExecutionRepository
from .presentation import show_task_start_time
from .registry import ActionContract, builtin_registry
from .transitions import (ExecutionFence, bind_fence, prepare_step, record_failure,
    record_output, request_cancel)


@dataclass(frozen=True)
class ActionInvocation:
    context: WorkerExecutionContext
    arguments: dict
    event: dict
    effect_key: str


@dataclass(frozen=True)
class ActionAdapter:
    contract: ActionContract
    execute: Callable[[ActionInvocation], dict]
    reconcile: Callable[[ActionInvocation], dict | None] | None = None


class ExecutionRegistry:
    """Closed application ports; frozen action versions must match exactly."""

    def __init__(self, adapters):
        ports = {}
        for adapter in adapters:
            if not isinstance(adapter, ActionAdapter) or not callable(adapter.execute):
                raise AutomationError('invalid_action_adapter', 'An action requires an explicit callable port')
            contract = ActionContract.from_document(adapter.contract.document())
            if contract.id in ports:
                raise AutomationError('duplicate_action_adapter', 'An action has more than one execution owner')
            if (adapter.reconcile is not None and not callable(adapter.reconcile)
                    or contract.recovery == 'reconcile' and adapter.reconcile is None):
                raise AutomationError('action_reconciliation_required', 'External actions require a reconciliation port')
            ports[contract.id] = adapter
        self._ports = MappingProxyType(ports)

    def get(self, document):
        contract = ActionContract.from_document(document)
        adapter = self._ports.get(contract.id)
        if adapter is None:
            raise AutomationError('action_adapter_unavailable', 'Action execution is not installed')
        if adapter.contract.document() != document:
            raise AutomationError('action_adapter_version_mismatch', 'The frozen action contract differs from its execution port')
        return adapter


def presentation_registry():
    """Deterministic task-time output; visibility remains the publisher's owner."""
    return ExecutionRegistry([ActionAdapter(builtin_registry().get('task.show_time'),
        lambda invocation: show_task_start_time(invocation.arguments, invocation.event))])


class AutomationExecutionHandler:
    def __init__(self, *, client_factory, registry, now_ms, poll_interval_ms):
        if (not isinstance(registry, ExecutionRegistry) or type(poll_interval_ms) is not int
                or not 0 < poll_interval_ms < LIMITS['safeIntegerMax']):
            raise ValueError('Execution requires an explicit registry and bounded wait interval')
        self._factory = client_factory
        self._registry = registry
        self._now_ms = now_ms
        self._poll_interval_ms = poll_interval_ms

    def _now(self):
        value = self._now_ms()
        if type(value) is not int or not 0 < value <= LIMITS['safeIntegerMax']:
            raise WorkerProtocolError('Invalid automation execution clock')
        return value / 1000

    def _load(self, context, payload):
        if (not isinstance(payload, Mapping) or set(payload) != {'contract', 'runId', 'principal'}
                or payload['contract'] != 'tofu.automation-job/v1'
                or payload['principal'] != context.principal.to_payload()):
            raise WorkerProtocolError('Invalid automation execution payload')
        repo = AutomationExecutionRepository(context.principal, client_factory=self._factory)
        entity = repo.get('run', payload['runId'])
        if (entity is None or entity.value.get('jobId') != context.claim.task_id
                or entity.value.get('principal') != payload['principal']):
            raise WorkerProtocolError('Automation job escaped its immutable run binding')
        if len(entity.value['stepOrder']) > definition_schema()['properties']['steps']['maxItems']:
            raise WorkerProtocolError('Automation step count exceeds its contract')
        fence = ExecutionFence(context.claim.task_id, context.claim.worker_id, context.fencing_token)
        return repo, entity, fence

    def _save(self, repo, entity, document, fence):
        return repo.advance(entity, document, fence, now=self._now())

    def _outcome(self, entity, context):
        state = entity.value['status']
        if state in RUNTIME['terminalRunStates']:
            return WorkerJobOutcome(state, result_ref=entity.id)
        deadline = int(entity.value['deadlineAt'] * 1000)
        wake = int(self._now() * 1000) + self._poll_interval_ms
        if state == 'waiting_permission':
            wake = max(int(self._now() * 1000), deadline)
        return WorkerJobSuspension(wake,
            acknowledged_cancel_sequence=context.cancellation_sequence)

    def _invoke(self, context, run, step_id, *, reconcile=False, cancelling=False):
        step = next(item for item in run['snapshot']['steps'] if item['id'] == step_id)
        adapter = self._registry.get(run['actions'][step['action']])
        for scope in adapter.contract.required_scopes:
            context.principal.require_scope(scope)
        context.checkpoint(allow_cancelled=cancelling)
        port = adapter.reconcile if reconcile else adapter.execute
        if reconcile and port is None and adapter.contract.recovery == 'replay':
            port = adapter.execute
        if port is None:
            return None
        state = run['steps'][step_id]
        invocation = ActionInvocation(context, deepcopy(state['arguments']),
            deepcopy(run['trigger']), state['effectKey'])
        output = port(invocation)
        if output is not None:
            adapter.contract.validate(output, output=True)
        elif not reconcile:
            raise AutomationError('invalid_action_output', 'Dispatch must return an action result')
        return output

    def _execute(self, context, payload, *, cancelling=False):
        context.checkpoint(allow_cancelled=cancelling)
        repo, entity, fence = self._load(context, payload)
        if entity.value['status'] in RUNTIME['terminalRunStates']:
            return self._outcome(entity, context)
        entity = self._save(repo, entity, bind_fence(entity.value, fence, now=self._now()), fence)
        if cancelling:
            entity = self._save(repo, entity, request_cancel(entity.value,
                reason=context.cancellation_reason or 'owner_cancelled', now=self._now()), fence)
        for _ in range(len(entity.value['stepOrder']) + 1):
            context.checkpoint(allow_cancelled=cancelling)
            run = entity.value
            if run['status'] in RUNTIME['terminalRunStates']:
                return self._outcome(entity, context)
            if self._now() >= run['deadlineAt'] and run['status'] != 'cancelling':
                entity = self._save(repo, entity, request_cancel(run,
                    reason='deadline_exceeded', now=self._now()), fence)
                run = entity.value
                if run['status'] in RUNTIME['terminalRunStates']:
                    return self._outcome(entity, context)
            active = next((key for key in run['stepOrder']
                if run['steps'][key]['status'] in RUNTIME['activeEffectStepStates']), None)
            if active is not None:
                output = self._invoke(context, run, active, reconcile=True,
                    cancelling=cancelling or run['status'] == 'cancelling')
                if output is None:
                    return self._outcome(entity, context)
                entity = self._save(repo, entity, record_output(run, active, output, fence, now=self._now()), fence)
                if entity.value['steps'][active]['status'] in RUNTIME['activeEffectStepStates']:
                    return self._outcome(entity, context)
                continue
            prepared, step_id = prepare_step(run, fence, now=self._now())
            entity = self._save(repo, entity, prepared, fence)
            if step_id is None:
                return self._outcome(entity, context)
            # The persisted dispatch intent and effect identity now precede I/O.
            output = self._invoke(context, entity.value, step_id)
            context.checkpoint()
            entity = self._save(repo, entity, record_output(entity.value, step_id, output, fence, now=self._now()), fence)
            if entity.value['steps'][step_id]['status'] in RUNTIME['activeEffectStepStates']:
                return self._outcome(entity, context)
        return self._outcome(entity, context)

    def __call__(self, context, payload):
        try:
            return self._execute(context, payload)
        except AutomationConflict:
            # Ownership commands may advance the row while this claim reads
            # or settles it. Preserve its effect intent and reacquire immediately.
            context.checkpoint(allow_cancelled=True)
            return WorkerJobSuspension(int(self._now()*1000), replay_cursor=context.replay_cursor)

    def cancel(self, context, payload):
        try:
            return self._execute(context, payload, cancelling=True)
        except AutomationConflict:
            context.checkpoint(allow_cancelled=True)
            return WorkerJobSuspension(int(self._now()*1000), replay_cursor=context.replay_cursor)

    def failure(self, context, payload, error):
        context.checkpoint(allow_cancelled=context.cancellation_requested)
        if isinstance(error, WorkerLeaseLost):
            raise error
        try:
            repo, entity, fence = self._load(context, payload)
            if entity.value['status'] in RUNTIME['terminalRunStates']:
                return self._outcome(entity, context)
            run = entity.value
            step_id = next((key for key in run['stepOrder']
                if run['steps'][key]['status'] in RUNTIME['activeEffectStepStates']), None)
            if step_id is None:
                raise WorkerDomainSettlementUnknown('Automation transition did not confirm a settled effect')
            step = next(item for item in run['snapshot']['steps'] if item['id'] == step_id)
            uncertain = run['actions'][step['action']]['recovery'] == 'reconcile'
            if isinstance(error, AutomationError) and error.code in {
                    'action_adapter_unavailable', 'action_adapter_version_mismatch'}:
                uncertain = False
            failure = {'code': error.code if isinstance(error, AutomationError) else 'action_execution_failed',
                'message': str(error)[:4000] if isinstance(error, AutomationError) else 'Action execution result is unavailable'}
            entity = self._save(repo, entity, record_failure(run, step_id, failure, fence,
                now=self._now(), uncertain=uncertain), fence)
            return self._outcome(entity, context)
        except WorkerDomainSettlementUnknown:
            raise
        except AutomationConflict:
            context.checkpoint(allow_cancelled=True)
            return WorkerJobSuspension(int(self._now()*1000), replay_cursor=context.replay_cursor)
        except Exception as exc:
            raise WorkerDomainSettlementUnknown('Automation failure settlement remains unconfirmed') from exc

    def registration(self):
        return WorkerKindRegistration('automation-run', self, WorkerKindSafety(True, True, True, True),
            cancel_handler=self.cancel, failure_handler=self.failure)
