"""Synchronous agent invocation policy, independent of scheduler and transport.

Root and child agents enter the mature preparation/request/tool/settlement
owner. A child owns its task and resources, never its parent's conversation
lifecycle. The scheduler supplies scoped tools and a required evidence sink.
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


def _stop_internal_invocation(task: dict, reason: str, *, beacon=None) -> bool:
    """Publish cause before the physical abort wakes provider/tool consumers."""
    from lib.error_envelope import make_envelope
    detail = ('Invocation stalled: no model or tool progress was observed'
              if reason == 'stalled' else 'Invocation execution deadline exceeded')
    if beacon is not None:
        # Numeric beacon facts only: notes, prompts and provider payloads are
        # deliberately excluded from public diagnostics.
        key = task.get('_invocationProgressKey') or task['id']
        elapsed = beacon.seconds_since_activity(key)
        task['_invocationStopEvidence'] = {
            'silentSeconds': elapsed, 'stallTimeoutSeconds': beacon.stall_timeout}
        detail += f' for {elapsed:.1f}s (stall threshold {beacon.stall_timeout:g}s)'
    task['_invocationStopReason'] = reason
    task['error'] = make_envelope('internal', detail=detail,
                                  context='invocation-guard', source='task-engine',
                                  extensions={'invocationStopReason': reason})
    task['aborted'] = True
    task['abort_event'].set()
    return True


def invocation_abort_requested(task: dict) -> bool:
    """Observe inherited cancellation without granting upward cancellation rights."""
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
    beacon = task.get('_invocationProgressBeacon')
    if (beacon is not None and not task.get('_storageWaits')
            and not beacon.is_making_progress(task.get('_invocationProgressKey') or task['id'])):
        return _stop_internal_invocation(task, 'stalled', beacon=beacon)
    deadline = task.get('_invocationDeadline')
    if deadline is not None and time.monotonic() >= deadline:
        return _stop_internal_invocation(task, 'deadline')
    return bool(task.get('aborted'))


def deliver_invocation_event(task: dict, event: dict) -> None:
    """Required, ordered handoff, unlike a best-effort observer notification."""
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
            from lib.tasks_pkg.persistence_wait import wait_for_persistence
            from lib.tasks_pkg.event_log import append_persistent_event
            # The append is idempotent on its natural (task, sequence) key.
            # Already-observed evidence waits for ACK even after user Stop;
            # transient storage pressure cannot poison invocation delivery.
            wait_for_persistence(
                task, lambda: append_persistent_event(
                    task['_inspectorTaskId'], event['seq'], event),
                label='invocation evidence append', cancellable=False)
        sink(event)
    except Exception as exc:
        task['_invocationDeliveryError'] = exc
        task['aborted'] = True
        task['abort_event'].set()
        raise


def invocation_output_projection(task: dict[str, Any]) -> dict[str, Any]:
    """Use the ordinary checkpoint assembly, never an event-log reconstruction."""
    from lib.tasks_pkg.segments import assemble_segments, segments_to_json
    return {
        'content': task.get('content') or '',
        'thinking': task.get('thinking') or '',
        'segments': segments_to_json(assemble_segments(task)),
        'toolRounds': [dict(row) for row in task.get('toolRounds') or []],
    }


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
        with isolated_conv_affinity(invocation_affinity_id(task)):
            invocation_abort_requested(task)
            return copy_context().run(run_in_scope)
    finally:
        if task.get('_swarm_inject_pending'):
            from lib.swarm.delivery import restore_unconsumed_results
            restore_unconsumed_results(task)


__all__ = ['InvocationPolicy', 'ROOT_INVOCATION_POLICY', 'execute_invocation',
           'is_child_invocation', 'deliver_invocation_event',
           'invocation_output_projection']
