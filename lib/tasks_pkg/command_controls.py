"""Apply interactive command controls at the process that owns TaskRuntime.

Entry points accept an explicit owner and resolve the existing foreground
command slots. They never create a process or treat request delivery as a
successful handoff; the command worker owns the actual result.
"""
import time

from lib.tasks_pkg.manager.runtime import chat_task_runtime


def interrupt_command(task_id: str, *, user_id: int, deadline_unix_ms: int | None = None):
    if deadline_unix_ms is not None and deadline_unix_ms <= time.time_ns() // 1_000_000:
        raise TimeoutError('Command control expired before application')
    task = chat_task_runtime.get_owned(task_id, user_id=user_id)
    if task is None:
        return None
    if task.get('status') != 'running' or task.get('aborted'):
        return {'interrupted': False, 'reason': 'task_not_running'}
    pid = task.get('_subprocess_pid')
    if not pid:
        return {'interrupted': False, 'reason': 'no_active_command'}
    chat_task_runtime.update_fields(task_id, fields={'_cmd_interrupt': {
        'source': 'user', 'ts': time.time(), 'note': '', 'pid': pid,
    }}, only_if_status='running')
    return {'interrupted': True, 'pid': pid}


def background_command(task_id: str, *, user_id: int, round_num: int, tool_call_id: str,
                       deadline_unix_ms: int | None = None):
    if deadline_unix_ms is not None and deadline_unix_ms <= time.time_ns() // 1_000_000:
        raise TimeoutError('Command control expired before application')
    task = chat_task_runtime.get_owned(task_id, user_id=user_id)
    if task is None:
        return None
    abort_event = task.get('abort_event')
    if (task.get('status') != 'running' or task.get('aborted')
            or (abort_event is not None and abort_event.is_set())):
        return {'backgrounded': False, 'reason': 'task_not_running'}
    control = task.get('_cmd_handoff_control')
    if control is None or control.task is not task:
        return {'backgrounded': False, 'reason': 'no_active_command'}
    options = {} if deadline_unix_ms is None else {'deadline_unix_ms': deadline_unix_ms}
    return control.request(round_num=round_num, tool_call_id=tool_call_id, **options)


def apply_command_control(method, task_id, *, user_id, deadline_unix_ms=None, **params):
    if method == 'interrupt' and not params:
        return interrupt_command(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'background' and set(params) == {'round_num', 'tool_call_id'}:
        return background_command(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms, **params)
    raise ValueError('Unsupported execution command control')
