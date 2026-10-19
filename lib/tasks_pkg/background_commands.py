"""Process-local control registry for explicit background commands.

Conversation completions use the existing durable queue + inbox handoff lane.
An idle conversation gets one collective wake only after all registered
commands have published their durable results.
Headless commands retain queryable results until completed-history eviction or process
exit; running entries are never evicted. Controls are scoped to their owner.

Durability (POSIX): every background command also writes a record directory
under ``data/background_commands/<command_id>/`` — its output streams to
files (never a pipe that dies with the server) and a wrapper writes an
exit-code sentinel. After a restart the sweep below delivers finished
results, reattaches to still-running processes (queryable via ``poll``,
stoppable via ``kill``), and reports processes that died with the server.
The model never learns about the restart unless the command actually died.
"""
from __future__ import annotations

import contextvars
import json
import os
import shlex
import shutil
import threading
import time
import uuid
from typing import Any

from lib.log import get_logger
from lib.tools.resource_policy import tool_result_cache_capacity

logger = get_logger(__name__)
# Mirrors the await_agents hard cap so both wait primitives share one contract.
WAIT_BACKGROUND_COMMAND_MAX_S = 60.0
REATTACH_POLL_S = 2.0
REATTACH_MAX_S = 24 * 3600.0
_registry: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_recovery_in_progress = False
_recovery_scan_failed = False
_recovery_incomplete_conversations: set[tuple[int, str]] = set()



def freeze_completion_envelope(*, command_id: str, conv_id: str, user_id: int,
                               message: dict, config: dict) -> dict[str, Any]:
    """Freeze the complete request before enqueue, across maintenance/restart.

    The existing command registry owns memory lifetime; its existing POSIX
    record directory owns restart lifetime. The envelope is committed before
    queue publication, so a lost ACK can only replay identical bytes.
    """
    import json
    import os
    import tempfile
    from lib.message_queue import freeze_queue_payload
    from lib.storage.protocol import MAX_FRAME_BYTES
    from lib.storage.errors import StorageError

    identity = {'command_id': command_id, 'conv_id': conv_id, 'user_id': user_id}
    proposed = {**identity, 'message': freeze_queue_payload(message), 'config': freeze_queue_payload(config)}
    with _lock:
        entry = _registry.get(command_id)
        if entry is not None and (entry.get('conv_id') != conv_id
                                  or entry.get('user_id') != user_id):
            raise ValueError('background completion owner differs')
        encoded = entry.get('completion_envelope') if entry is not None else None
        record_dir = os.path.join(_records_root(), command_id)
        persistent = (_posix_durable() and os.path.basename(command_id) == command_id
                      and os.path.isdir(record_dir))
        path = os.path.join(record_dir, 'delivery.json')
        if encoded is None and persistent:
            try:
                with open(path, encoding='utf-8') as stream:
                    encoded = stream.read(MAX_FRAME_BYTES + 1)
            except FileNotFoundError:
                pass  # No publication attempt has frozen this record yet.
        if encoded is None:
            encoded = json.dumps(proposed, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            if len(encoded.encode('utf-8')) > MAX_FRAME_BYTES:
                raise StorageError('database_resource_exhausted', 'Background completion envelope exceeds frame bound')
            if persistent:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                            dir=record_dir, prefix='.delivery-', delete=False) as stream:
                        temporary = stream.name
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                    temporary = None
                    directory_fd = os.open(record_dir, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    if temporary is not None:
                        os.unlink(temporary)
            if entry is not None:
                entry['completion_envelope'] = encoded
        if len(encoded.encode('utf-8')) > MAX_FRAME_BYTES:
            raise StorageError('database_resource_exhausted', 'Background completion envelope exceeds frame bound')
        frozen = json.loads(encoded)
        if any(frozen.get(key) != value for key, value in identity.items()):
            raise ValueError('background completion envelope identity differs')
        return frozen


def release_completion_envelope(*, command_id: str, conv_id: str, user_id: int) -> None:
    """Release frozen retry bytes only after queue acknowledgement, owner-fenced."""
    with _lock:
        entry = _registry.get(command_id)
        if (entry is not None and entry.get('conv_id') == conv_id
                and entry.get('user_id') == user_id):
            entry.pop('completion_envelope', None)


def conversation_results_complete(*, conv_id: str, user_id: int) -> bool:
    """True when every known command for this owner/conversation is published.

    A finished process is still pending until its durable receipt commits.
    Startup scans all records before allowing a completion wake, so a result
    encountered early in that scan cannot race a still-running sibling.
    """
    with _lock:
        if (_recovery_in_progress or _recovery_scan_failed
                or (user_id, conv_id) in _recovery_incomplete_conversations):
            return False
        return all(
            entry['done'].is_set()
            and entry['state']['delivery_status'] == 'queued'
            for entry in _registry.values()
            if entry.get('conv_id') == conv_id and entry.get('user_id') == user_id
        )


def conversation_has_known_commands(*, conv_id: str, user_id: int) -> bool:
    with _lock:
        return ((user_id, conv_id) in _recovery_incomplete_conversations
                or any(entry.get('conv_id') == conv_id
                   and entry.get('user_id') == user_id
                   for entry in _registry.values()))


def task_has_running_commands(*, task_id: str, user_id: int) -> bool:
    """Fence workspace capture/reclamation until its supervised children exit."""
    with _lock:
        if _recovery_in_progress or _recovery_scan_failed:
            return True
        return any(entry.get('user_id') == user_id
                   and str((entry.get('task') or {}).get('id') or '') == task_id
                   and not entry['done'].is_set() for entry in _registry.values())


def recovery_needs_retry() -> bool:
    with _lock:
        return _recovery_scan_failed or bool(_recovery_incomplete_conversations)


def wake_completed_background_batch(conv_id: str, user_id: int | None) -> None:
    if not conv_id or user_id is None:
        return
    try:
        from lib.message_queue import dispatch_completed_background_batch
        dispatch_completed_background_batch(conv_id, user_id=int(user_id))
    except Exception as exc:
        logger.warning('[BackgroundCommand] batch wake deferred conv=%s: %s',
                       conv_id[:8], exc)


class BackgroundCommandTask(dict):
    """Only execution identity/policy crosses the detached lifetime boundary.

    Parent Stop and PID/interrupt state must not leak into an explicitly
    independent command. Neither transcript nor tool-round objects are retained.
    """

    def __init__(self, parent: dict[str, Any]):
        super().__init__({key: parent[key] for key in (
            'id', 'convId', '_userId', '_unattended', '_via_agent_run',
            '_compat_openai', '_compat_anthropic', '_api_key_id',
        ) if key in parent})
        self['_background_isolated'] = True
        self['_background_detached'] = True
        self['aborted'] = False

        from copy import deepcopy
        from lib.tasks_pkg.workspace_delivery import (
            execution_repositories, ensure_execution_roots)
        repositories = execution_repositories(parent)
        if repositories:
            self['_workspaceExecution'] = {'repositories': deepcopy(repositories)}
        parent_policy = ensure_execution_roots(parent)
        if parent_policy is not None:
            self['_workspaceRootPolicy'] = deepcopy(parent_policy)
            config = parent.get('config') or {}
            self['config'] = {key: deepcopy(config[key]) for key in (
                'projectPath', 'projectPaths', 'readOnlyPaths', 'canonicalProjectPath')
                if key in config}

def background_command_result(command: str, command_id: str, *,
                              task: dict[str, Any],
                              has_conversation: bool = True) -> str:
    from lib.tools.background_guidance import background_command_guidance

    guidance = background_command_guidance(
        authority_catalog=task.get('_executable_tool_catalog'),
        has_conversation=has_conversation,
    )
    return (f'$ {command}\n\n[Command moved to background: {command_id}. '
            f'{guidance}]')


def start_background_command(*, task: dict[str, Any], command: str,
                             execute: Any, config: dict | None = None) -> str:
    """Register controls before starting exactly one detached worker.

    ``execute`` is called as ``execute(bg_task, exec_ctx)`` where exec_ctx
    carries ``command_id``, ``wrapped_command`` (POSIX durable wrapper, or
    None on platforms without it), ``note_spawn(pid, pgid)``, and
    ``build_result(aborted=...)``. Callers must supply an execution closure
    that does not retain foreground presentation/task objects. A failed
    thread start releases its registry entry and durable record.
    """
    from lib.tasks_pkg.handlers._background_command import _delivery_config

    command_id = f'bg_{uuid.uuid4().hex[:12]}'
    bg_task = BackgroundCommandTask(task)
    delivery_task = {key: bg_task.get(key) for key in ('convId', '_userId')}
    delivery_config = _delivery_config(config)
    done = threading.Event()
    state: dict[str, Any] = {
        'command': command, 'result': None, 'error': None,
        'finished': False, 'started_at': time.time(), 'finished_at': None,
        'delivery_status': ('pending' if delivery_task.get('convId')
                            and delivery_task.get('_userId') else 'not_applicable'),
    }
    paths, record = _create_record(command_id, command, task,
                                   state['started_at'])

    def _note_spawn(pid: Any, pgid: Any) -> None:
        if record is None or not pid:
            return
        try:
            record.update({
                'pid': int(pid),
                'pgid': int(pgid) if pgid else None,
                'pid_starttime': _pid_starttime(int(pid)),
            })
            _write_record(paths, record)
        except (OSError, TypeError, ValueError) as exc:
            logger.debug('[BackgroundCommand:%s] spawn note failed: %s',
                         command_id, exc)

    exec_ctx = {
        'command_id': command_id,
        'wrapped_command': (_wrap_for_record(command, paths)
                            if paths is not None else None),
        'note_spawn': _note_spawn,
        'build_result': (lambda aborted=False:
                         _result_from_record(record, paths, aborted=aborted)),
    }
    # The execution context carries command restrictions/credentials. It is
    # owned only by the worker, not retained in the completed registry entry.
    copied_context = contextvars.copy_context()
    entry = {'done': done, 'state': state, 'task': bg_task,
             'launch_task_id': str(task.get('id') or ''),
             'user_id': task.get('_userId'), 'conv_id': task.get('convId') or ''}

    def _worker() -> None:
        error = None
        try:
            result = execute(bg_task, exec_ctx)
        except BaseException as exc:
            logger.debug('background command worker failed: %s',
                         type(exc).__name__)
            error = f'{type(exc).__name__}: {exc}'
            result = (f'$ {command}\n\nBackground execution failed with '
                      f'{error}\n[exit code: -1]')
        # Publish a complete result atomically before attempting delivery. No
        # exception/traceback (and thus parent frames) lives in the registry.
        with _lock:
            state.update(result=str(result or ''), error=error, finished=True,
                         finished_at=time.time())
            entry['task'] = None
            done.set()
        if delivery_task.get('convId') and delivery_task.get('_userId'):
            from lib.tasks_pkg.handlers._background_command import _queue_completion
            published = _queue_completion(
                task=delivery_task, config=delivery_config,
                command_id=command_id, command=command, result=state['result'])
            with _lock:
                state['delivery_status'] = 'queued' if published else 'failed'
        # The durable record survives ONLY an undelivered result; anything
        # delivered (or with nowhere to deliver) is settled.
        if paths is not None and state['delivery_status'] in (
                'queued', 'not_applicable'):
            _remove_record_dir(paths['dir'])
        if state['delivery_status'] == 'queued':
            wake_completed_background_batch(delivery_task['convId'], delivery_task['_userId'])
        with _lock:
            _prune_finished()
        logger.info('[BackgroundCommand:%s] finished', command_id)

    with _lock:
        _registry[command_id] = entry
    try:
        worker = threading.Thread(
            target=lambda: copied_context.run(_worker),
            name=f'bg-cmd-{command_id[3:]}', daemon=True)
        worker.start()
    except BaseException:
        with _lock:
            _registry.pop(command_id, None)
        if paths is not None:
            _remove_record_dir(paths['dir'])
        raise
    return command_id


def register_handed_off_command(*, command_id: str, task: dict[str, Any],
                                command: str, done: threading.Event) -> bool:
    """Adopt a live foreground subprocess before its tool call settles.

    The foreground worker still owns its pipes. This process-local control
    record makes poll and kill work while it drains; unlike an explicitly
    backgrounded command, its already-running shell has no restart record.
    """
    with _lock:
        spawned_at = getattr(task, 'spawned_at', None)
        elapsed = max(0.0, time.monotonic() - spawned_at) if spawned_at else 0.0
        _registry[command_id] = {
            'done': done,
            'task': task,
            'launch_task_id': str(task.get('id') or ''),
            'user_id': task.get('_userId'),
            'conv_id': task.get('convId') or '',
            'state': {
                'command': command, 'result': None, 'error': None,
                'finished': False, 'started_at': time.time() - elapsed,
                'finished_at': None, 'delivery_status': 'pending',
            },
        }
    return True


def unregister_handed_off_command(*, command_id: str, task: dict[str, Any]) -> None:
    """Release an uncommitted handoff if Stop wins before detachment."""
    with _lock:
        entry = _registry.get(command_id)
        if entry is not None and entry['task'] is task and not entry['done'].is_set():
            del _registry[command_id]


def finish_handed_off_command(*, command_id: str, result: str,
                              error: str | None) -> None:
    with _lock:
        entry = _registry[command_id]
        entry['state'].update(result=result, error=error, finished=True,
                              finished_at=time.time())
        entry['task'] = None
        entry['done'].set()


def set_handed_off_delivery(command_id: str, published: bool) -> None:
    with _lock:
        entry = _registry.get(command_id)
        if entry is not None:
            entry['state']['delivery_status'] = ('queued' if published
                                                  else 'failed')
            _prune_finished()


def _prune_finished() -> None:
    """Bound only replayable completed history, with _lock held.

    Active commands and unpublished results never compete with history for
    admission. Failed delivery stays registered for retry and batch fencing.
    The shared launch-probed receipt budget sizes history, never execution.
    """
    finished = [(cid, entry) for cid, entry in _registry.items()
                if entry['done'].is_set()
                and entry['state']['delivery_status'] in ('queued', 'not_applicable')]
    excess = len(finished) - tool_result_cache_capacity()
    if excess > 0:
        finished.sort(key=lambda item: item[1]['state']['finished_at'])
        for cid, _ in finished[:excess]:
            del _registry[cid]


def _owned(entry: dict | None, user_id: Any) -> bool:
    # None is the explicit anonymous/headless scope, never a privileged bypass.
    return entry is not None and entry['user_id'] == user_id


def _snapshot(command_id: str, entry: dict) -> dict[str, Any]:
    state = entry['state']
    finished = entry['done'].is_set()
    return {
        'command_id': command_id, 'command': state['command'],
        'conv_id': entry.get('conv_id', ''),
        'status': 'completed' if finished else 'running',
        'result': state['result'] if finished else None,
        'error': state['error'], 'started_at': state['started_at'],
        'delivery_status': state['delivery_status'],
        'elapsed_s': round((state['finished_at'] or time.time())
                           - state['started_at'], 1),
    }


def check_background_command(command_id: str, *, user_id: Any = None,
                             wait_s: float = 0, task_id: str | None = None) -> dict | None:
    """Snapshot a command; with ``wait_s`` block until completion or the cap.

    An optional task_id restricts provenance to the original launching task,
    whose small identity survives release of the completed task object.
    Waiting is idempotent: a completed result can be re-read any number of
    times, so a caller whose wait hit the cap simply calls again.
    """
    try:
        requested = max(0.0, float(wait_s or 0))
    except (TypeError, ValueError):
        requested = 0.0
    deadline = time.monotonic() + min(requested, WAIT_BACKGROUND_COMMAND_MAX_S)
    while True:
        with _lock:
            entry = _registry.get(command_id)
            if (not _owned(entry, user_id) or (task_id is not None
                    and entry.get('launch_task_id',str((entry.get('task') or {}).get('id') or '')) != task_id)):
                return None
            snapshot = _snapshot(command_id, entry)
            done = entry['done']
            remaining = deadline - time.monotonic()
        if snapshot['status'] == 'completed' or remaining <= 0:
            return snapshot
        done.wait(timeout=remaining)


def kill_background_command(command_id: str, *, user_id: Any = None, task_id: str | None = None) -> bool:
    """Signal only this owner's live command; never inherit or kill parent PID."""
    with _lock:
        entry = _registry.get(command_id)
        if (not _owned(entry, user_id) or entry['done'].is_set() or (task_id is not None
                and entry.get('launch_task_id',str((entry.get('task') or {}).get('id') or '')) != task_id)):
            return False
        entry['task']['aborted'] = True
    return True


def list_background_commands(*, user_id: Any = None) -> list[dict[str, Any]]:
    with _lock:
        result = []
        for cid, entry in _registry.items():
            if not _owned(entry, user_id):
                continue
            item = _snapshot(cid, entry)
            item.pop('result')
            item.pop('error')
            item['command'] = item['command'][:200]
            result.append(item)
        return result


def task_effect_snapshot(*, task_id: str, user_id: Any) -> dict:
    """Observe this task's registered commands and registry recovery under one lock.

    Empty commands do not prove absence of effects when recovery is incomplete.
    Results, credentials and task objects never enter this control snapshot.
    """
    if not isinstance(task_id,str) or not task_id:
        raise ValueError('background effect snapshot requires a task identity')
    with _lock:
        commands=[{'command_id':cid,'status':'completed' if entry['done'].is_set() else 'running'}
            for cid,entry in _registry.items() if _owned(entry,user_id)
            and entry.get('launch_task_id',str((entry.get('task') or {}).get('id') or ''))==task_id]
        return {'recovery_pending':bool(_recovery_in_progress or _recovery_scan_failed),
                'commands':sorted(commands,key=lambda row:row['command_id'])}


def retry_failed_background_deliveries() -> int:
    """Retry due receipts fairly, with one serial publisher and bounded work.

    Failed originals stay recoverable. Exponential backoff bounds serialization,
    storage calls and error logs; success releases the existing durable record.
    """
    now = time.monotonic()
    deadline = now + 1.0
    with _lock:
        failed = sorted(
            ((entry['state'].get('delivery_retry_at', 0.0), command_id)
             for command_id, entry in _registry.items()
             if entry['done'].is_set()
             and entry['state'].get('delivery_status') == 'failed'
             and not entry['state'].get('delivery_inflight')
             and entry.get('conv_id') and entry.get('user_id') is not None
             and entry['state'].get('delivery_retry_at', 0.0) <= now),
        )
    published_count = 0
    for _, command_id in failed:
        if time.monotonic() >= deadline:
            break
        with _lock:
            entry = _registry.get(command_id)
            if (entry is None or entry['state'].get('delivery_status') != 'failed'
                    or entry['state'].get('delivery_inflight')
                    or entry['state'].get('delivery_retry_at', 0.0) > time.monotonic()):
                continue
            state = entry['state']
            state['delivery_inflight'] = True
            conv_id, user_id = entry['conv_id'], entry['user_id']
            command, result = state.get('command') or '', state.get('result') or ''
        published = False
        try:
            from lib.tasks_pkg.handlers._background_command import _queue_completion
            published = _queue_completion(
                task={'convId': conv_id, '_userId': user_id}, config=None,
                command_id=command_id, command=command, result=result,
            )
        finally:
            with _lock:
                state.pop('delivery_inflight', None)
                if published:
                    state['delivery_status'] = 'queued'
                    state.pop('delivery_retry_at', None)
                    state.pop('delivery_retry_attempts', None)
                else:
                    attempts = min(9, state.get('delivery_retry_attempts', 0) + 1)
                    state['delivery_retry_attempts'] = attempts
                    state['delivery_retry_at'] = time.monotonic() + min(300.0, 2.0 ** attempts)
        if not published:
            continue
        if _posix_durable():
            _remove_record_dir(os.path.join(_records_root(), command_id))
        wake_completed_background_batch(conv_id, user_id)
        published_count += 1
    with _lock:
        _prune_finished()
    return published_count


# ── Durable records (POSIX) ────────────────────────────────
# A record directory is the restart-surviving authority for one command:
# ``record.json`` (identity + pid), ``stdout.log`` / ``stderr.log`` (the
# command writes here directly — no pipe that dies with the server), and
# ``exit_code`` (sentinel written by the shell wrapper; its absence after
# the process is gone means "died before completion").

_RECORD_READ_CAP = 200_000


def _posix_durable() -> bool:
    return os.name == 'posix'


def _records_root() -> str:
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), 'background_commands')


def _record_paths(rdir: str) -> dict[str, str]:
    return {
        'dir': rdir,
        'record': os.path.join(rdir, 'record.json'),
        'stdout': os.path.join(rdir, 'stdout.log'),
        'stderr': os.path.join(rdir, 'stderr.log'),
        'exit': os.path.join(rdir, 'exit_code'),
    }


def _create_record(command_id: str, command: str, task: dict,
                   started_at: float):
    """Mint the record directory; (paths, record) or (None, None)."""
    if not _posix_durable():
        return None, None
    rdir = os.path.join(_records_root(), command_id)
    paths = _record_paths(rdir)
    record = {
        'command_id': command_id, 'command': command,
        'task_id': str(task.get('id') or ''),
        'conv_id': str(task.get('convId') or ''),
        'user_id': task.get('_userId'),
        'started_at': started_at,
        'pid': None, 'pgid': None, 'pid_starttime': None,
    }
    try:
        os.makedirs(rdir, exist_ok=False)
        _write_record(paths, record)
    except OSError as exc:
        logger.debug('[BackgroundCommand:%s] durable record unavailable: %s',
                     command_id, exc)
        return None, None
    return paths, record


def _write_record(paths: dict, record: dict) -> None:
    tmp = paths['record'] + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(record, fh)
    os.replace(tmp, paths['record'])


def _read_record(rdir: str) -> dict | None:
    try:
        with open(os.path.join(rdir, 'record.json'), encoding='utf-8') as fh:
            record = json.load(fh)
        if isinstance(record, dict) and record.get('command_id'):
            return record
    except (OSError, ValueError):
        pass
    return None


def _remove_record_dir(rdir: str) -> None:
    try:
        shutil.rmtree(rdir, ignore_errors=True)
    except OSError as exc:
        logger.debug('[BackgroundCommand] record cleanup failed: %s', exc)


def _wrap_for_record(command: str, paths: dict) -> str:
    """POSIX wrapper: streams to files, exit code to a sentinel.

    The subshell shares the process group the upstream spawn creates
    (start_new_session), so tree-kill semantics are unchanged; killing the
    wrapper prevents the sentinel from being written, which the reattach
    path correctly reads as "died before completion".
    """
    return (f'( {command}\n) > {shlex.quote(paths["stdout"])} '
            f'2> {shlex.quote(paths["stderr"])}; '
            f'printf %s "$?" > {shlex.quote(paths["exit"])}')


def _read_bounded(path: str) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as fh:
            if size <= _RECORD_READ_CAP:
                data = fh.read()
            else:
                half = _RECORD_READ_CAP // 2
                head = fh.read(half)
                fh.seek(max(half, size - half))
                data = head + b'\n...[middle omitted]...\n' + fh.read()
        return data.decode('utf-8', errors='replace')
    except OSError:
        return ''


def _read_exit_code(paths: dict) -> int | None:
    try:
        with open(paths['exit'], encoding='ascii') as fh:
            return int(fh.read().strip() or '0')
    except (OSError, ValueError):
        return None


def _result_from_record(record: dict, paths: dict, *, aborted: bool = False,
                        interrupted_by: str = '') -> str:
    from lib.project_mod.run_command import _format_run_output
    stdout = _read_bounded(paths['stdout'])
    stderr = _read_bounded(paths['stderr'])
    if aborted:
        return _format_run_output(record['command'], stdout, stderr, -1,
                                  aborted=True)
    if interrupted_by:
        return _format_run_output(record['command'], stdout, stderr, -1,
                                  interrupted_by=interrupted_by)
    code = _read_exit_code(paths)
    return _format_run_output(record['command'], stdout, stderr,
                              code if code is not None else -1)


def _pid_starttime(pid: int) -> str | None:
    """Linux start-time token from the shared comm-safe process-stat parser."""
    from lib.project_mod.process_group import ProcessGroupRetirementError, read_process_fields
    try:
        return read_process_fields(pid)[19].decode('ascii')
    except (OSError, IndexError, TypeError, ValueError, UnicodeError, ProcessGroupRetirementError):
        return None


def _record_pid_alive(record: dict) -> bool:
    pid = record.get('pid')
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    expected = record.get('pid_starttime')
    if expected:
        return _pid_starttime(int(pid)) == expected
    return True


def _kill_record_tree(record: dict) -> bool:
    """Return confirmed retirement, never equate missing signal authority to it."""
    import sys
    from lib.project_mod.process_group import retire_process_group
    token = record.get('pid_starttime')
    if sys.platform.startswith('linux') and not (
            isinstance(token, str) and token.isascii() and token.isdecimal()):
        return False  # Numeric PID/PGID existence is not recovered signal authority.
    if not _record_pid_alive(record):
        return False  # An absent/stale leader may still have surviving descendants.
    try:
        pgid = int(record['pgid'])
        if pgid <= 1 or pgid == os.getpgrp() or os.getpgid(int(record['pid'])) != pgid:
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    retire_process_group(pgid, leader_alive=lambda: _record_pid_alive(record))
    return True


def _deliver_record(record: dict, result: str) -> bool:
    """Queue the completion through the standard lane. True when settled —
    delivered, or with nowhere to deliver (headless)."""
    if not record.get('conv_id') or record.get('user_id') is None:
        return True
    from lib.tasks_pkg.handlers._background_command import _queue_completion
    delivery_task = {'convId': record['conv_id'], '_userId': record['user_id']}
    return bool(_queue_completion(
        task=delivery_task, config=None, command_id=record['command_id'],
        command=record['command'], result=result))


_DIED_NOTE = ('server restart — the recorded process is no longer running and '
              'no exit receipt is available; partial output above. Completion '
              'and effects are unconfirmed. Inspect outputs and current state '
              'before repeating any operation; do not automatically replay writes. '
              'Continue independent work')


def _reattach(record: dict, paths: dict) -> None:
    """Re-register a surviving process and watch it to settlement."""

    try:
        group_id = int(record['pgid'])
        if isinstance(record['pgid'], bool) or group_id <= 1 or group_id == os.getpgrp():
            raise ValueError('Recovered record has no valid owned process group')
    except (KeyError, TypeError, ValueError) as error:
        # Startup recovery retains the unreadable/unsafe record and reports
        # incomplete recovery; never manufacture a worker or signal authority.
        raise ValueError('Cannot reattach an unverified process-group record') from error
    command_id = record['command_id']
    done = threading.Event()
    state: dict[str, Any] = {
        'command': record['command'], 'result': None, 'error': None,
        'finished': False,
        'started_at': record.get('started_at') or time.time(),
        'finished_at': None,
        'delivery_status': ('pending' if record.get('conv_id')
                            and record.get('user_id') is not None
                            else 'not_applicable'),
    }
    task = {'id': record.get('task_id') or f'reattach-{command_id}', 'convId': record.get('conv_id') or '',
            '_userId': record.get('user_id'), 'aborted': False,
            '_background_isolated': True, '_background_detached': True}
    entry = {'done': done, 'state': state, 'task': task,
             'launch_task_id': str(record.get('task_id') or ''),
             'user_id': record.get('user_id'),
             'conv_id': record.get('conv_id') or ''}
    with _lock:
        if command_id in _registry:
            return
        _registry[command_id] = entry

    def _finish(result: str) -> None:
        with _lock:
            state.update(result=result, finished=True,
                         finished_at=time.time())
            entry['task'] = None
            done.set()
        if _deliver_record(record, result):
            with _lock:
                state['delivery_status'] = (
                    'queued' if record.get('conv_id') else 'not_applicable')
            _remove_record_dir(paths['dir'])
        else:
            with _lock:
                state['delivery_status'] = 'failed'
        if state['delivery_status'] == 'queued':
            wake_completed_background_batch(record.get('conv_id') or '', record.get('user_id'))
        with _lock:
            _prune_finished()
        logger.info('[BackgroundCommand:%s] reattached command settled',
                    command_id)

    def _retire_before_settlement() -> None:
        from lib.project_mod.process_group import ProcessGroupRetirementError, group_has_live_members
        try:
            if _kill_record_tree(record) is not True:
                raise ProcessGroupRetirementError('No verified recovered leader identity; no signal was authorized')
        except ProcessGroupRetirementError as error:
            # Do not orphan this watcher or publish an aborted result while an
            # owned descendant may still hold inherited exclusion/resources.
            with _lock:
                state['error'] = 'Cancellation requested; group retirement is not yet confirmed: ' + str(error)
            logger.warning('[BackgroundCommand:%s] retirement remains pending: %s', command_id, error)
            while True:
                try:
                    if not group_has_live_members(int(record['pgid'])):
                        break
                except ProcessGroupRetirementError:
                    pass  # Observation is still unavailable; neither signal nor settle by guess.
                time.sleep(REATTACH_POLL_S)

    def _worker() -> None:
        deadline = time.monotonic() + REATTACH_MAX_S
        while time.monotonic() < deadline:
            if task.get('aborted'):
                _retire_before_settlement()
                _finish(_result_from_record(record, paths, aborted=True))
                return
            if _read_exit_code(paths) is not None:
                _finish(_result_from_record(record, paths))
                return
            if not _record_pid_alive(record):
                _finish(_result_from_record(record, paths,
                                            interrupted_by=_DIED_NOTE))
                return
            time.sleep(REATTACH_POLL_S)
        _retire_before_settlement()
        _finish(_result_from_record(
            record, paths,
            interrupted_by='reattach lifetime exceeded — group retirement observed'))

    thread = threading.Thread(target=_worker, daemon=True,
                              name=f'bg-cmd-reattach-{command_id[3:]}')
    thread.start()


def recover_background_commands_on_startup() -> int:
    """Settle durable records left by a previous process.

    Finished commands deliver their result; still-running ones reattach
    (queryable via poll, stoppable via kill); dead ones deliver a
    died-with-the-server notice with partial output. Nothing is re-run and
    the model learns nothing unless the command actually died.
    """
    global _recovery_in_progress, _recovery_scan_failed
    with _lock:
        _recovery_in_progress = True
    settled = 0
    scan_failed = False
    incomplete: set[tuple[int, str]] = set()
    try:
        try:
            names = (sorted(os.listdir(_records_root()))
                     if _posix_durable() else [])
        except FileNotFoundError:
            names = []
        except OSError as exc:
            logger.warning('[BackgroundCommand] record scan failed: %s', exc)
            names = []
            scan_failed = True
        for command_id in names:
            rdir = os.path.join(_records_root(), command_id)
            if not os.path.isdir(rdir):
                continue
            # Maintenance may retry this scan while new commands are being
            # created. Their process-local owner already watches the record.
            with _lock:
                if command_id in _registry:
                    continue
            paths = _record_paths(rdir)
            record = _read_record(rdir)
            if record is None:
                # A writer can have created the directory but not yet
                # atomically published record.json. Never delete that state.
                scan_failed = True
                logger.warning('[BackgroundCommand:%s] record metadata unreadable; '
                               'retrying recovery', command_id)
                continue
            identity = None
            if record.get('conv_id') and record.get('user_id') is not None:
                try:
                    identity = (int(record['user_id']), str(record['conv_id']))
                except (TypeError, ValueError):
                    pass
            try:
                if _read_exit_code(paths) is not None:
                    delivered = _deliver_record(
                        record, _result_from_record(record, paths))
                    if delivered:
                        _remove_record_dir(rdir)
                elif _record_pid_alive(record):
                    _reattach(record, paths)
                    delivered = True  # The live registry entry blocks wake.
                else:
                    delivered = _deliver_record(record, _result_from_record(
                        record, paths, interrupted_by=_DIED_NOTE))
                    if delivered:
                        _remove_record_dir(rdir)
                if not delivered and identity is not None:
                    incomplete.add(identity)
                settled += 1
            except Exception as exc:
                if identity is not None:
                    incomplete.add(identity)
                logger.warning('[BackgroundCommand:%s] startup recovery failed: '
                               '%s', command_id, exc)
    finally:
        with _lock:
            _recovery_in_progress = False
            _recovery_scan_failed = scan_failed
            _recovery_incomplete_conversations.clear()
            _recovery_incomplete_conversations.update(incomplete)
    if settled:
        logger.info('[BackgroundCommand] startup recovery settled %d '
                    'record(s)', settled)
    # Receipts committed before the crash have no remaining process record.
    # The owner-scoped queue scan recovers their one collective wake as well.
    if scan_failed:
        return settled
    try:
        from lib.message_queue import drain_idle_background_batches
        drain_idle_background_batches()
    except Exception as exc:
        logger.warning('[BackgroundCommand] startup batch scan deferred: %s', exc)
    return settled


__all__ = ['start_background_command', 'background_command_result',
           'check_background_command', 'kill_background_command',
           'list_background_commands', 'conversation_results_complete',
           'conversation_has_known_commands', 'wake_completed_background_batch',
           'retry_failed_background_deliveries', 'recovery_needs_retry',
           'recover_background_commands_on_startup']
