"""Reasons a silent task is not a wedged one.

Entry point: ``explained_wait(task) -> str``. Empty means the caller's own
liveness clocks are the only evidence available, i.e. silence that no other
component owns and that therefore may be a real wedge.

Two consumers decide from silence and MUST ask this owner instead of testing
one marker themselves (``manager._maintenance.reap_stuck_running_tasks`` and
``invocation.invocation_abort_requested``). Enumerating the wait class
separately in each consumer is exactly how a 20-minute writer-admission wait
was force-failed as "no progress" while it was queued behind another writer.

Markers are published for the DURATION of the wait only, by the module that
owns the wait:

- ``_storageWaits`` — persistence ACK backpressure
  (``persistence_wait.wait_for_persistence``). The write is unacknowledged;
  storage may take as long as it takes.
- ``_workspaceAdmission`` — workspace lease admission
  (``project_mod.workspace_lease._publish_admission_wait``, entered for every
  acquisition, so commands, scoped file writes and MCP preparation are all
  covered; the key itself is owned by ``project_mod.tool_admission``). No
  process exists yet, no output can arrive, and
  ``tool_admission.admission_timeout()`` defaults to "admission owns
  completion". The marker is dropped before execution, where the ordinary
  process clock applies again.

An explained wait never grants cancellation immunity: explicit Stop, an
execution deadline, and the operator bound
``TOFU_WORKSPACE_TOOL_WAIT_SECONDS`` all remain owned by the invocation.
"""

from lib.project_mod.tool_admission import WORKSPACE_ADMISSION_KEY

STORAGE_WAIT_KEY = '_storageWaits'


def explained_wait(task):
    """Name the external owner of this task's silence, or '' when none does."""
    if not isinstance(task, dict):
        return ''
    if task.get(STORAGE_WAIT_KEY):
        return 'storage'
    if isinstance(task.get(WORKSPACE_ADMISSION_KEY), dict):
        return 'workspace-admission'
    return ''
