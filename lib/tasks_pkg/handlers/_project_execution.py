# HOT_PATH
"""Admitted local project execution and command callback lifetimes.

Entry point: execute_local_project_tool returns content, an optional explicit
background receipt and the file changes a foreground command recorded for
undo, so the handler never re-reads the undo journal. The project handler owns scope/admission, path restrictions
and final settlement. Domain I/O stays in project_mod; command callbacks and
output settlement reuse code_exec, and background work uses its existing owner.
"""
from __future__ import annotations

from dataclasses import dataclass

from lib.log import get_logger
from lib.tasks_pkg.handlers.code_exec import (
    _make_grep_intercept_cb,
    _make_run_command_progress_cb,
    _make_run_command_spawn_cb,
    _make_stdin_callback,
    finalize_command_output,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class LocalProjectResult:
    content: str | dict
    background_command_id: str | None = None
    # {basePath, path, action} for each change a foreground command recorded
    # for undo while this call waited for it.
    recorded_file_changes: tuple = ()


def execute_local_project_tool(task, cfg, fn_name, tc_id, fn_args, rn,
                               round_entry, project_path, *, root_conv_id,
                               execution_args, result_projection_items,
                               file_source_capture, read_observations):
    """Execute already-admitted arguments without settling their tool round."""
    from lib.project_mod import execute_tool
    from lib.tasks_pkg.conversation_delivery import conversation_delivery_available

    recorded_file_changes = []

    # read_files is globally available — when no project is attached,
    # absolute paths still work (routed inside tool_read_files via
    # lib.file_reader); project-relative paths error out helpfully.
    if fn_name in ('read_files', 'inspect_image', 'inspect_video') and not project_path:
        # read_files / inspect_image / inspect_video need the task so an
        # uploaded-attachment ref (att_media_ / att_txt_ / /api/images/) can
        # be resolved under the owner scope (media refs require
        # task['_userId'], legacy text refs scan task['messages']).
        _no_proj_kw = {'task': task}
        if fn_name == 'read_files':
            _no_proj_kw['result_projection_items'] = result_projection_items
            _no_proj_kw['file_source_capture'] = file_source_capture

            _no_proj_kw['read_observations'] = read_observations
        tool_content = execute_tool(fn_name, fn_args, '', conv_id=root_conv_id,
                                    task_id=task['id'], **_no_proj_kw)
    else:
        _progress_cb = None
        _spawn_cb = None
        _extra_kw = {'task': task}
        if fn_name == 'write_file':
            from lib.tasks_pkg.tool_runtime import active_context_for_call
            _extra_kw['runtime_context'] = active_context_for_call(
                task, round_num=rn, tool_call_id=tc_id, round_entry=round_entry)
        if fn_name == 'read_files':
            _extra_kw['result_projection_items'] = result_projection_items
            _extra_kw['file_source_capture'] = file_source_capture

            _extra_kw['read_observations'] = read_observations
        if fn_name == 'run_command' and fn_args.get('background') and project_path:
            from lib.tasks_pkg.background_commands import (
                background_command_result, start_background_command,
            )
            # Capture only execution inputs, never foreground presentation
            # callbacks or task/round graphs in a long-lived closure.
            background_args = dict(fn_args)
            background_task_id = task['id']
            # The durable wrapper binds exact bytes, so bind the text the
            # foreground runner would execute (hardened Git reads), not
            # the raw original: an unhardened read inside a long test run
            # otherwise holds a checkout/metadata fence for its lifetime.
            from lib.project_mod.run_command import command_execution_source
            background_command = command_execution_source(
                str(background_args.get('command') or ''))

            from copy import deepcopy
            from lib.tasks_pkg.workspace_delivery import (
                execution_repositories, ensure_execution_roots)
            background_policy = deepcopy(ensure_execution_roots(task))

            background_roots = {key: deepcopy((task.get('config') or {}).get(key))
                for key in ('projectPath', 'projectPaths', 'readOnlyPaths',
                            'workspaceRuntimeRoots')}
            background_execution = {
                'repositories': deepcopy(execution_repositories(task))}
            def _execute_explicit_background(command_task, exec_ctx):
                exec_args = dict(background_args)
                command_task['_workspaceExecution'] = background_execution

                if background_policy is not None:
                    command_task['_workspaceRootPolicy'] = deepcopy(background_policy)
                command_task['config'] = background_roots
                wrapped = exec_ctx.get('wrapped_command')
                from lib.project_mod.tool_admission import RecordedCommandSource
                admission_source = (RecordedCommandSource(
                    background_command, wrapped) if wrapped else None)
                if wrapped:
                    # POSIX durable record: streams go to files, the exit
                    # code to a sentinel; the worker rebuilds the
                    # authoritative result from those files afterwards.
                    exec_args['command'] = wrapped

                spawned = False

                def _note_spawn(*_spawn_args):
                    nonlocal spawned
                    spawned = bool(command_task.get('_subprocess_pid'))
                    exec_ctx['note_spawn'](
                        command_task.get('_subprocess_pid'),
                        command_task.get('_subprocess_pgid'))

                result = execute_tool(
                    'run_command', exec_args, project_path,
                    conv_id=root_conv_id, task_id=background_task_id,
                    task=command_task, stdin_callback=None, on_chunk=None,
                    on_spawn=_note_spawn, runtime_context=None,
                    recorded_command_source=admission_source,
                )
                if wrapped and spawned:
                    return exec_ctx['build_result'](
                        aborted=bool(command_task.get('aborted')),
                        runner_result=str(result or ''))
                return result

            command_id = start_background_command(
                task=task, config=cfg,
                command=background_command,
                execute=_execute_explicit_background,
                credentials=background_args.get('credentials'),
            )
            tool_content = background_command_result(
                str(background_args.get('command') or ''), command_id,
                task=task,
                has_conversation=conversation_delivery_available(task),
            )
            return LocalProjectResult(tool_content, command_id)
        if fn_name == 'run_command':
            _cmd = fn_args.get('command', '') or ''
            _stdin_cb = _make_stdin_callback(task, rn, round_entry, _cmd)
            from lib.tasks_pkg.tool_runtime import active_context_for_call
            _runtime_context = active_context_for_call(
                task, round_num=rn, tool_call_id=tc_id,
                round_entry=round_entry)
            _spawn_cb = _make_run_command_spawn_cb(
                task, rn, round_entry)
            _progress_cb = _make_run_command_progress_cb(
                task, rn, round_entry, _cmd,
                runtime_context=_runtime_context,
                lifecycle=_spawn_cb)
            _extra_kw = {
                'stdin_callback': _stdin_cb,
                'on_chunk': _progress_cb,
                # The spawn clock (). Without this the project
                # mode — the COMMON case — published no deadlineTs and
                # forced no checkpoint, so the countdown silently never
                # appeared exactly where it mattered most. The same factory
                # _handle_code_exec uses, so both entry points share ONE
                # implementation (the lesson of the entrance-count drift).
                'on_spawn': _spawn_cb,
                'on_grep_intercept': _make_grep_intercept_cb(
                    task, rn, round_entry),
                'task': task,  # enable cooperative abort of subprocesses

                'runtime_context': _runtime_context,
                'recorded_file_changes': recorded_file_changes,
            }
        tool_content = ''
        command_failed = True
        try:
            if fn_name == 'run_command' and project_path:
                from lib.tasks_pkg.handlers._background_command import (
                    run_with_steer_handoff,
                )

                def _detach_progress():
                    detach = getattr(_progress_cb, 'detach', None)
                    if callable(detach):
                        detach()

                def _execute_run(command_task):
                    command_kw = dict(_extra_kw)
                    command_kw['task'] = command_task
                    return execute_tool(
                        fn_name, fn_args, project_path,
                        conv_id=root_conv_id, task_id=task['id'],
                        **command_kw,
                    )

                tool_content = run_with_steer_handoff(
                    task=task,
                    config=cfg,
                    command=_cmd,
                    execute=_execute_run,
                    on_detach=_detach_progress,

                    on_handoff=lambda command_id: round_entry.update(backgroundCommandId=command_id),
                    round_num=rn,
                    tool_call_id=tc_id,
                )
            else:
                tool_content = (
                    execute_tool(
                        fn_name, execution_args, project_path,
                        conv_id=root_conv_id, task_id=task['id'],
                        **_extra_kw,
                    ) if project_path else 'Error: No project path.'
                )
            command_failed = False
        finally:
            if _spawn_cb is not None:
                _spawn_cb.finish()
            # Flush any buffered run_command output tail.
            if _progress_cb is not None:
                try:
                    _progress_cb.flush()
                except Exception as e:
                    logger.debug('[run_command] progress flush failed: %s', e)

                tool_content = finalize_command_output(
                    task, round_entry, _progress_cb, tool_content,
                    runtime_context=_runtime_context, command=_cmd,
                    tool_call_id=tc_id, command_failed=command_failed)

    return LocalProjectResult(
        tool_content, recorded_file_changes=tuple(recorded_file_changes))
