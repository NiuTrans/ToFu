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
from lib.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InvocationPolicy:
    """Explicit lifecycle authority; model/capabilities remain task-local."""

    parent_task_id: str | None = None
    presentation: str = 'task'
    settlement: str = 'task'
    cancellation: str = 'task'
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
        root = (not self.is_child and self.presentation == 'task'
                and self.settlement == 'task' and self.cancellation == 'task')
        child = (bool(self.parent_task_id) and self.presentation == 'child'
                 and self.settlement == 'child' and self.cancellation == 'inherited'
                 and not self.kickoff)
        if not (root or child):
            raise ValueError('Invalid invocation policy: child requires task-local evidence port and lifecycle isolation')

    @classmethod
    def child(cls, parent_task_id: str, *, timeout_seconds: float = 0,
              max_429_attempts: int | None = None) -> 'InvocationPolicy':
        return cls(parent_task_id=parent_task_id, presentation='child',
                   settlement='child', cancellation='inherited', kickoff=False,
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
    if task.get('aborted'):
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
    if limits.get('deadline') is not None and time.time() >= limits['deadline']:
        task['_executionLimitReason'] = 'execution_deadline_exceeded'
        return _stop_internal_invocation(task, 'deadline')
    beacon = task.get('_invocationProgressBeacon')
    if beacon is not None and not beacon.is_making_progress(task.get('_invocationProgressKey') or task['id']):
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
            from lib.storage.retry import call_with_transient_retry
            from lib.tasks_pkg.event_log import append_persistent_event
            # The append is idempotent on its natural (task, sequence) key,
            # so a transient authority stall earns a bounded second chance
            # instead of poisoning every later delivery of this invocation.
            call_with_transient_retry(
                lambda: append_persistent_event(
                    task['_inspectorTaskId'], event['seq'], event),
                abort_event=task.get('abort_event'),
                label=f"invocation evidence append task={str(task.get('id') or '')[:8]}")
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
    from lib.tasks_pkg.orchestrator._run import _execute_task_invocation
    policy.validate()
    from lib.agent_core.execution_limits import validate_execution_limits
    validate_execution_limits((task.get('config') or {}).get('executionLimits'))
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
            task['_invocationDeadline'] = time.monotonic() + policy.timeout_seconds
        # ContextVars isolate logs, principal and provider pin. Affinity and its
        # pick diagnostic are thread-local and need an explicit nested scope.
        # A child invocation is user-requested work, not optional background
        # work: it must NOT inherit a strict billing-stop-admission scope its
        # parent entered for its own cheap side calls (title-gen, translate,
        # optimizer, …). copy_context() would otherwise propagate that
        # ContextVar and the child's model dispatch would reject every slot
        # with a recorded billing stop → "No slot available" with no upstream
        # request ever sent (the swarm sub-agent failure, conv mu8e3bdwdqovh6).
        from lib.key_stats._admission import _STRICT_BILLING_STOP_ADMISSION
        from lib.llm_dispatch.conv_affinity import isolated_conv_affinity

        def _run_child(t, *, policy):
            # Clear the flag inside the COPIED context only — the parent
            # thread's own strict scope (if any) stays intact.
            _STRICT_BILLING_STOP_ADMISSION.set(False)
            return _execute_task_invocation(t, policy=policy)

        with isolated_conv_affinity(invocation_affinity_id(task)):
            invocation_abort_requested(task)
            return copy_context().run(_run_child, task, policy=policy)
    try:
        return _execute_task_invocation(task, policy=policy)
    finally:
        if task.get('_swarm_inject_pending'):
            from lib.swarm.delivery import restore_unconsumed_results
            restore_unconsumed_results(task)


__all__ = ['InvocationPolicy', 'ROOT_INVOCATION_POLICY', 'execute_invocation',
           'is_child_invocation', 'deliver_invocation_event',
           'invocation_output_projection']
