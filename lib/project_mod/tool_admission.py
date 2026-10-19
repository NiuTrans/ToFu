"""Per-call admission controls; no task registry or durable ownership state.

The project dispatcher installs this scope before file preimage acquisition.
Waiting writers recheck cancellation and content freshness after admission.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import os

_control = ContextVar('project_tool_admission', default=None)


@dataclass(frozen=True)
class RecordedCommandSource:
    """Trusted background adapter binding, never a model-supplied argument.

    Only the exact generated wrapper may borrow this original admission source;
    changing the executed bytes invalidates the binding and fails closed.
    """
    original: str
    wrapped: str


class Cancellation:
    def __init__(self, task=None, runtime_context=None, *, tool_name='', command_source=None):
        self.task = task
        self.runtime_context = runtime_context
        self.tool_name = tool_name
        self.command_source = command_source

    def is_set(self):
        return bool((self.task and self.task.get('aborted')) or
                    (self.runtime_context is not None and
                     self.runtime_context.cancellation_requested))


def admission_timeout():
    """An optional operator wait bound; default admission owns completion."""
    raw = os.environ.get('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '')
    if not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError('TOFU_WORKSPACE_TOOL_WAIT_SECONDS must be nonnegative and finite') from error
    if not math.isfinite(value) or value < 0:
        raise ValueError('TOFU_WORKSPACE_TOOL_WAIT_SECONDS must be nonnegative and finite')
    return value


@contextmanager
def tool_admission_scope(task=None, runtime_context=None, *, tool_name='', command_source=None):
    if command_source is not None and not isinstance(command_source, RecordedCommandSource):
        raise ValueError('Invalid recorded command admission binding')
    token = _control.set(Cancellation(task, runtime_context, tool_name=tool_name,
                                      command_source=command_source))
    try:
        yield
    finally:
        _control.reset(token)


def recorded_command_source(command):
    """Return trusted original only for its exact server-generated wrapper."""
    control = _control.get()
    binding = control.command_source if control is not None else None
    if binding is None:
        return None
    if command != binding.wrapped:
        raise ValueError('Recorded command wrapper changed before execution')
    return binding.original


def journal_conv_id(execution_key):
    """Undo/recovery belong to the actual conversation, never the cwd key."""
    control = _control.get()
    task = control.task if control is not None else None
    if task and str(task.get('id') or '') == str(execution_key or ''):
        return task.get('convId') or execution_key
    return execution_key


def enforce_workspace_target(path):
    """Check real checkout after path resolution, including symlink escapes."""
    control = _control.get()
    task = control.task if control is not None else None
    if task:
        from lib.tasks_pkg.workspace_delivery import workspace_target_refusal
        refusal = workspace_target_refusal(task, path)
        if refusal:
            raise ValueError(refusal)


def cancellation():
    return _control.get()
