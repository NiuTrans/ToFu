"""Codex-style full progress snapshots for ``todo_write``.

The schema and pure replacement/validation live here; the task handler persists
one accepted snapshot for UI and compaction. Progress is not completion authority.
Historical v2 stacks are read-only migration inputs, never a live state machine.
"""

from __future__ import annotations

import copy
import json as json
import threading

VALID_STATUSES = ('pending', 'in_progress', 'blocked', 'completed')
TODO_STATE_VERSION = 3
TODO_MAX_ITEMS = 24
TODO_MAX_CONTENT_CHARS = 512
TODO_MAX_EXPLANATION_CHARS = 2048
# A migrated v2 checkpoint can contain six full frames. Do not truncate its
# ancestors, or lose their rows on the next save/restore cycle.
TODO_MAX_RESTORED_ITEMS = 144

TODO_WRITE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'todo_write',
        'description': (
            'Update the plan for nontrivial work. Send the FULL current todo list '
            'each time; the same call creates, updates, or replaces the plan. '
            'An empty list clears it. No prior plan is required. '
            'At most one item may be in_progress. Optionally explain a plan change. '
            'Todo status records progress, not proof of task success.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'explanation': {
                    'type': 'string',
                    'maxLength': TODO_MAX_EXPLANATION_CHARS,
                    'description': 'Optional explanation of this update.',
                },
                'todos': {
                    'type': 'array',
                    'maxItems': TODO_MAX_ITEMS,
                    'description': 'Full current plan, not a patch.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'content': {
                                'type': 'string', 'minLength': 1,
                                'maxLength': TODO_MAX_CONTENT_CHARS,
                                'description': 'A concise actionable step.',
                            },
                            'status': {'type': 'string', 'enum': list(VALID_STATUSES)},
                        },
                        'required': ['content', 'status'],
                    },
                },
            },
            'required': ['todos'],
        },
    },
}


def _normalize_todos(raw, *, restoring: bool = False) -> list[dict]:
    """Validate before copying. Malformed lists never mean an empty plan.

    Old IDs are intentionally discarded. Migration preserves all statuses,
    including active ancestor rows. The single in_progress convention lives
    only in the tool description, matching codex/opencode: no runtime
    rejection, so a model slip never discards the accepted plan.
    """
    limit = TODO_MAX_RESTORED_ITEMS if restoring else TODO_MAX_ITEMS
    if not isinstance(raw, list):
        raise ValueError('todos must be an array')
    if len(raw) > limit:
        raise ValueError(f'todos may contain at most {limit} items')
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('each todo must be an object with content and status')
        content, status = item.get('content'), item.get('status')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('todo content must be a non-empty string')
        if len(content) > TODO_MAX_CONTENT_CHARS:
            raise ValueError(f'todo content exceeds {TODO_MAX_CONTENT_CHARS} characters')
        if not isinstance(status, str) or status not in VALID_STATUSES:
            raise ValueError('todo status must be pending, in_progress, blocked, or completed')
        rows.append({'content': content if restoring else content.strip(), 'status': status})
    return rows


def incomplete_todos(todos) -> list[dict]:
    """Return unfinished progress rows, without imposing continuation policy."""
    return [t for t in todos if isinstance(t, dict) and t.get('status') != 'completed'] \
        if isinstance(todos, list) else []


def render_todo_list(todos) -> str:
    """Render flat progress; no hidden identities or lifecycle instructions."""
    lines = []
    for item in todos or []:
        status = item.get('status')
        box = '[x]' if status == 'completed' else '[ ]'
        marker = ' ⏳' if status == 'in_progress' else (' [blocked]' if status == 'blocked' else '')
        lines.append(f'- {box} {item.get("content", "")}{marker}')
    return '\n'.join(lines)


def _empty_state() -> dict:
    return {'version': TODO_STATE_VERSION, 'todos': [], 'update_count': 0, 'explanation': ''}


def _normalise_state(raw, legacy_todos=None) -> dict:
    """Detach accepted state; migrate old stacks without losing ancestor work.

    Untrusted durable checkpoints undergo strict preflight in _resume_state
    before entering this projection. Reject malformed values rather than
    truthiness-repairing them into a cleared checklist.
    """
    state = _empty_state()
    if raw is None:
        if legacy_todos is not None:
            state['todos'] = _normalize_todos(legacy_todos, restoring=True)
        return state
    if not isinstance(raw, dict):
        raise ValueError('todo state must be an object')
    version = raw.get('version')
    if type(version) is not int or version not in (2, TODO_STATE_VERSION):
        raise ValueError('todo state version must be 2 or 3')
    count = raw.get('update_count', 0)
    if type(count) is not int or count < 0:
        raise ValueError('todo update_count must be a non-negative integer')
    state['update_count'] = count
    if version == TODO_STATE_VERSION:
        explanation = raw.get('explanation', '')
        if not isinstance(explanation, str) or len(explanation) > TODO_MAX_EXPLANATION_CHARS:
            raise ValueError('todo explanation must be text of at most 2048 characters')
        state['explanation'] = explanation
        state['todos'] = _normalize_todos(raw.get('todos'), restoring=True)
        return state
    stack = raw.get('stack')
    if not isinstance(stack, list) or len(stack) > 6:
        raise ValueError('legacy todo stack must be a list of at most six frames')
    rows = []
    for frame in stack:
        if not isinstance(frame, dict) or not isinstance(frame.get('todos'), list):
            raise ValueError('legacy todo frame must carry a todo list')
        if len(frame['todos']) > TODO_MAX_ITEMS:
            raise ValueError('legacy todo frame exceeds 24 items')
        rows.extend(frame['todos'])
    state['todos'] = _normalize_todos(rows, restoring=True)
    return state


def public_todo_state(raw) -> dict:
    """Return the detached flat checkpoint, never a revision/history stack."""
    return _normalise_state(raw)


def todo_state_from_task(task: dict | None) -> dict:
    task = task or {}
    return _normalise_state(task.get('_todoState'), task.get('_todos'))


def _outcome(state: dict, *, reason: str = '', no_op: bool = False) -> dict:
    return {'state': state, 'todos': copy.deepcopy(state['todos']),
            'rejected': bool(reason), 'no_op': no_op, 'reason': reason}


TODO_LIST_COMMANDS = ('create', 'complete', 'load', 'add', 'remove', 'lookup')


def _todo_list_command_name(args: dict) -> str:
    command = args.get('command')
    command = command.strip().lower() if isinstance(command, str) else ''
    return command if command in TODO_LIST_COMMANDS else ''


def _todo_list_task_texts(raw) -> list[str]:
    """Read Amazon Q's text/dict task vocabulary (not native todo validation)."""
    texts = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = next((item[k] for k in ('content', 'description', 'title', 'task', 'text')
                         if isinstance(item.get(k), str)), '')
        else:
            continue
        if text.strip():
            texts.append(text.strip())
    return texts


def _todo_list_indices(raw) -> list[int]:
    indices = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            index = item
        elif isinstance(item, float) and item.is_integer():
            index = int(item)
        elif isinstance(item, str) and item.strip().lstrip('-').isdigit():
            index = int(item.strip())
        else:
            continue
        if index >= 0 and index not in indices:
            indices.append(index)
    return indices


def _translate_todo_list_command(state: dict, args: dict, command: str):
    """Compatibility adapter only: expand Amazon Q commands into a snapshot."""
    current = copy.deepcopy(state['todos'])
    if command in ('load', 'lookup'):
        return None, f'todo_list {command}: current checklist (no state change).'
    if command == 'create':
        texts = _todo_list_task_texts(args.get('tasks'))
        if not texts:
            raise ValueError('todo_list create requires a non-empty tasks array')
        explanation = args.get('todo_list_description', '')
        return {'todos': [{'content': text, 'status': 'pending'} for text in texts],
                'explanation': explanation}, ''
    if command == 'add':
        texts = _todo_list_task_texts(args.get('new_tasks'))
        if not texts:
            raise ValueError('todo_list add requires a non-empty new_tasks array')
        positions = _todo_list_indices(args.get('insert_indices'))
        for i, text in enumerate(texts):
            row = {'content': text, 'status': 'pending'}
            current.insert(min(positions[i], len(current)) if i < len(positions) else len(current), row)
    else:
        if not current:
            raise ValueError(f'todo_list {command} found no active checklist; create one first')
        if command == 'complete':
            for index in _todo_list_indices(args.get('completed_indices')):
                if index < len(current):
                    current[index]['status'] = 'completed'
        elif command == 'remove':
            drop = set(_todo_list_indices(args.get('remove_indices')))
            remaining = [t for i, t in enumerate(current) if i not in drop]
            if len(remaining) == len(current):
                raise ValueError('todo_list remove: no remove_indices matched the checklist')
            current = remaining
    return {'todos': current, 'explanation': state['explanation']}, ''


def apply_todo_operation(current_state: dict | None, fn_args: dict | None) -> dict:
    """Accept a full snapshot with no lifecycle preconditions; reject bad input.

    The historical entry point name remains for callers, not as a public verb.
    Stale sync/replan arguments are safe full snapshots. A stale push is NOT:
    its child-only payload cannot silently supersede the unsubmitted parent.
    """
    state = _normalise_state(current_state)
    if not isinstance(fn_args, dict):
        return _outcome(state, reason='arguments must be an object containing todos')
    args = fn_args
    try:
        operation = args.get('operation')
        if operation not in (None, '', 'sync', 'replan'):
            raise ValueError('nested or unknown operations are no longer supported; submit the full todo list')
        command = _todo_list_command_name(args)
        if command and ('todos' not in args or args.get('_todo_list_command')):
            translated, note = _translate_todo_list_command(state, args, command)
            if translated is None:
                result = _outcome(state, no_op=True)
                result['note'] = note
                return result
            args = translated
        if 'todos' not in args:
            raise ValueError('todos is required; submit the full current plan')
        todos = _normalize_todos(args['todos'])
        explanation = args.get('explanation', args.get('reason', ''))
        if not isinstance(explanation, str):
            raise ValueError('explanation must be a string')
        if len(explanation) > TODO_MAX_EXPLANATION_CHARS:
            raise ValueError(f'explanation exceeds {TODO_MAX_EXPLANATION_CHARS} characters')
        explanation = explanation.strip()
    except ValueError as exc:
        return _outcome(state, reason=str(exc))
    if (state['update_count'] > 0 and todos == state['todos']
            and explanation == state['explanation']):
        return _outcome(state, no_op=True)
    return _outcome({'version': TODO_STATE_VERSION, 'todos': todos,
                     'explanation': explanation, 'update_count': state['update_count'] + 1})


def render_todo_snapshot(state: dict, *, explanation_label: str = 'Explanation') -> str:
    """Render accepted fields separately; labels must never rewrite item text."""
    body = render_todo_list(state.get('todos')) or 'The accepted todo list is empty.'
    explanation = state.get('explanation') or ''
    return body + (f'\n{explanation_label}: {explanation}' if explanation else '')


def _result_text(outcome: dict) -> str:
    todos = outcome['todos']
    if outcome['rejected']:
        prefix = 'Checklist update rejected: ' + outcome['reason'] + '.'
    elif outcome['no_op']:
        prefix = outcome.get('note') or 'Checklist unchanged (identical snapshot).'
    else:
        done = sum(t['status'] == 'completed' for t in todos)
        prefix = f'Checklist updated: {done}/{len(todos)} completed.'
    return prefix + '\n' + render_todo_snapshot(outcome['state'])


def apply_todo_write(fn_args: dict) -> tuple[list[dict], str]:
    outcome = apply_todo_operation(None, fn_args)
    return outcome['todos'], _result_text(outcome)


def apply_todo_write_to_task(task: dict, fn_args: dict | None) -> tuple[list[dict], str, dict]:
    """Validate before atomically replacing accepted progress and its mirror."""
    lock = task.get('_todo_lock')
    if lock is None:
        lock = task.setdefault('_todo_lock', threading.RLock())
    with lock:
        outcome = apply_todo_operation(todo_state_from_task(task), fn_args)
        if not outcome['rejected']:
            task['_todoState'] = copy.deepcopy(outcome['state'])
            task['_todos'] = copy.deepcopy(outcome['todos'])
    return outcome['todos'], _result_text(outcome), outcome


def compact_todo_rounds_for_replay(rounds: list[dict] | None) -> list[dict]:
    """Retain latest accepted snapshot and latest receipt, not the entire audit.

    A failed/no-op last call must not evict accepted state; durable input rows
    remain unchanged. Historical metadata remains readable in this projection.
    """
    source = list(rounds or [])
    todo_indexes = [i for i, row in enumerate(source)
                    if isinstance(row, dict) and row.get('toolName') == 'todo_write']
    if len(todo_indexes) <= 1:
        return source
    effective = []
    for idx in todo_indexes:
        row = source[idx]
        status = str(row.get('status') or '')
        if status and status != 'done':
            continue
        results = row.get('results') or []
        meta = results[0] if results and isinstance(results[0], dict) else {}
        result_status = str(meta.get('status') or '').lower()
        if (meta.get('isError') or meta.get('is_error')
                or str(meta.get('type') or '').lower() == 'error'
                or result_status in {'error', 'failed', 'failure', 'rejected', 'aborted'}):
            continue
        if not meta.get('todoRejected') and not meta.get('todoNoop'):
            effective.append(idx)
    keep_indexes = {todo_indexes[-1]}
    if effective:
        keep_indexes.add(effective[-1])
    todo_set = set(todo_indexes)
    return [row for idx, row in enumerate(source)
            if idx not in todo_set or idx in keep_indexes]
