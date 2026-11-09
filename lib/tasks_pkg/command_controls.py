"""Apply interactive controls at the process that owns TaskRuntime and its swarms.

Entry points accept an explicit owner and resolve the existing foreground
command slots or swarm sessions of THIS process. They never create a process or
treat request delivery as a successful handoff; the command worker and the
swarm session own the actual result. ``None`` means this process holds no such
task or session, so a split API asks the execution incarnation instead.
``storage_rpc_metrics`` reports this process's storage RPC histogram for the
API's admin ``/metrics``; it carries no owner data.
"""
import time

from lib.tasks_pkg.manager.runtime import chat_task_runtime


def _admit(deadline_unix_ms):
    if deadline_unix_ms is not None and deadline_unix_ms <= time.time_ns() // 1_000_000:
        raise TimeoutError('Command control expired before application')


def interrupt_command(task_id: str, *, user_id: int, deadline_unix_ms: int | None = None):
    _admit(deadline_unix_ms)
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
    _admit(deadline_unix_ms)
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


def abort_swarm_session(task_id: str, *, user_id: int, deadline_unix_ms: int | None = None):
    _admit(deadline_unix_ms)
    from lib.swarm.integration import abort_swarm
    result = abort_swarm(task_id, user_id=user_id)
    if result.get('success'):
        return {'aborted': True}
    if result.get('outcome') == 'failed':
        return {'aborted': False, 'reason': 'abort_failed'}
    return None


def stop_swarm_session_agent(swarm_id: str, *, user_id: int, agent_id: str,
                             deadline_unix_ms: int | None = None):
    _admit(deadline_unix_ms)
    from lib.swarm.integration import stop_swarm_agent
    result = stop_swarm_agent(swarm_id, agent_id, user_id=user_id)
    if result['outcome'] == 'not_found':
        return None
    if result['outcome'] == 'stopped':
        return {'stopped': True, 'status': result['status']}
    return {'stopped': False, 'reason': 'not_running', 'status': result['status']}


def background_activity(*, user_id: int, deadline_unix_ms: int | None = None):
    """This process's live swarm sessions and running background commands."""
    _admit(deadline_unix_ms)
    from lib.swarm.integration import list_active_swarms
    from lib.tasks_pkg.background_commands import list_background_commands
    return {'swarms': list_active_swarms(user_id=user_id),
            'backgroundCommands': [item for item in list_background_commands(user_id=user_id)
                                   if item.get('status') == 'running']}


def storage_rpc_metrics(*, user_id: int, deadline_unix_ms: int | None = None):
    """This process's storage RPC latency histogram; it holds no owner data."""
    _admit(deadline_unix_ms)
    from lib.observability import storage_rpc_snapshot
    return {'histograms': storage_rpc_snapshot()}


def merge_background_activity(*views):
    """Union per-process views; one swarm run or command appears once."""
    merged = {'swarms': [], 'backgroundCommands': []}
    for field, identity in (('swarms', 'swarm_id'), ('backgroundCommands', 'command_id')):
        seen = set()
        for view in views:
            for row in view[field]:
                if row.get(identity) not in seen:
                    seen.add(row.get(identity))
                    merged[field].append(row)
    return merged


def apply_command_control(method, task_id, *, user_id, deadline_unix_ms=None, **params):
    if method == 'interrupt' and not params:
        return interrupt_command(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'background' and set(params) == {'round_num', 'tool_call_id'}:
        return background_command(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms, **params)
    if method == 'swarm_abort' and not params:
        return abort_swarm_session(task_id, user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'swarm_agent_stop' and set(params) == {'agent_id'}:
        return stop_swarm_session_agent(task_id, user_id=user_id,
                                        deadline_unix_ms=deadline_unix_ms, **params)
    if method == 'background_activity' and not params and task_id == '':
        return background_activity(user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    if method == 'storage_rpc_metrics' and not params and task_id == '':
        return storage_rpc_metrics(user_id=user_id, deadline_unix_ms=deadline_unix_ms)
    raise ValueError('Unsupported execution command control')
