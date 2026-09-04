"""Signal-driven Project Brain application boundary.

Entry points cover automatic work derivation, read-only projections, prompt
context, versioned checkers, human decision promotion, Watch maintenance, and
ephemeral path-overlap advisories.  Durable state is exclusively owned by the
Sidecar ``project_brain.*`` semantic operations.
"""

from __future__ import annotations

from fnmatch import fnmatch
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid
from typing import Any

from lib.log import get_logger
from lib.storage import get_storage_client

from .project_identity import normalize_project_path, project_channel_key


logger = get_logger(__name__)

WORK_TRIGGER_TODO = 'todo_write'
WORK_TRIGGER_FILE = 'file_write'
WORK_TRIGGER_ISOLATED = 'isolated_workspace'
WORK_TERMINAL_STATUSES = frozenset({'completed', 'failed', 'cancelled'})

_TITLE_PATH = 100
_TITLE_REQUEST = 200
_TITLE_GOAL = 300
_TITLE_TODO_UNFINISHED = 400
_TITLE_TODO_ACTIVE = 500

_WRITE_TOOLS = frozenset({
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
})
_TRANSIENT_ADVISORY_LIMIT = 20
_MAX_CHECKER_OUTPUT_CHARS = 4000
_MAX_CHECKER_OUTPUT_BYTES = _MAX_CHECKER_OUTPUT_CHARS * 4
_MAX_NARRATIVE_BYTES = 720
_overlap_lock = threading.Lock()
_work_signal_lock = threading.Lock()


def _owner(task: dict) -> int:
    from lib.tasks_pkg.manager import task_user_id
    return int(task_user_id(task))


def _command(
    operation: str,
    project_path: str,
    *,
    user_id: int,
    command_id: str,
    **payload: Any,
) -> dict:
    project_key = normalize_project_path(project_path)
    result = get_storage_client(write=True).command(
        operation,
        {
            'owner_user_id': int(user_id),
            'project_key': project_key,
            **payload,
        },
        command_id,
    )
    hint = (result or {}).get('pushHint') if isinstance(result, dict) else None
    if hint:
        _push_project_hint(project_key, int(user_id), dict(hint))
    return result or {}


def _push_project_hint(project_key: str, user_id: int, hint: dict) -> None:
    try:
        from lib.agent_core.push import push_event
        push_event(
            'project', project_channel_key(project_key), hint,
            user_id=int(user_id),
        )
    except Exception as exc:
        logger.debug('[ProjectBrain] push hint skipped: %s', exc)


def read_projection(project_path: str, *, user_id: int) -> dict:
    project_key = normalize_project_path(project_path)
    if not project_key:
        return {
            'version': 1, 'projectKey': '', 'headSequence': 0,
            'checkpointSequence': 0, 'workItems': [], 'narratives': [],
            'charter': {'decisions': []}, 'checkers': [], 'watch': [],
            'attention': [],
        }
    return get_storage_client().query(
        'project_brain.get', {
            'owner_user_id': int(user_id), 'project_key': project_key,
        })


def board_projection(project_path: str, *, user_id: int) -> dict:
    projection = read_projection(project_path, user_id=user_id)
    items = list(projection.get('workItems') or ())
    active = [item for item in items if item.get('status') == 'active']
    recent = [item for item in items if item.get('status') in WORK_TERMINAL_STATUSES]
    recent.sort(key=lambda item: int(item.get('finishedAt') or 0), reverse=True)
    active.sort(key=lambda item: int(item.get('startedAt') or 0), reverse=True)
    return {
        'project': projection.get('projectKey') or '',
        'headSequence': int(projection.get('headSequence') or 0),
        'active': active,
        'recentOutcomes': recent[:100],
    }


def feed_projection(
    project_path: str,
    *,
    user_id: int,
    since_sequence: int = 0,
    limit: int = 100,
) -> dict:
    projection = read_projection(project_path, user_id=user_id)
    events = [dict(item) for item in projection.get('narratives') or ()
              if int(item.get('sequence') or 0) > int(since_sequence or 0)]
    events.sort(key=lambda item: int(item.get('sequence') or 0), reverse=True)
    return {
        'events': events[:max(1, min(int(limit or 100), 200))],
        'headSequence': int(projection.get('headSequence') or 0),
    }


def status_projection(project_path: str, *, user_id: int) -> dict:
    projection = read_projection(project_path, user_id=user_id)
    work = list(projection.get('workItems') or ())
    attention = list(projection.get('attention') or ())
    return {
        'project': projection.get('projectKey') or '',
        'headSequence': int(projection.get('headSequence') or 0),
        'activeCount': sum(1 for item in work if item.get('status') == 'active'),
        'recentOutcomeCount': sum(
            1 for item in work if item.get('status') in WORK_TERMINAL_STATUSES),
        'attentionCount': len(attention),
        'checkerCount': sum(
            1 for item in projection.get('checkers') or () if item.get('enabled')),
        'watchCount': len(projection.get('watch') or ()),
    }


def attention_projection(project_path: str, *, user_id: int) -> dict:
    projection = read_projection(project_path, user_id=user_id)
    items = list(projection.get('attention') or ())
    items.sort(key=lambda item: int(item.get('createdAt') or 0), reverse=True)
    return {'project': projection.get('projectKey') or '', 'items': items}


def charter_projection(project_path: str, *, user_id: int) -> dict:
    projection = read_projection(project_path, user_id=user_id)
    return {
        'project': projection.get('projectKey') or '',
        'decisions': list((projection.get('charter') or {}).get('decisions') or ()),
    }


def checker_catalog(project_path: str, *, user_id: int) -> dict:
    projection = read_projection(project_path, user_id=user_id)
    items = list(projection.get('checkers') or ())
    items.sort(key=lambda item: (
        str(item.get('checkerId') or ''), int(item.get('version') or 0)))
    return {'project': projection.get('projectKey') or '', 'items': items}


def watch_projection(project_path: str, *, user_id: int) -> dict:
    projection = read_projection(project_path, user_id=user_id)
    return {'project': projection.get('projectKey') or '',
            'items': list(projection.get('watch') or ())}


def deterministic_work_id(task_id: str) -> str:
    return 'pw_' + hashlib.sha256(
        str(task_id).encode('utf-8', 'replace')).hexdigest()[:24]


def _first_line(value: Any, limit: int = 500) -> str:
    text = str(value or '').strip()
    return (text.splitlines()[0].strip() if text else '')[:limit]


def _bounded_utf8(value: Any, max_bytes: int) -> str:
    encoded = str(value or '').strip().encode('utf-8', 'replace')
    if len(encoded) <= max_bytes:
        return encoded.decode('utf-8')
    return encoded[:max_bytes].decode('utf-8', 'ignore').rstrip()


def _title_candidate(
    task: dict,
    *,
    todos: list[dict] | None = None,
    first_path: str = '',
) -> tuple[str, int]:
    todo_rows = [item for item in (todos or task.get('_todos') or ())
                 if isinstance(item, dict)]
    active = next((item for item in todo_rows
                   if item.get('status') == 'in_progress'), None)
    if active and _first_line(active.get('content')):
        return _first_line(active.get('content')), _TITLE_TODO_ACTIVE
    unfinished = next((item for item in todo_rows
                       if item.get('status') != 'completed'), None)
    if unfinished and _first_line(unfinished.get('content')):
        return _first_line(unfinished.get('content')), _TITLE_TODO_UNFINISHED
    config = task.get('config') or {}
    goal_title = (
        task.get('goalTitle') or config.get('goalTitle')
        or (task.get('_goal') or {}).get('title')
    )
    if _first_line(goal_title):
        return _first_line(goal_title), _TITLE_GOAL
    if _first_line(task.get('lastUserQuery')):
        return _first_line(task.get('lastUserQuery')), _TITLE_REQUEST
    if _first_line(first_path):
        return _first_line(first_path), _TITLE_PATH
    return 'Project work', _TITLE_PATH


def ensure_work_item(
    task: dict,
    project_path: str,
    *,
    trigger: str,
    todos: list[dict] | None = None,
    first_path: str = '',
) -> str:
    if not project_path or task.get('_transientRuntime'):
        return ''
    task_id = str(task.get('id') or '').strip()
    conversation_id = str(task.get('convId') or '').strip()
    if not (task_id and conversation_id):
        return ''
    work_id = deterministic_work_id(task_id)
    title, priority = _title_candidate(
        task, todos=todos, first_path=first_path)
    if not task.get('_projectWorkId'):
        # todo_write and a write result can arrive on adjacent runtime paths.
        # Serialize only the one first-signal decision so their differing
        # trigger/title payloads cannot race under the same command receipt.
        with _work_signal_lock:
            if not task.get('_projectWorkId'):
                now_ms = int(time.time() * 1000)
                item = {
                    'id': work_id,
                    'taskId': task_id,
                    'conversationId': conversation_id,
                    'title': title,
                    'trigger': trigger,
                    'status': 'active',
                    'changedPaths': [],
                    'artifacts': [],
                    'resultSummary': '',
                    'startedAt': now_ms,
                    'finishedAt': None,
                    '_titlePriority': priority,
                    '_titleRefined': False,
                }
                _command(
                    'project_brain.work.start', project_path,
                    user_id=_owner(task),
                    command_id=f'project-work-start:{work_id}',
                    work_item=item, timestamp=now_ms,
                )
                task['_projectWorkId'] = work_id
                task['_projectWorkTitlePriority'] = priority
                task['_projectWorkPath'] = normalize_project_path(project_path)
    if (task.get('_projectWorkId')
          and priority > int(task.get('_projectWorkTitlePriority') or 0)
          and not task.get('_projectWorkTitleRefined')):
        result = _command(
            'project_brain.work.refine', project_path,
            user_id=_owner(task),
            command_id=f'project-work-refine:{work_id}',
            work_id=work_id, title=title, title_priority=priority,
        )
        if (result.get('event') or {}).get('kind') == 'work_title_refined':
            task['_projectWorkTitlePriority'] = priority
            task['_projectWorkTitleRefined'] = True
    return work_id


def note_todo_signal(
    task: dict,
    project_path: str,
    todos: list[dict],
    *,
    accepted: bool,
) -> str:
    if not accepted:
        return ''
    return ensure_work_item(
        task, project_path, trigger=WORK_TRIGGER_TODO, todos=todos)


def note_isolated_workspace_signal(task: dict, project_path: str) -> str:
    """Create work at physical start when its deterministic workspace exists."""
    if not project_path or task.get('_transientRuntime'):
        return ''
    task_id = str(task.get('id') or '').strip()
    if not task_id:
        return ''
    from lib.integration_control import has_active_workspace_for_work

    work_id = deterministic_work_id(task_id)
    if not has_active_workspace_for_work(
            project_path, work_id, user_id=_owner(task)):
        return ''
    return ensure_work_item(
        task, project_path, trigger=WORK_TRIGGER_ISOLATED)


def _normalize_changed_path(project_path: str, path: str) -> str:
    raw = str(path or '').strip()
    if not raw:
        return ''
    project = Path(normalize_project_path(project_path))
    candidate = Path(os.path.expanduser(raw))
    if candidate.is_absolute():
        try:
            raw = str(candidate.resolve().relative_to(project.resolve()))
        except (OSError, ValueError):
            raw = str(candidate.resolve())
    return raw.replace('\\', '/').strip('/')[:4096]


def changed_paths_from_tool(
    fn_name: str,
    fn_args: dict,
    meta: dict | None,
    project_path: str,
) -> list[str]:
    paths: list[str] = []
    if isinstance(meta, dict):
        for item in meta.get('fileChanges') or ():
            if isinstance(item, dict):
                paths.append(str(item.get('path') or ''))
    if fn_name in _WRITE_TOOLS:
        if fn_args.get('path'):
            paths.append(str(fn_args.get('path') or ''))
        for item in fn_args.get('edits') or ():
            if isinstance(item, dict) and item.get('path'):
                paths.append(str(item.get('path') or ''))
    normalized = [_normalize_changed_path(project_path, path) for path in paths]
    return list(dict.fromkeys(path for path in normalized if path))[:200]


def _tool_succeeded(tool_content: Any) -> bool:
    if isinstance(tool_content, dict):
        return bool(tool_content.get('ok', True)) and not bool(
            tool_content.get('error'))
    text = str(tool_content or '').lstrip().lower()
    return not text.startswith((
        'error:', 'write failed', 'edit failed', 'apply failed',
        'insert failed', 'failed:', '❌',
    ))


def note_file_signal(
    task: dict,
    project_path: str,
    *,
    fn_name: str,
    fn_args: dict,
    tool_content: Any,
    meta: dict | None = None,
) -> str:
    paths = changed_paths_from_tool(fn_name, fn_args, meta, project_path)
    if not paths or not _tool_succeeded(tool_content):
        return ''
    work_id = ensure_work_item(
        task, project_path, trigger=WORK_TRIGGER_FILE, first_path=paths[0])
    if not work_id:
        return ''
    artifacts = []
    if isinstance(meta, dict) and meta.get('artifactId'):
        artifacts.append({
            'id': str(meta.get('artifactId') or ''),
            'title': str(meta.get('artifactTitle') or ''),
            'format': str(meta.get('artifactFormat') or ''),
            'path': paths[0],
        })
    digest = hashlib.sha256(
        '\0'.join([work_id, *paths, repr(artifacts)]).encode('utf-8')
    ).hexdigest()[:24]
    _command(
        'project_brain.work.change', project_path,
        user_id=_owner(task),
        command_id=f'project-work-change:{work_id}:{digest}',
        work_id=work_id, changed_paths=paths, artifacts=artifacts,
    )
    _send_overlap_advisories(task, project_path, paths)
    return work_id


def _paths_overlap(left: str, right: str) -> str:
    left_parts = tuple(part for part in left.strip('/').split('/') if part)
    right_parts = tuple(part for part in right.strip('/').split('/') if part)
    if not left_parts or not right_parts:
        return ''
    shorter = min(len(left_parts), len(right_parts))
    if left_parts[:shorter] != right_parts[:shorter]:
        return ''
    return '/'.join(left_parts[:shorter])


def _queue_transient_advisory(task: dict, key: str, text: str) -> bool:
    keys = task.setdefault('_projectOverlapKeys', [])
    if key in keys or len(keys) >= _TRANSIENT_ADVISORY_LIMIT:
        return False
    keys.append(key)
    task.setdefault('_projectOverlapAdvisories', []).append({
        'key': key, 'value': text[:1600],
    })
    return True


def _send_overlap_advisories(
    task: dict,
    project_path: str,
    changed_paths: list[str],
) -> None:
    try:
        from lib.tasks_pkg.manager import task_user_id
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        owner_user_id = _owner(task)
        project_key = normalize_project_path(project_path)
        this_task_id = str(task.get('id') or '')
        peers = chat_task_runtime.snapshot()
        with _overlap_lock:
            for peer in peers:
                if peer is task or peer.get('status') not in {'pending', 'running'}:
                    continue
                if task_user_id(peer) != owner_user_id:
                    continue
                if normalize_project_path(
                        (peer.get('config') or {}).get('projectPath') or '') != project_key:
                    continue
                peer_paths = list(peer.get('_projectChangedPaths') or ())
                for left in changed_paths:
                    for right in peer_paths:
                        overlap = _paths_overlap(left, right)
                        if not overlap:
                            continue
                        pair = ':'.join(sorted((this_task_id, str(peer.get('id') or ''))))
                        key = hashlib.sha256(
                            f'{pair}\0{overlap}'.encode('utf-8')).hexdigest()[:24]
                        text = (
                            '[Project overlap advisory] Another active conversation '
                            f'is editing an overlapping path: {overlap}. Re-read the '
                            'current file before the next write and coordinate within '
                            'this execution if needed.'
                        )
                        sent_left = _queue_transient_advisory(task, key, text)
                        sent_right = _queue_transient_advisory(peer, key, text)
                        if sent_left or sent_right:
                            _push_project_hint(project_key, owner_user_id, {
                                'type': 'path_overlap', 'path': overlap,
                                'taskIds': [this_task_id, str(peer.get('id') or '')],
                            })
        task['_projectChangedPaths'] = list(dict.fromkeys(
            [*(task.get('_projectChangedPaths') or ()), *changed_paths]))[-200:]
    except Exception as exc:
        logger.debug('[ProjectBrain] overlap detection failed soft: %s', exc)


def settle_work_item(
    task: dict,
    project_path: str,
    *,
    result_summary: str = '',
) -> str:
    work_id = str(task.get('_projectWorkId') or '')
    if not work_id:
        return ''
    if task.get('aborted'):
        status = 'cancelled'
    elif task.get('error'):
        status = 'failed'
    else:
        status = 'completed'
    summary = _first_line(
        result_summary or task.get('error') or task.get('content'), 4000)
    _command(
        'project_brain.work.finish', project_path,
        user_id=_owner(task), command_id=f'project-work-finish:{work_id}',
        work_id=work_id, status=status, result_summary=summary,
    )
    # End-of-task cleanup is deliberately in-memory only.  An advisory that
    # missed the next model round is discarded, never converted to history.
    task.pop('_projectOverlapAdvisories', None)
    task.pop('_projectOverlapKeys', None)
    changed_paths = list(task.pop('_projectChangedPaths', []) or ())
    try:
        run_matching_checkers(
            project_path, changed_paths, user_id=_owner(task), work_id=work_id)
    except Exception as exc:
        logger.debug('[ProjectBrain] automatic checker run failed soft: %s', exc)
    return status


def prepare_project_context(
    project_path: str,
    conversation_id: str,
    *,
    user_id: int,
    task: dict | None = None,
) -> str:
    project_key = normalize_project_path(project_path)
    if not (project_key and conversation_id):
        return ''
    page = get_storage_client(write=True).command(
        'project_brain.cursor.prepare', {
            'owner_user_id': int(user_id), 'project_key': project_key,
            'conversation_id': conversation_id, 'limit': 12,
            'token_budget': 900,
        }, None,
    )
    projection = read_projection(project_key, user_id=user_id)
    active = [item for item in projection.get('workItems') or ()
              if item.get('status') == 'active']
    decisions = list((projection.get('charter') or {}).get('decisions') or ())
    watch = [item for item in projection.get('watch') or ()
             if item.get('status', 'active') == 'active']
    entries = list(page.get('entries') or ())
    # A Project Context suffix exists only to deliver an unseen narrative
    # page. New/switching conversations initialize at head and receive no
    # state snapshot; steady-state rounds therefore add zero prompt tokens.
    if not entries:
        return ''
    lines: list[str] = ['New project narrative:']
    lines.extend(
        f"- #{item.get('sequence')} [{item.get('kind')}] "
        f"{item.get('text')}" for item in entries)
    if decisions:
        lines.append('Executable Charter:')
        for item in decisions[-32:]:
            ref = item.get('checkerRef') or {}
            lines.append(
                f"- {str(item.get('text') or '')[:300]} "
                f"[checker {ref.get('id')}@{ref.get('version')}]")
        if len(decisions) > 32:
            lines.append(f'- … {len(decisions) - 32} older decisions omitted')
    if watch:
        lines.append('Watch:')
        lines.extend(
            f"- {str(item.get('text') or '')[:300]}" for item in watch[-20:])
        if len(watch) > 20:
            lines.append(f'- … {len(watch) - 20} older Watch items omitted')
    if active:
        lines.append('Active work:')
        lines.extend(
            f"- {str(item.get('title') or '')[:220]} "
            f"({item.get('conversationId')}, paths: "
            f"{', '.join((item.get('changedPaths') or ())[-10:]) or 'none yet'})"
            for item in active[-20:]
        )
        if len(active) > 20:
            lines.append(f'- … {len(active) - 20} older active items omitted')
    if task is not None:
        task['_projectNarrativeDelivery'] = {
            'projectPath': project_key,
            'conversationId': conversation_id,
            'fromSequence': int(page.get('fromSequence') or 0),
            'toSequence': int(page.get('toSequence') or 0),
            'deliveryToken': str(page.get('deliveryToken') or ''),
            'userId': int(user_id),
        }
    return '[Project Context]\n' + '\n'.join(lines)


def confirm_project_context_delivery(task: dict) -> bool:
    pending = task.pop('_projectNarrativeDelivery', None)
    if not isinstance(pending, dict) or not pending.get('deliveryToken'):
        return False
    from lib.agent_core.store import get_conversation_store
    return get_conversation_store().confirm_project_context_delivery(pending)


def register_checker(
    project_path: str,
    definition: dict,
    *,
    user_id: int,
) -> dict:
    checker_id = str(definition.get('checkerId') or '').strip()
    version = int(definition.get('version') or 0)
    label = str(definition.get('label') or '').strip()
    argv = definition.get('argv')
    cwd = str(definition.get('cwd') or '.').strip()
    globs = definition.get('pathGlobs')
    timeout_ms = int(definition.get('timeoutMs') or 0)
    if not checker_id or version < 1 or not label:
        raise ValueError('checkerId, positive version, and label are required')
    if len(checker_id) > 128 or len(label) > 256:
        raise ValueError('checkerId and label exceed their size limits')
    if (not isinstance(argv, list) or not argv or len(argv) > 32
            or any(not isinstance(arg, str) or not arg or len(arg) > 4096
                   for arg in argv)):
        raise ValueError('argv must contain 1-32 strings of at most 4096 chars')
    if (not isinstance(globs, list) or not globs or len(globs) > 64
            or any(not isinstance(pattern, str) or not pattern
                   or len(pattern) > 4096 for pattern in globs)):
        raise ValueError(
            'pathGlobs must contain 1-64 strings of at most 4096 chars')
    if len(cwd) > 4096:
        raise ValueError('cwd must contain at most 4096 chars')
    if timeout_ms < 100 or timeout_ms > 3_600_000:
        raise ValueError('timeoutMs must be between 100 and 3600000')
    normalized = {
        'checkerId': checker_id, 'version': version,
        'label': label, 'argv': argv, 'cwd': cwd,
        'pathGlobs': globs, 'timeoutMs': timeout_ms,
        'enabled': bool(definition.get('enabled', True)),
    }
    digest = hashlib.sha256(repr(normalized).encode()).hexdigest()[:24]
    _command(
        'project_brain.checker.register', project_path,
        user_id=user_id,
        command_id=f'project-checker-register:{checker_id}:{version}:{digest}',
        definition=normalized,
    )
    return normalized


def _checker_definition(
    project_path: str,
    checker_id: str,
    version: int,
    *,
    user_id: int,
) -> dict:
    catalog = checker_catalog(project_path, user_id=user_id)['items']
    definition = next((item for item in catalog
                       if item.get('checkerId') == checker_id
                       and int(item.get('version') or 0) == int(version)), None)
    if definition is None:
        raise ValueError('unknown checker version')
    return definition


def _checker_cwd(project_path: str, cwd: str) -> Path:
    root = Path(project_path).resolve()
    target = (root / (cwd or '.')).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError('checker cwd must stay inside the project') from exc
    if not target.is_dir():
        raise ValueError('checker cwd does not exist')
    return target


def run_checker(
    project_path: str,
    checker_id: str,
    version: int,
    *,
    user_id: int,
    work_id: str = '',
    decision_id: str = '',
    reason: str = 'manual',
) -> dict:
    definition = _checker_definition(
        project_path, checker_id, version, user_id=user_id)
    started = time.monotonic()
    timed_out = False
    exit_code: int | None = None
    output = ''
    error = ''
    try:
        process = subprocess.Popen(
            list(definition['argv']),
            cwd=str(_checker_cwd(project_path, definition.get('cwd') or '.')),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == 'posix'),
        )
        retained = bytearray()

        def _drain_output() -> None:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    return
                if len(retained) < _MAX_CHECKER_OUTPUT_BYTES:
                    remaining = _MAX_CHECKER_OUTPUT_BYTES - len(retained)
                    retained.extend(chunk[:remaining])

        drain = threading.Thread(
            target=_drain_output, name='project-checker-output', daemon=True)
        drain.start()
        try:
            exit_code = int(process.wait(
                timeout=int(definition['timeoutMs']) / 1000))
        except subprocess.TimeoutExpired:
            timed_out = True
            error = 'checker timed out'
            if os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
        finally:
            drain.join(timeout=2.0)
            if drain.is_alive():
                process.kill()
                drain.join(timeout=1.0)
        output = retained.decode('utf-8', 'replace')[:_MAX_CHECKER_OUTPUT_CHARS]
    except (OSError, ValueError) as exc:
        error = str(exc)[:1000]
    ok = not timed_out and not error and exit_code == 0
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    result = {
        'checkerRef': {'id': checker_id, 'version': int(version)},
        'label': definition.get('label') or checker_id,
        'ok': ok, 'exitCode': exit_code, 'timedOut': timed_out,
        'durationMs': duration_ms, 'reason': reason,
        'summary': ('passed' if ok else (error or f'exit code {exit_code}')),
        'output': output,
        'workId': work_id,
        'timestamp': int(time.time() * 1000),
    }
    run_id = uuid.uuid4().hex
    _command(
        'project_brain.checker.result', project_path,
        user_id=user_id,
        command_id=f'project-checker-run:{run_id}',
        result=result, decision_id=decision_id,
    )
    return result


def run_matching_checkers(
    project_path: str,
    changed_paths: list[str],
    *,
    user_id: int,
    work_id: str = '',
) -> list[dict]:
    if not changed_paths:
        return []
    results = []
    for definition in _enabled_checker_definitions(
            project_path, user_id=user_id):
        patterns = definition.get('pathGlobs') or ()
        if not any(fnmatch(path, pattern)
                   for path in changed_paths for pattern in patterns):
            continue
        results.append(run_checker(
            project_path, str(definition['checkerId']), int(definition['version']),
            user_id=user_id, work_id=work_id, reason='changed_paths'))
    return results


def run_all_enabled_checkers(
    project_path: str,
    *,
    user_id: int,
    work_id: str = '',
    reason: str = 'integration',
) -> list[dict]:
    results = []
    for definition in _enabled_checker_definitions(
            project_path, user_id=user_id):
        results.append(run_checker(
            project_path, str(definition['checkerId']),
            int(definition['version']), user_id=user_id,
            work_id=work_id, reason=reason))
    return results


def _enabled_checker_definitions(
    project_path: str,
    *,
    user_id: int,
) -> list[dict]:
    """Return only the newest version of each logical checker when enabled."""
    latest: dict[str, dict] = {}
    for definition in checker_catalog(project_path, user_id=user_id)['items']:
        checker_id = str(definition.get('checkerId') or '')
        current = latest.get(checker_id)
        if current is None or int(definition.get('version') or 0) > int(
                current.get('version') or 0):
            latest[checker_id] = definition
    return [latest[key] for key in sorted(latest)
            if latest[key].get('enabled')]


def promote_decision(
    project_path: str,
    *,
    decision_id: str,
    text: str,
    checker_id: str,
    checker_version: int,
    source_conversation_id: str,
    source_turn_id: str,
    user_id: int,
) -> dict:
    decision_id = str(decision_id or '').strip() or (
        'pd_' + uuid.uuid4().hex[:20])
    text = str(text or '').strip()
    source_conversation_id = str(source_conversation_id or '').strip()
    source_turn_id = str(source_turn_id or '').strip()
    if not text:
        raise ValueError('decision text is required')
    if not source_conversation_id or not source_turn_id:
        raise ValueError('decision source conversation and turn are required')
    if (len(decision_id) > 128 or len(text) > 4000
            or len(source_conversation_id) > 256 or len(source_turn_id) > 256):
        raise ValueError('decision fields exceed their size limits')
    _checker_definition(
        project_path, checker_id, checker_version, user_id=user_id)
    decision = {
        'decisionId': decision_id,
        'text': text,
        'checkerRef': {'id': checker_id, 'version': int(checker_version)},
        'sourceConversationId': source_conversation_id,
        'sourceTurnId': source_turn_id,
        'latestVerification': None,
    }
    _command(
        'project_brain.decision.promote', project_path,
        user_id=user_id,
        command_id=f"project-decision-promote:{decision['decisionId']}",
        decision=decision,
    )
    return decision


def add_attention(
    project_path: str,
    *,
    kind: str,
    text: str,
    user_id: int,
    work_id: str = '',
) -> dict:
    attention_id = 'pa_' + uuid.uuid4().hex[:20]
    return _command(
        'project_brain.attention.add', project_path,
        user_id=user_id,
        command_id=f'project-attention:{attention_id}',
        attention_id=attention_id, kind=kind, text=text, work_id=work_id,
    )


def add_narrative(
    project_path: str,
    *,
    kind: str,
    text: str,
    user_id: int,
    work_id: str = '',
    conversation_id: str = '',
    command_id: str = '',
) -> dict:
    """Append one important, bounded narrative result to the projection."""
    normalized_text = _bounded_utf8(text, _MAX_NARRATIVE_BYTES)
    if not normalized_text:
        raise ValueError('narrative text is required')
    identity = command_id or hashlib.sha256(
        f'{kind}\0{work_id}\0{conversation_id}\0{normalized_text}'.encode(
            'utf-8', 'replace')).hexdigest()[:24]
    return _command(
        'project_brain.narrative.add', project_path,
        user_id=user_id, command_id=f'project-narrative:{identity}',
        kind=str(kind or 'note')[:64], text=normalized_text,
        work_id=str(work_id or '')[:128],
        conversation_id=str(conversation_id or '')[:256],
    )


def record_integration_failure(
    project_path: str,
    *,
    work_id: str,
    reason: str,
    user_id: int,
) -> None:
    text = f'Integration failed for {work_id}: {str(reason or "unknown failure")[:3000]}'
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]
    _command(
        'project_brain.narrative.add', project_path,
        user_id=user_id,
        command_id=f'project-integration-narrative:{work_id}:{digest}',
        kind='integration_failed', text=text, work_id=work_id,
    )
    _command(
        'project_brain.attention.add', project_path,
        user_id=user_id,
        command_id=f'project-integration-attention:{work_id}:{digest}',
        attention_id=f'integration:{work_id}:{digest}',
        kind='integration', text=text, work_id=work_id,
    )


def record_integration_success(
    project_path: str,
    *,
    work_id: str,
    user_id: int,
) -> None:
    text = f'Integration completed for {work_id}; candidate updated.'
    _command(
        'project_brain.narrative.add', project_path,
        user_id=user_id,
        command_id=f'project-integration-complete:{work_id}',
        kind='integration_completed', text=text, work_id=work_id,
    )


def add_watch_item(
    project_path: str,
    *,
    kind: str,
    text: str,
    user_id: int,
    source_conversation_id: str = '',
) -> dict:
    item_id = 'pw_' + uuid.uuid4().hex[:20]
    item = {
        'id': item_id, 'kind': str(kind or 'concern')[:64],
        'text': str(text or '').strip()[:4000], 'status': 'active',
        'sourceConversationId': str(source_conversation_id or '')[:256],
        'createdAt': int(time.time() * 1000), 'updatedAt': int(time.time() * 1000),
        'latestResult': None,
    }
    if not item['text']:
        raise ValueError('watch text is required')
    _command(
        'project_brain.watch.add', project_path, user_id=user_id,
        command_id=f'project-watch-add:{item_id}', item=item,
    )
    return item


def update_watch_item(
    project_path: str,
    item_id: str,
    *,
    user_id: int,
    text: str | None = None,
    status: str | None = None,
    latest_result: dict | None = None,
) -> dict:
    current = next((item for item in watch_projection(
        project_path, user_id=user_id)['items'] if item.get('id') == item_id), None)
    if current is None:
        raise ValueError('watch item not found')
    item = dict(current)
    if text is not None:
        item['text'] = str(text).strip()[:4000]
    if status is not None:
        if status not in {'active', 'resolved'}:
            raise ValueError('watch status must be active or resolved')
        item['status'] = status
    if latest_result is not None:
        item['latestResult'] = dict(latest_result)
    item['updatedAt'] = int(time.time() * 1000)
    _command(
        'project_brain.watch.update', project_path, user_id=user_id,
        command_id=f'project-watch-update:{item_id}:{item["updatedAt"]}', item=item,
    )
    return item


def delete_watch_item(project_path: str, item_id: str, *, user_id: int) -> None:
    _command(
        'project_brain.watch.delete', project_path, user_id=user_id,
        command_id=f'project-watch-delete:{item_id}', item_id=item_id,
    )


__all__ = [
    'WORK_TERMINAL_STATUSES', 'add_attention', 'add_narrative', 'add_watch_item',
    'attention_projection', 'board_projection', 'charter_projection',
    'checker_catalog', 'confirm_project_context_delivery',
    'delete_watch_item', 'deterministic_work_id', 'ensure_work_item',
    'feed_projection', 'note_file_signal', 'note_isolated_workspace_signal',
    'note_todo_signal', 'prepare_project_context', 'promote_decision',
    'read_projection',
    'record_integration_failure', 'record_integration_success',
    'register_checker',
    'run_all_enabled_checkers', 'run_checker',
    'run_matching_checkers', 'settle_work_item', 'status_projection',
    'update_watch_item', 'watch_projection',
]
