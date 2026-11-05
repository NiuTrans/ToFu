"""Call-order exceptions for one concurrently started tool batch.

Owner decision 2026-10-28 (docs/contracts/TOOL_EXECUTION_POLICY.md, "Concurrent
tool rounds"): every admitted call of one model round starts at once, writes
included. Order survives only where concurrent starts would silently change
what the sequential calls meant and keeping it is cheap:

* file mutations resolving to the same file, desktop writes to the same path,
  memory mutations of the same memory id, and browser page actions on the
  same tab apply in provider call order; other targets run concurrently;
* ``todo_write`` revisions apply in call order among themselves;
* each ``run_command`` working directory is frozen here, in call order, so a
  sticky ``working_dir`` set by an earlier call of the round is honoured and
  concurrency never changes the directory a command runs in. A command that
  inherits a sibling's directory waits for that sibling's admission verdict,
  never its completion: a sibling refused before execution never moved the
  sticky directory.

Entry points: ``call_predecessors``, ``plan_command_working_dirs``
(``CommandDirectoryPlan``) and ``restore_command_sticky_cwd``. Path resolution
borrows the workspace owners read-only; nothing here executes a tool or holds
a lock.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.tool_dispatch._flags import ORDERED_STATE_TOOLS

logger = get_logger(__name__)

FILE_MUTATION_TOOLS = frozenset({
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
})

# A browser action without ``tab_id`` acts on whichever tab is active, so it
# conflicts with every other page action of the round.
_ANY_TAB = '*'

_MEMORY_MUTATION_TOOLS = frozenset({'update_memory', 'delete_memory', 'merge_memories'})


def _mutation_paths(arguments: dict[str, Any]) -> list[str]:
    paths = [arguments.get('path')]
    paths += [edit.get('path') for edit in arguments.get('edits') or ()
              if isinstance(edit, dict)]
    return list(dict.fromkeys(
        path for path in paths if isinstance(path, str) and path.strip()))


def _file_identity(task: dict, project_path: str | None, raw: str) -> str:
    """Resolve one write target the way the execution view will see it."""
    value = raw[7:] if raw.startswith('file://') else raw
    try:
        from lib.project_mod.config import execution_root_scope
        from lib.project_mod.path_resolution import _resolve_base
        from lib.tasks_pkg.workspace_delivery import (
            ensure_execution_roots, execution_root_key, rebind_project_tool_arguments,
        )
        value = rebind_project_tool_arguments(task, {'path': value})['path']
        if value.startswith(('/', '~')):
            return os.path.realpath(os.path.expanduser(value))
        key = execution_root_key(task)
        with execution_root_scope(key, ensure_execution_roots(task)):
            base, relative = _resolve_base(project_path or '', value, conv_id=key)
        return os.path.realpath(os.path.join(base, os.path.expanduser(relative)))
    except Exception as error:
        # The handler reports the invalid path; ordering keeps the raw spelling.
        logger.debug('[ToolOrder] unresolved write target %r: %s', raw, error)
        return 'unresolved:' + raw


def _ordering_keys(task: dict, project_path: str | None, name: str,
                   arguments: dict[str, Any], write_tools: frozenset,
                   resolve_files: bool) -> frozenset[str]:
    if name in ORDERED_STATE_TOOLS:
        return frozenset({'state:' + name})
    if name in FILE_MUTATION_TOOLS:
        paths = _mutation_paths(arguments)
        if resolve_files:
            paths = [_file_identity(task, project_path, path) for path in paths]
        return frozenset('file:' + path for path in paths)
    if name.startswith('browser_') and name in write_tools:
        tab = arguments.get('tab_id')
        scalar = isinstance(tab, (str, int)) and not isinstance(tab, bool) and tab != ''
        return frozenset({'browser:' + (str(tab) if scalar else _ANY_TAB)})
    if name == 'desktop_write_file' and isinstance(arguments.get('path'), str):
        # A remote device path: compared as spelled, it cannot resolve here.
        return frozenset({'desktop-file:' + arguments['path']})
    if name in _MEMORY_MUTATION_TOOLS:
        ids = [arguments.get('memory_id')]
        if isinstance(arguments.get('memory_ids'), (list, tuple)):
            ids += arguments['memory_ids']
        return frozenset('memory:' + str(memory_id) for memory_id in ids
                         if isinstance(memory_id, (str, int)) and memory_id != '')
    return frozenset()


def _conflicts(left: frozenset[str], right: frozenset[str]) -> bool:
    for one in left:
        family, _, target = one.partition(':')
        for other in right:
            other_family, _, other_target = other.partition(':')
            if family == other_family and (
                    target == other_target
                    or (family == 'browser' and _ANY_TAB in (target, other_target))):
                return True
    return False


def call_predecessors(task: dict, project_path: str | None, items: list[tuple],
                      write_tools: frozenset) -> dict[str, tuple[str, ...]]:
    """Map each call id to the earlier calls it must wait for (call order)."""
    mutations = sum(1 for item in items if item[1] in FILE_MUTATION_TOOLS)
    keyed = [(item[2], _ordering_keys(task, project_path, item[1], item[3] or {},
                                      write_tools, resolve_files=mutations > 1))
             for item in items]
    predecessors: dict[str, tuple[str, ...]] = {}
    for index, (call_id, keys) in enumerate(keyed):
        predecessors[call_id] = tuple(
            earlier_id for earlier_id, earlier_keys in keyed[:index]
            if keys and earlier_keys and _conflicts(keys, earlier_keys))
    return predecessors


def _execution_cwd(task: dict, frozen_cwd: str) -> str:
    from lib.tasks_pkg.workspace_delivery import rebind_project_tool_arguments
    return rebind_project_tool_arguments(task, {'working_dir': frozen_cwd})['working_dir']


@dataclass(frozen=True)
class _Directory:
    frozen: str  # the ``working_dir`` argument the handler receives
    execution: str  # where the command runs; the sticky value it leaves


@dataclass
class _PlannedCommand:
    arguments: dict
    directory: _Directory  # the directory its arguments carry now
    fallback: _Directory  # the sticky directory before the round
    # Earlier sticky-setting siblings this call inherits from, oldest first.
    sources: tuple[str, ...] = ()


@dataclass
class _AdmissionVerdict:
    decided: threading.Event = field(default_factory=threading.Event)
    entered: bool = False


class CommandDirectoryPlan:
    """Call-order ``run_command`` directories of one concurrently started round.

    Sequential execution runs a command without ``working_dir`` where the last
    earlier command that actually ran left the sticky directory. Siblings are
    admitted concurrently (approval and pre-hooks run on each call's worker), so
    an inheriting call settles its directory only once the admission verdict of
    each sibling it could inherit from is known: a sibling refused at approval,
    by a pre-hook or by Stop never moved the sticky directory. Iterating the
    plan yields ``(call id, execution cwd)`` in call order.
    """

    def __init__(self) -> None:
        self._commands: dict[str, _PlannedCommand] = {}
        self._verdicts: dict[str, _AdmissionVerdict] = {}

    def __iter__(self):
        return iter([(call_id, planned.directory.execution)
                     for call_id, planned in self._commands.items()])

    def resolve(self, call_id: str, task: dict) -> bool:
        """Settle ``call_id``'s directory before its admission; False when Stop wins."""
        planned = self._commands.get(call_id)
        if planned is None or not planned.sources:
            return True
        chosen = planned.fallback
        for source in reversed(planned.sources):
            verdict = self._verdicts[source]
            while not verdict.decided.wait(0.1):
                if task.get('aborted'):
                    return False
            if verdict.entered:
                chosen = self._commands[source].directory
                break
        planned.directory = chosen
        planned.arguments['working_dir'] = chosen.frozen
        return True

    def settle(self, call_id: str, entered: bool) -> None:
        """Publish a sticky-setting call's admission verdict to its inheritors."""
        verdict = self._verdicts.get(call_id)
        if verdict is not None:
            verdict.entered = entered
            verdict.decided.set()


def plan_command_working_dirs(task: dict, items: list[tuple]) -> CommandDirectoryPlan:
    """Freeze every ``run_command`` cwd in call order.

    A command without ``working_dir`` resumes from the sticky directory an
    earlier call of this round leaves behind, exactly as sequential execution
    would. Its arguments carry that directory now; ``resolve`` falls back to the
    previous candidate when the sibling never runs.
    """
    plan = CommandDirectoryPlan()
    config = task.get('config') or {}
    if not config.get('projectPath') or config.get('project_remote'):
        return plan
    from lib.project_mod.config import _cwd_within_conv_roots
    from lib.project_mod.tools import _sticky_cwd_enabled
    from lib.tasks_pkg.project_guidance import freeze_project_command_cwd
    from lib.tasks_pkg.workspace_delivery import execution_root_key

    sticky_enabled = _sticky_cwd_enabled()
    key = execution_root_key(task)
    setters: list[str] = []
    for item in items:
        if item[1] != 'run_command' or not isinstance(item[3], dict):
            continue
        arguments = item[3]
        explicit = bool(arguments.get('working_dir'))
        try:
            # Without ``working_dir`` this is the sticky directory before the
            # round: the fallback when no earlier sibling runs.
            frozen = freeze_project_command_cwd(task, arguments)['working_dir']
            directory = _Directory(frozen, _execution_cwd(task, frozen))
        except Exception as error:
            # The handler freezes again and reports this argument error itself.
            logger.debug('[ToolOrder] run_command cwd not frozen for %s: %s',
                         item[2], error)
            continue
        sources = () if explicit else tuple(setters)
        inherited = plan._commands[sources[-1]].directory if sources else directory
        plan._commands[item[2]] = _PlannedCommand(arguments, inherited, directory, sources)
        arguments['working_dir'] = inherited.frozen
        if (explicit and sticky_enabled and os.path.isdir(directory.execution)
                and _cwd_within_conv_roots(key, directory.execution)):
            setters.append(item[2])
            plan._verdicts[item[2]] = _AdmissionVerdict()
    return plan


def restore_command_sticky_cwd(task: dict, planned, entered: set[str]) -> None:
    """Leave the sticky cwd where the last command in call order left it.

    ``planned`` yields ``(call id, execution cwd)`` in call order (a
    ``CommandDirectoryPlan``). Concurrent commands each record their own
    directory as they start, so the last writer is arbitrary. Re-assert the
    call-order result once all ran.
    """
    ran = [cwd for call_id, cwd in planned if call_id in entered]
    if len(set(ran)) < 2:
        return
    from lib.project_mod.config import set_conv_cwd
    from lib.project_mod.tools import _sticky_cwd_enabled
    from lib.tasks_pkg.workspace_delivery import execution_root_key
    if not _sticky_cwd_enabled():
        return
    key = execution_root_key(task)
    for cwd in reversed(ran):
        if set_conv_cwd(key, cwd):
            return
