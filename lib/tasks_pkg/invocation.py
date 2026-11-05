"""Synchronous agent invocation policy, independent of scheduler and transport.

Root and child agents enter the mature preparation/request/tool/settlement
owner. A child owns its task and resources, never its parent's conversation
lifecycle. The scheduler supplies scoped tools and a required evidence sink.
Silence is judged for every agent by ``lib.tasks_pkg.liveness``; its stall
verdict reaches an invocation through ``stop_stalled_invocation``.
"""
from __future__ import annotations

from contextvars import copy_context
from dataclasses import dataclass
from typing import Any
import math
import time
from lib.log import clear_log_context, get_logger, set_req_id

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InvocationPolicy:
    """Explicit lifecycle authority; model/capabilities remain task-local."""

    parent_task_id: str | None = None
    kickoff: bool = True
    timeout_seconds: float = 0
    max_429_attempts: int | None = None

    @property
    def is_child(self) -> bool:
        return self.parent_task_id is not None

    def validate(self) -> None:
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds < 0):
            raise ValueError('Invocation timeout must be finite and nonnegative')
        if self.max_429_attempts is not None and (
                type(self.max_429_attempts) is not int or not 1 <= self.max_429_attempts <= 4096):
            raise ValueError('Invocation provider retry allowance must be 1..4096')
        if self.parent_task_id is not None and (
                not isinstance(self.parent_task_id, str) or not self.parent_task_id.strip()):
            raise ValueError('Invocation creator identity must be a nonempty string')
        if type(self.kickoff) is not bool:
            raise ValueError('Invocation kickoff policy must be boolean')

    @classmethod
    def child(cls, parent_task_id: str, *, timeout_seconds: float = 0,
              max_429_attempts: int | None = None) -> 'InvocationPolicy':
        return cls(parent_task_id=parent_task_id, kickoff=False,
                   timeout_seconds=timeout_seconds, max_429_attempts=max_429_attempts)


ROOT_INVOCATION_POLICY = InvocationPolicy()


def is_child_invocation(task: dict) -> bool:
    policy = task.get('_invocationPolicy')
    return isinstance(policy, InvocationPolicy) and policy.is_child


def _stop_internal_invocation(task: dict, reason: str, *,
                              silent_seconds: float | None = None,
                              stall_timeout: float | None = None) -> bool:
    """Publish cause before the physical abort wakes provider/tool consumers."""
    from lib.error_envelope import make_envelope
    detail = ('Invocation stalled: no model or tool progress was observed'
              if reason == 'stalled' else 'Invocation execution deadline exceeded')
    if silent_seconds is not None and stall_timeout is not None:
        # Numeric liveness facts only: notes, prompts and provider payloads are
        # deliberately excluded from public diagnostics.
        task['_invocationStopEvidence'] = {
            'silentSeconds': silent_seconds, 'stallTimeoutSeconds': stall_timeout}
        detail += f' for {silent_seconds:.1f}s (stall threshold {stall_timeout:g}s)'
    task['_invocationStopReason'] = reason
    task['error'] = make_envelope('internal', detail=detail,
                                  context='invocation-guard', source='task-engine',
                                  extensions={'invocationStopReason': reason})
    task['aborted'] = True
    task['abort_event'].set()
    return True


def stop_stalled_invocation(task: dict, *, silent_seconds: float,
                            stall_timeout: float) -> bool:
    """Deliver the liveness judge's stall verdict in band; False if already stopping.

    Explicit Stop and deadlines are observed first and keep their own cause, so
    a silence verdict can never impersonate or overwrite an owner stop.
    """
    if invocation_abort_requested(task):
        return False
    return _stop_internal_invocation(task, 'stalled', silent_seconds=float(silent_seconds),
                                     stall_timeout=float(stall_timeout))


def invocation_abort_requested(task: dict) -> bool:
    """Observe inherited cancellation without granting upward cancellation rights.

    Silence is not judged here: ``lib.tasks_pkg.liveness`` decides it for root
    and child agents alike and delivers a stall through
    ``stop_stalled_invocation``.
    """
    if task.get('aborted') or (task.get('abort_event') is not None
                              and task['abort_event'].is_set()):
        task['aborted'] = True
        return True
    probe = task.get('_invocationAbortCheck')
    if callable(probe):
        try:
            requested = bool(probe())
        except Exception as exc:
            logger.warning('Child %s lost its owner control port: %s', task.get('id'), exc)
            task['_invocationDeliveryError'] = exc
            requested = True  # Lost owner control fails closed.
        if requested:
            task['aborted'] = True
            task['abort_event'].set()
            return True
    limits = (task.get('config') or {}).get('executionLimits') or {}
    account = task.get('_executionBudget')
    account_deadline = (getattr(account, 'limits', {}) or {}).get('deadline')
    if any(deadline is not None and time.time() >= deadline
           for deadline in (limits.get('deadline'), account_deadline)):
        task['_executionLimitReason'] = 'execution_deadline_exceeded'
        return _stop_internal_invocation(task, 'deadline')
    deadline = task.get('_invocationDeadline')
    if deadline is not None and time.monotonic() >= deadline:
        return _stop_internal_invocation(task, 'deadline')
    return bool(task.get('aborted'))


def deliver_invocation_event(task: dict, event: dict) -> None:
    """Required, ordered handoff, unlike a best-effort observer notification.

    A child's Request Inspector snapshot is accepted into its ordered evidence
    lane and the sink receives it at once: the paid call never waits for it.
    """
    beacon = task.get('_invocationProgressBeacon')
    if beacon is not None:
        beacon.touch(task.get('_invocationProgressKey') or task['id'], str(event.get('type') or 'event'))
    sink = task.get('_invocationEventSink')
    if not callable(sink):
        return
    previous = task.get('_invocationDeliveryError')
    if previous is not None:
        raise RuntimeError('Invocation evidence delivery previously failed') from previous
    try:
        if event.get('type') == 'messages_snapshot' and not task.get('_invocationStorageFree'):
            _accept_invocation_evidence(task, event)
        sink(event)
    except Exception as exc:
        task['_invocationDeliveryError'] = exc
        task['aborted'] = True
        task['abort_event'].set()
        raise


def _accept_invocation_evidence(task: dict, event: dict) -> None:
    """Queue one snapshot append behind the child's earlier Inspector evidence."""
    from lib.conversation_sync.settled_rows import detach_sharing_settled
    from lib.tasks_pkg.event_delivery import coordinator, invocation_evidence_lane
    inspector_task_id = task['_inspectorTaskId']
    sequence = event['seq']
    # Detached like a root-lane frame, copied and charged in one visit: later
    # prompt mutation cannot alter it.
    detached, retained_bytes = detach_sharing_settled(event)
    pending = {'event': detached, 'projected': False}

    def persist():
        from lib.tasks_pkg.event_log import (
            append_persistent_event, project_persistent_event,
        )
        # Projection advances the snapshot codec in this lane's FIFO order and
        # failure may reset its baseline. Freeze the occurrence once: a lost
        # ACK must replay the original delta, never a new payload under the
        # already-committed (task, sequence) key.
        if not pending['projected']:
            pending['event'] = project_persistent_event(inspector_task_id, pending['event'])
            pending['projected'] = True
        append_persistent_event(inspector_task_id, sequence, pending['event'])

    # The lane retries transient pressure on this exact append; Stop halts
    # execution, not persistence of already-observed evidence. Sequence -1:
    # the child's own event cursor is not a public replay frame.
    coordinator().submit(invocation_evidence_lane(task), -1,
                         retained_bytes, persist, lambda: None,
                         accept_after_stop=True)


def settle_invocation_evidence(task: dict) -> None:
    """Drain a finished child's accepted Inspector evidence, then release its lane.

    Accepted evidence outlives Stop, so this waits without a cancellation check;
    only shutdown ends a still-retrying head. A failed head is retried once
    more; a refusal becomes the invocation's delivery failure with the authority
    verdict, and the unrepairable remainder is released.
    """
    lane = task.get('_invocationEvidenceLane')
    if lane is None:
        return
    from lib.tasks_pkg.event_delivery import EventDeliveryError, coordinator
    owner = coordinator()
    try:
        owner.flush(lane, timeout=None)
    except Exception as error:
        # Never raised: a teardown caller may already be propagating the
        # invocation's own failure. The barrier error only wraps the verdict.
        failure = (error.__cause__ if isinstance(error, EventDeliveryError)
                   and error.__cause__ is not None else error)
        if task.get('_invocationDeliveryError') is None:
            task['_invocationDeliveryError'] = failure
        task['aborted'] = True
        task['abort_event'].set()
        owner.revoke(lane)
    task.pop('_invocationEvidenceLane', None)


def invocation_affinity_id(task: dict) -> str:
    """Children own routing cache affinity; convId remains the ownership link."""
    if is_child_invocation(task):
        return f'invocation:{task["_userId"]}:{task["id"]}'
    return task.get('convId') or ''


def execute_invocation(task: dict[str, Any], *, policy: InvocationPolicy) -> None:
    """Validate once, then enter the mature body inside the owned lane scope."""
    try:
        return _execute_invocation(task, policy=policy)
    finally:
        # copy_context isolates task teardown from its caller. A root owns a
        # pooled lane and must also retire that caller's stale correlation;
        # nested children retain their parent's request/task context.
        if not policy.is_child:
            set_req_id('')
            clear_log_context()


def _execute_invocation(task: dict[str, Any], *, policy: InvocationPolicy) -> None:
    from lib.tasks_pkg.orchestrator._run import _execute_task_invocation
    policy.validate()
    from lib.agent_core.execution_limits import validate_execution_limits
    validate_execution_limits((task.get('config') or {}).get('executionLimits'))

    from lib.project_dispatch.admission import validate_task
    validate_task(task)
    if not task.get('id'):
        raise ValueError("run_task called with a task dict missing 'id' — use create_task()")
    if not policy.is_child and (is_child_invocation(task) or task.get('_parentTaskId')):
        raise ValueError('A child cannot acquire root invocation authority')
    if policy.is_child:
        if (task['id'] == policy.parent_task_id
                or task.get('_parentTaskId') != policy.parent_task_id
                or not callable(task.get('_invocationEventSink'))
                or not callable(task.get('_invocationAbortCheck'))
                or not isinstance(task.get('_invocationTools'), list)
                or task.get('_turnId') or task.get('_attemptId')
                or task.get('_flowLiveTurn') or task.get('_flowRootAttemptId')):
            raise ValueError('Child requires its own carrier and task-local evidence port')
    task['_invocationPolicy'] = policy
    if policy.timeout_seconds:
        task.setdefault('_invocationDeadline', time.monotonic() + policy.timeout_seconds)
    # Every authorized agent owns an isolated execution context. Creator
    # provenance is not an optional-side-call billing or resilience policy.
    from lib.key_stats._admission import _STRICT_BILLING_STOP_ADMISSION
    from lib.llm_dispatch.conv_affinity import isolated_conv_affinity

    def run_in_scope():
        _STRICT_BILLING_STOP_ADMISSION.set(False)
        from lib.token_counter.native import native_token_scope
        native = task.get('_nativePreparedInvocation')
        if native is None:
            return _execute_task_invocation(task, policy=policy)
        from lib.tasks_pkg.native_invocation import NativePreparedInvocation
        if (not isinstance(native, NativePreparedInvocation)
                or type(task.get('_userId')) is not int
                or task['_userId'] != native.principal.require_owner(context='native token invocation')):
            raise ValueError('native token scope requires the task-owned invocation')
        host = native.host
        # Background preparation has no Quart app context. Borrow the issued
        # host before the first context count; reset it even on fatal teardown.
        with native_token_scope(host):
            return _execute_task_invocation(task, policy=policy)

    try:
        # Only a child's scope is ephemeral: a root scope IS the conversation,
        # so its recorded warm key must survive the turn end.
        with isolated_conv_affinity(invocation_affinity_id(task),
                                    ephemeral=policy.is_child):
            invocation_abort_requested(task)
            return copy_context().run(run_in_scope)
    finally:
        if task.get('_swarm_inject_pending'):
            from lib.swarm.delivery import restore_unconsumed_results
            restore_unconsumed_results(task)


__all__ = ['InvocationPolicy', 'ROOT_INVOCATION_POLICY', 'execute_invocation',
           'is_child_invocation', 'deliver_invocation_event',
           'settle_invocation_evidence', 'stop_stalled_invocation']
