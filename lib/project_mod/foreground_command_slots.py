"""Per-command views of the task slots that address one foreground command.

Responsibility: every tool call of a model round runs concurrently
(docs/contracts/TOOL_EXECUTION_POLICY.md, "Concurrent tool rounds"), so a task
can run several foreground ``run_command`` subprocesses on every execution
path: conversation turns, headless requests, swarm/Goal/Flow child
invocations, Evolution and timers. Owners outside a command address it through
task-level slots: the ``_subprocess_pid``/``_subprocess_pgid`` mirror (interrupt
route, durable interrupt sweep, stall watchdog), the pid-stamped
``_cmd_interrupt`` request, and ``_cmd_handoff_control`` (background route).
Each runner sees its task through a :class:`CommandTaskView`, which publishes
its process into the per-task ``_foregroundCommands`` registry. The mirror
names the newest live command and falls back to the next one when it exits or
detaches; an interrupt addressed to a live sibling is invisible to every other
command and is voided only once its pid no longer runs; each command keeps its
own output clock, so the watchdog interrupts the command that is silent.

Entry points: ``command_task_view`` (both run_command runners),
``CommandTaskView`` (base of the conversation handoff proxy),
``foreground_command_silences`` (stall watchdog), ``foreground_pid_is_live``,
``register_handoff_control``/``retire_handoff_control``/``handoff_control_for``
(background route).
Dependencies: ``lib.project_mod.tool_admission`` (marker key only).
``_REGISTRY_LOCK`` is a leaf lock: nothing is called while it is held.
"""
from __future__ import annotations

import threading
import time
from typing import Any

FOREGROUND_REGISTRY_KEY = '_foregroundCommands'
PROCESS_KEYS = frozenset({'_subprocess_pid', '_subprocess_pgid'})
# The command never owns its task's lifecycle. A copied value outlives the
# turn: a late frame then re-adopted a settled or discarded task as live.
_LIFECYCLE_KEYS = frozenset({'status', '_discarded_at'})
# A view starts from a snapshot of its task; these slots address one command,
# so a copy of a sibling's value must never become this command's own.
_COMMAND_SLOT_KEYS = PROCESS_KEYS | {'_cmd_interrupt', FOREGROUND_REGISTRY_KEY}
_SPAWNED_AT = 'spawnedAt'
_OUTPUT_AT = 'outputAt'
_REGISTRY_LOCK = threading.Lock()


def _registry(task: dict[str, Any], *, create: bool = False) -> dict | None:
    """Caller holds ``_REGISTRY_LOCK``."""
    registry = task.get(FOREGROUND_REGISTRY_KEY)
    if registry is None and create:
        registry = {'process': {}, 'controls': []}
        task[FOREGROUND_REGISTRY_KEY] = registry
    return registry


def _drop_empty_registry(task: dict[str, Any], registry: dict | None) -> None:
    if registry is not None and not registry['process'] and not registry['controls']:
        if task.get(FOREGROUND_REGISTRY_KEY) is registry:
            task.pop(FOREGROUND_REGISTRY_KEY, None)


def _publish_process(task: dict[str, Any], token: object, key: str, value: Any) -> None:
    with _REGISTRY_LOCK:
        processes = _registry(task, create=True)['process']
        entry = processes.pop(token, None) or {_SPAWNED_AT: time.time()}
        entry[key] = value
        processes[token] = entry
        task[key] = value


def _retract_process(task: dict[str, Any], token: object, key: str, value: Any) -> None:
    """Withdraw one command's process key; the mirror falls back to the next."""
    with _REGISTRY_LOCK:
        registry = _registry(task)
        entry = registry['process'].get(token) if registry is not None else None
        if entry is not None:
            entry.pop(key, None)
            if not PROCESS_KEYS & entry.keys():
                registry['process'].pop(token, None)
        if value is not None and task.get(key) == value:
            successor = next((
                live[key] for live in reversed(registry['process'].values())
                if key in live), None) if registry is not None else None
            if successor is None:
                task.pop(key, None)
            else:
                task[key] = successor
        _drop_empty_registry(task, registry)


def _note_output(task: dict[str, Any], token: object) -> None:
    with _REGISTRY_LOCK:
        registry = _registry(task)
        entry = registry['process'].get(token) if registry is not None else None
        if entry is not None:
            entry[_OUTPUT_AT] = time.time()


def foreground_pid_is_live(task: dict[str, Any], pid: Any) -> bool:
    if pid is None:
        return False
    if task.get('_subprocess_pid') == pid:
        return True
    with _REGISTRY_LOCK:
        registry = _registry(task)
        return registry is not None and any(
            live.get('_subprocess_pid') == pid for live in registry['process'].values())


def foreground_command_silences(task: dict[str, Any], now: float) -> list[tuple[Any, float]]:
    """``(pid, seconds since its last output or spawn)`` for each live command."""
    with _REGISTRY_LOCK:
        registry = _registry(task)
        return [
            (live['_subprocess_pid'], now - live.get(_OUTPUT_AT, live[_SPAWNED_AT]))
            for live in (registry['process'].values() if registry is not None else ())
            if '_subprocess_pid' in live]


def _interrupt_names_live_sibling(task: dict[str, Any], token: object, flag: Any) -> bool:
    """An interrupt addressed to another live command is not this one's to void."""
    if not isinstance(flag, dict) or flag.get('pid') is None:
        return False
    with _REGISTRY_LOCK:
        registry = _registry(task)
        return registry is not None and any(
            other is not token and live.get('_subprocess_pid') == flag['pid']
            for other, live in registry['process'].items())


def register_handoff_control(task: dict[str, Any], control: Any) -> None:
    with _REGISTRY_LOCK:
        _registry(task, create=True)['controls'].append(control)
        task['_cmd_handoff_control'] = control


def retire_handoff_control(task: dict[str, Any], control: Any) -> None:
    with _REGISTRY_LOCK:
        registry = _registry(task)
        controls = registry['controls'] if registry is not None else []
        if control in controls:
            controls.remove(control)
        if task.get('_cmd_handoff_control') is control:
            if controls:
                task['_cmd_handoff_control'] = controls[-1]
            else:
                task.pop('_cmd_handoff_control', None)
        _drop_empty_registry(task, registry)


def handoff_control_for(task: dict[str, Any], round_num: int, tool_call_id: str) -> Any:
    with _REGISTRY_LOCK:
        registry = _registry(task)
        return next((
            control for control in reversed(registry['controls'] if registry else [])
            if control.round_num == round_num and control.tool_call_id == tool_call_id),
            None)


class CommandTaskView(dict):
    """One foreground command's view of its task.

    Reads come from a snapshot of the task, except Stop (``aborted``), the
    lifecycle keys and interrupt requests, which stay live. Process keys the
    command writes are published into the task's registry while it is
    attached. ``slot_lock`` serializes those updates with a decision about this
    command (the conversation handoff detaches under it).
    """

    def __init__(self, parent: dict[str, Any]):
        from lib.project_mod.tool_admission import WORKSPACE_ADMISSION_KEY
        super().__init__(parent)
        # An internal lease wait of another call of the task (dependency
        # setup) may be published right now; its marker belongs to that call.
        for key in (*_COMMAND_SLOT_KEYS, WORKSPACE_ADMISSION_KEY):
            dict.pop(self, key, None)
        self._parent = parent
        self._command_token = object()
        self.slot_lock = threading.RLock()
        self._detached = False

    def _detach_from_parent(self) -> None:
        """Caller holds ``slot_lock``: the task's slots stop naming this command."""
        self._detached = True
        for key in PROCESS_KEYS:
            _retract_process(self._parent, self._command_token, key, dict.get(self, key))

    def note_output(self) -> None:
        """Restart this command's silence clock (the stall watchdog's evidence)."""
        with self.slot_lock:
            if not self._detached:
                _note_output(self._parent, self._command_token)

    def get(self, key, default=None):
        with self.slot_lock:
            if key == 'aborted':
                return bool(self._parent.get(key, False)
                            or dict.get(self, key, False))
            if key in _LIFECYCLE_KEYS:
                return self._parent.get(key, default)
            if key == '_cmd_interrupt' and not self._detached:
                flag = self._parent.get(key, default)
                if _interrupt_names_live_sibling(self._parent, self._command_token, flag):
                    return default
                return flag
            return dict.get(self, key, default)

    def __setitem__(self, key, value):
        with self.slot_lock:
            dict.__setitem__(self, key, value)
            if key in PROCESS_KEYS and not self._detached:
                _publish_process(self._parent, self._command_token, key, value)

    def pop(self, key, default=None):
        with self.slot_lock:
            value = dict.pop(self, key, default)
            if key in PROCESS_KEYS and not self._detached:
                _retract_process(self._parent, self._command_token, key, value)
            elif key == '_cmd_interrupt' and not self._detached:
                if not _interrupt_names_live_sibling(
                        self._parent, self._command_token, self._parent.get(key)):
                    self._parent.pop(key, None)
            return value


def command_task_view(task: dict[str, Any] | None) -> dict[str, Any] | None:
    """The view a runner uses for ``task``; an existing view is reused."""
    if task is None or isinstance(task, CommandTaskView):
        return task
    return CommandTaskView(task)


__all__ = [
    'CommandTaskView', 'FOREGROUND_REGISTRY_KEY', 'PROCESS_KEYS', 'command_task_view',
    'foreground_command_silences', 'foreground_pid_is_live', 'handoff_control_for',
    'register_handoff_control', 'retire_handoff_control',
]
