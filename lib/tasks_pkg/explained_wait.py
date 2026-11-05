"""Reasons a silent task is not a wedged one.

Entry point: ``explained_wait(task) -> str``. Empty means the caller's own
liveness clocks are the only evidence available, i.e. silence that no other
component owns and that therefore may be a real wedge.

The one silence judge for root and child agents (``tasks_pkg.liveness``) asks
this owner instead of testing one marker itself. Enumerating the wait class
separately in each consumer is exactly how a 20-minute writer-admission wait
was force-failed as "no progress" while it was queued behind another writer.
The judge also stamps when it last saw a wait explained, so the silence that
follows restarts from the wait's end (``liveness.silence_origin``).

Markers are published for the DURATION of the wait only, by the module that
owns the wait:

- ``_storageWaits`` — persistence ACK backpressure
  (``persistence_wait.wait_for_persistence``). The write is unacknowledged;
  storage may take as long as it takes.
- ``_workspaceAdmission`` — a task-bound internal workspace lease acquisition
  (``project_mod.workspace_lease._publish_admission_wait``): dependency setup
  before a worker command, or a stable integration snapshot. Model tool calls
  take no lease and never publish it. The key itself is owned by
  ``project_mod.tool_admission``. No process exists yet, no output can
  arrive, and ``tool_admission.admission_timeout()`` defaults to waiting until
  admitted. The marker is dropped before execution, where the ordinary process
  clock applies again.
- ``_childInvocationWaits`` — a parent blocked synchronously on child
  invocation carriers (:func:`child_invocation_wait`, published by the Flow
  step runner ``orchestration.agent_runner``). The parent's clocks advance
  only when a child emits events, so a child inside one silent 30-minute
  command froze both parent clocks and the reaper force-failed the Flow turn
  on the very tick it correctly interrupted the child's command (2026-11-01).
  The wait is explained only while a published child is itself live: the
  child is a registered task judged by its own clocks and command watch, and
  once it is reaped or finished the parent's silence is unexplained again.
- host filesystem stall — a keepalive ``stat`` unanswered past the stale-mount
  bound (``lib.fs_keepalive.filesystem_unresponsive``). Worktrees, logs and the
  storage authority share that mount, so blocked I/O silences every agent at
  once and no stop can interrupt it; a verdict could only take effect when the
  I/O returns, i.e. exactly when the agent could continue (2026-11-02).

An explained wait never grants cancellation immunity: explicit Stop, an
execution deadline, and the operator bound
``TOFU_WORKSPACE_TOOL_WAIT_SECONDS`` all remain owned by the invocation.
"""

from contextlib import contextmanager

STORAGE_WAIT_KEY = '_storageWaits'
CHILD_INVOCATION_WAIT_KEY = '_childInvocationWaits'


@contextmanager
def child_invocation_wait(parent, child):
    """Publish that ``parent`` is blocked on ``child`` for the wait's duration."""
    # Never removed: concurrent Flow branches share one mapping, and an empty
    # mapping already means no wait.
    waits = parent.setdefault(CHILD_INVOCATION_WAIT_KEY, {})
    waits[child['id']] = child
    try:
        yield
    finally:
        waits.pop(child['id'], None)


def _live_child(child):
    return (isinstance(child, dict) and not child.get('aborted')
            and child.get('status') in ('pending', 'running'))


def explained_wait(task):
    """Name the external owner of this task's silence, or '' when none does."""
    if not isinstance(task, dict):
        return ''
    if task.get(STORAGE_WAIT_KEY):
        return 'storage'
    # Deferred: the task manager imports this module at server boot, and the
    # project tool package must stay unloaded until a task actually runs.
    from lib.project_mod.tool_admission import WORKSPACE_ADMISSION_KEY
    if isinstance(task.get(WORKSPACE_ADMISSION_KEY), dict):
        return 'workspace-admission'
    children = task.get(CHILD_INVOCATION_WAIT_KEY)
    if isinstance(children, dict) and any(
            _live_child(child) for child in list(children.values())):
        return 'child-invocation'
    from lib.fs_keepalive import filesystem_unresponsive
    if filesystem_unresponsive():
        return 'filesystem'
    return ''
