"""Workspace admission-wait evidence -> one task event (the lease observer port).

``lib.project_mod.workspace_lease`` owns every admission wait and reports an
acquisition that had to wait through ``set_admission_wait_observer``; the lease
layer never imports the task layer. This module is the task layer's observer:
it projects the wait's monotonic instants onto the waiting task's execution
clock and appends one ``workspace_admission_wait`` event, from which the Turn
Trace fold derives the ``admission_wait`` span
(``docs/contracts/TURN_TRACE_CONTRACT.md``). ``lib.tasks_pkg.executor``
installs it at import, before any tool can execute.
"""

from __future__ import annotations

from lib.agent_core.events import EventType, build_event
from lib.agent_core.execution_clock import ExecutionClock
from lib.log import get_logger

logger = get_logger(__name__)


def observe_admission_wait(task, wait):
    """Append the evidence event for one finished wait; never raises."""
    clock = task.get('_executionClock')
    if not isinstance(clock, ExecutionClock):
        # Without the task's execution clock the instants have no event-time
        # domain to land in (legacy/transient carriers); omit, never guess.
        return
    started = clock.at_monotonic(wait.started_monotonic)
    ended = max(started, clock.at_monotonic(wait.ended_monotonic))
    fields = {}
    if wait.round_num is not None:
        fields['roundNum'] = wait.round_num
    if wait.tool_call_id:
        fields['toolCallId'] = wait.tool_call_id
    if wait.waiter_tool:
        fields['toolName'] = wait.waiter_tool
    fields.update(
        waitStartedAtMs=started, waitEndedAtMs=ended, waitedMs=ended - started,
        mode=wait.mode, lockDomain=wait.lock_domain, lockRoot=wait.lock_root,
        outcome=wait.outcome)
    for name, value in (('holderTool', wait.holder_tool),
                        ('holderTask', wait.holder_task),
                        ('holderConversation', wait.holder_conversation),
                        ('waiterClaims', wait.waiter_claims),
                        ('holderMode', wait.holder_mode),
                        ('holderClaims', wait.holder_claims),
                        ('holderCommand', wait.holder_command)):
        if value:
            fields[name] = value
    if wait.holder_held_ms is not None:
        fields['holderHeldMs'] = wait.holder_held_ms
    from lib.tasks_pkg.manager import append_event
    try:
        append_event(task, build_event(EventType.WORKSPACE_ADMISSION_WAIT, **fields))
    except Exception as exc:
        # A revoked/cancelled delivery lane refuses this optional evidence like
        # every later frame; the tool's own lifecycle events surface that fate.
        logger.debug('[AdmissionWait] evidence not delivered task=%s: %s',
                     str(task.get('id') or '?')[:8], exc)


def install_admission_wait_observer():
    from lib.project_mod.workspace_lease import set_admission_wait_observer
    set_admission_wait_observer(observe_admission_wait)


__all__ = ['install_admission_wait_observer', 'observe_admission_wait']
