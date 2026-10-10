"""Canonical leased clock handler for UTC automation occurrences.

Entry point: AutomationClockHandler.registration. The existing worker owns
claims, heartbeat, suspension and terminal accounting. This handler selects one
calendar slot, proves admission through that clock fence, and returns a durable
wake time. It starts no pool/thread and registers no production worker kind.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from lib.durable_worker import (
    WorkerExecutionContext, WorkerJobOutcome, WorkerJobSuspension,
    WorkerKindRegistration, WorkerKindSafety, WorkerLeaseLost,
    WorkerProtocolError,
)
from lib.scheduler.calendar import CronSchedule
from lib.storage.errors import StorageError
from .contract import LIMITS, storage_input_validator, storage_schema
from .errors import AutomationConflict
from .execution_repository import AutomationExecutionRepository
from .publication import clock_job_id
from .transitions import ExecutionFence, occurrence_id


def _utc(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc)


class AutomationClockHandler:
    """One injectable clock adapter; application composition owns its lifetime."""

    def __init__(self, *, client_factory: Callable, now_ms: Callable[[], int]):
        self._factory = client_factory
        self._now_ms = now_ms

    def _clock(self) -> int:
        value = self._now_ms()
        if type(value) is not int or not 0 < value <= LIMITS['safeIntegerMax']:
            raise WorkerProtocolError('Invalid automation clock observation')
        return value

    def _definition(self, context, payload):
        if next(storage_input_validator('clockJob').iter_errors(payload), None):
            raise WorkerProtocolError('Invalid automation clock payload')
        repo = AutomationExecutionRepository(context.principal, client_factory=self._factory)
        if (payload['definitionKey'] != repo._key(payload['definitionId'])
                or context.claim.task_id != clock_job_id(payload['definitionKey'], payload['definitionRevision'])
                or payload['principal'] != context.principal.to_payload()
                or payload['firstDueMs'] <= payload['publishedAtMs']):
            raise WorkerProtocolError('Automation clock identity escaped its definition')
        definition = repo.get('definition', payload['definitionId'])
        if definition is None:
            return repo, None
        value = definition.value
        if (definition.version != payload['definitionRevision']
                or value.get('deleted') or not value['definition']['enabled']
                or value['definition']['trigger']['kind'] != 'schedule'
                or value['triggerState']['clockJobId'] != context.claim.task_id):
            return repo, None
        if (value['compiled']['digest'] != payload['definitionDigest']
                or value['grant']['principal'] != payload['principal']):
            raise WorkerProtocolError('Automation clock immutable binding is inconsistent')
        return repo, definition

    def __call__(self, context: WorkerExecutionContext, payload: Mapping):
        context.checkpoint()
        repo, definition = self._definition(context, payload)
        if definition is None:
            return self.cancel(context, payload)
        now = self._clock()
        after = max(context.replay_cursor, payload['publishedAtMs'])
        rule = definition.value['definition']
        schedule = rule['trigger']
        plan = CronSchedule(schedule['schedule'], schedule['timezone'])
        # A backwards wall clock cannot rewind the durable cursor or dispatch
        # an already consumed slot. The canonical wake still uses UTC.
        through = max(after, now)
        slot = plan.due_slot(_utc(after), _utc(through), catch_up=schedule['catchUp'])
        context.checkpoint()
        if slot is not None and now >= payload['firstDueMs']:
            slot_ms = int(slot.timestamp() * 1000)
            trigger = {'id': f'utc:{slot_ms}', 'kind': 'schedule', 'occurredAt': slot_ms / 1000,
                'origin': 'automation', 'evidence': {'scheduledAt': slot_ms / 1000}, 'automationChain': []}
            if rule.get('projectId'):
                trigger['projectId'] = rule['projectId']
            # Revision edits cannot rewrite an accepted UTC occurrence's
            # original project/evidence. Reuse its frozen producer envelope.
            existing = repo.get('run', occurrence_id(definition.id, trigger))
            if existing is not None:
                frozen = existing.value['trigger']
                if any(frozen.get(key) != trigger[key] for key in
                       ('id', 'kind', 'occurredAt', 'origin', 'evidence', 'automationChain')):
                    raise WorkerProtocolError('Accepted clock occurrence has inconsistent slot evidence')
                trigger = frozen
            repo.admit(definition.id, trigger, now=self._clock() / 1000, producer_fence=ExecutionFence(
                context.claim.task_id, context.claim.worker_id, context.fencing_token))
        context.checkpoint()
        next_slot = plan.next_after(_utc(through))
        if next_slot is None:
            return WorkerJobOutcome('succeeded', result_ref='calendar_exhausted', replay_cursor=through)
        return WorkerJobSuspension(int(next_slot.timestamp() * 1000), replay_cursor=through)

    def cancel(self, context: WorkerExecutionContext, payload: Mapping):
        # Admission is one atomic store command and starts no external effect;
        # accepted runs keep their own queue/lifecycle after this clock retires.
        context.checkpoint(allow_cancelled=True)
        return WorkerJobOutcome('cancelled', replay_cursor=context.replay_cursor)

    def failure(self, context: WorkerExecutionContext, payload: Mapping, error: Exception):
        context.checkpoint()
        if isinstance(error, AutomationConflict):
            _, definition = self._definition(context, payload)
            if definition is None:
                return self.cancel(context, payload)
            proof = self._factory().query('worker_job.claim_state', {
                'task_id': context.claim.task_id, 'worker_id': context.claim.worker_id,
                'fencing_token': context.fencing_token, 'now_ms': self._clock()})
            if not proof.get('ok'):
                raise WorkerLeaseLost('Automation clock admission lost its producer fence')
        if isinstance(error, StorageError) and error.code in {
                'database_timeout', 'database_unavailable', 'database_busy'}:
            retry = storage_schema()['x-clock']['failureRetryMs']
            return WorkerJobSuspension(self._clock() + retry, replay_cursor=context.replay_cursor)
        return WorkerJobOutcome('failed', error={'kind': 'automation_clock_failed'},
            replay_cursor=context.replay_cursor)

    def registration(self) -> WorkerKindRegistration:
        # Explicit composition is required. No import/global registration makes
        # a kind claimable; production also needs the queue-retention lifecycle.
        return WorkerKindRegistration(storage_schema()['x-clock']['taskKind'], self,
            WorkerKindSafety(True, True, True, True), cancel_handler=self.cancel, failure_handler=self.failure)
