"""Structured, revisioned task checklists for ``todo_write``.

Backport of OMC's TodoWrite / Claude Code's TodoWriteTool (Rec 1 of
``docs/modules/task_engine.md``).  The model maintains a
machine-readable checklist on the live ``task`` dict as ``task['_todos']`` —
NOT in the message list — so it:

  * survives Layer-2 force-compaction (compaction rewrites ``messages``; the
    todo state lives on ``task`` and is never touched);
  * gives the continuation enforcer (``lib.tasks_pkg.stream_handler``) a hard,
    structured signal to detect a premature stop with unfinished DECLARED work
    — the case the zero-deliverable guard (INACTION) and suspicious-completion
    (content-shape heuristics) both structurally miss;
  * lets the frontend render live progress.

The compatibility mirror remains ``task['_todos']``, but the authoritative
state is ``task['_todoState']``: a stack of revisioned checklists.  Ordinary
``sync`` calls revise the current checklist, ``push`` explicitly enters a
child checklist bound to one parent item, and ``replan`` is the only operation
allowed to remove unfinished work.  Completed children are popped
automatically and their parent item is completed atomically.

The pure merge/validation logic lives here (``apply_todo_write``) so it is
unit-testable without a task/LLM; the dispatch handler in
``lib/tasks_pkg/handlers/misc.py`` is a thin wrapper that persists onto
``task['_todos']``.
"""

from __future__ import annotations

import copy
import threading
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

VALID_STATUSES = ('pending', 'in_progress', 'blocked', 'completed')
VALID_OPERATIONS = ('sync', 'push', 'replan')
TODO_STATE_VERSION = 2

TODO_WRITE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'todo_write',
        'description': (
            'Maintain the CURRENT task checklist. The default operation is '
            '`sync`: send the FULL current list; it creates a new REVISION of '
            'the active checklist, not a new checklist. Combine transitions '
            '(for example complete A and start B) into one sync. A sync cannot '
            'delete unfinished items or reopen completed items. Use `replan` '
            'with a reason when unfinished work must be replaced. Use `push` '
            'with parent_todo_id ONLY when decomposing one active parent item '
            'into a child checklist. The runtime automatically returns to the '
            'parent and completes that parent item when the child is complete; '
            'do not recreate the parent list yourself. Keep at most one item '
            'in_progress per checklist. A task with pending, in_progress, or '
            'blocked items cannot finish successfully. Do not use this for a '
            'single trivial step.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'operation': {
                    'type': 'string',
                    'enum': list(VALID_OPERATIONS),
                    'description': ('sync (default) revises the active list; '
                                    'push enters a child list; replan replaces '
                                    'unfinished work and requires reason.'),
                },
                'parent_todo_id': {
                    'type': 'string',
                    'description': 'Required for push; an unfinished item in the active parent list.',
                },
                'reason': {
                    'type': 'string',
                    'description': 'Required for replan; explains why unfinished work is being replaced.',
                },
                'todos': {
                    'type': 'array',
                    'description': 'The complete desired checklist for this operation.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'id': {
                                'type': 'string',
                                'description': 'Stable short id for the item '
                                               '(e.g. "1", "read-config").',
                            },
                            'content': {
                                'type': 'string',
                                'description': 'Imperative description of the '
                                               'step (e.g. "Add retry to '
                                               'fetch_page").',
                            },
                            'status': {
                                'type': 'string',
                                'enum': list(VALID_STATUSES),
                            },
                        },
                        'required': ['id', 'content', 'status'],
                    },
                },
            },
            'required': ['todos'],
        },
    },
}


def _normalize_todos(raw) -> list[dict]:
    """Validate + normalize a raw ``todos`` payload into clean item dicts.

    Drops malformed entries (non-dict, missing content), coerces an unknown
    status to ``pending``, and synthesizes a stable id when absent.  Never
    raises — a bad payload yields the best-effort cleaned list (possibly
    empty), because a tool call must return a result, not crash the turn.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        content = item.get('content')
        if not isinstance(content, str) or not content.strip():
            continue
        status = item.get('status')
        if status not in VALID_STATUSES:
            status = 'pending'
        tid = item.get('id')
        if not isinstance(tid, str) or not tid.strip():
            tid = str(i + 1)
        out.append({'id': tid.strip(), 'content': content.strip(),
                    'status': status})

    # Enforce the tool contract: at most ONE item may be in_progress at a
    # time. If the model sends several, keep the FIRST in document order and
    # demote the rest to pending — the checklist is meant to reflect a single
    # active step, and multiple in_progress defeats the continuation-enforcer
    # signal that keys off it.
    seen_in_progress = False
    demoted = 0
    for t in out:
        if t['status'] != 'in_progress':
            continue
        if seen_in_progress:
            t['status'] = 'pending'
            demoted += 1
        else:
            seen_in_progress = True
    if demoted:
        logger.warning('todo_write: %d extra in_progress item(s) demoted to '
                       'pending — exactly one may be in_progress at a time',
                       demoted)
    return out


def incomplete_todos(todos) -> list[dict]:
    """Return items that are not completed (including blocked items)."""
    if not isinstance(todos, list):
        return []
    return [t for t in todos
            if isinstance(t, dict) and t.get('status') != 'completed']


def render_todo_list(todos) -> str:
    """Render a checklist as GitHub-style markdown checkboxes for a reminder."""
    lines = []
    for t in (todos or []):
        if not isinstance(t, dict):
            continue
        status = t.get('status')
        box = '[x]' if status == 'completed' else '[ ]'
        marker = (' ⏳' if status == 'in_progress' else
                  (' [blocked]' if status == 'blocked' else ''))
        lines.append(f'- {box} {t.get("content", "")}{marker}')
    return '\n'.join(lines)


def apply_todo_write(fn_args: dict) -> tuple[list[dict], str]:
    """Pure core of the ``todo_write`` tool.

    Takes the raw tool arguments, returns ``(normalized_todos, result_text)``.
    ``result_text`` is the tool result string the model sees — a compact
    progress summary so it always knows the current state without re-sending.
    """
    todos = _normalize_todos((fn_args or {}).get('todos'))
    total = len(todos)
    done = sum(1 for t in todos if t.get('status') == 'completed')
    in_prog = sum(1 for t in todos if t.get('status') == 'in_progress')
    blocked = sum(1 for t in todos if t.get('status') == 'blocked')
    pending = total - done - in_prog - blocked

    if total == 0:
        return todos, 'Checklist cleared (no items).'

    summary = (f'Checklist updated: {done}/{total} completed'
               f'{f", {in_prog} in progress" if in_prog else ""}'
               f'{f", {pending} pending" if pending else ""}'
               f'{f", {blocked} blocked" if blocked else ""}.')
    body = render_todo_list(todos)
    return todos, f'{summary}\n{body}'


def _new_checklist_id() -> str:
    return 'cl_' + uuid.uuid4().hex[:12]


def _empty_state() -> dict:
    return {
        'version': TODO_STATE_VERSION,
        'stack': [],
        'history': [],
        'update_count': 0,
        'root_completed': False,
    }


def _frame(todos: list[dict], *, parent_todo_id: str | None = None,
           checklist_id: str | None = None, revision: int = 1) -> dict:
    return {
        'checklist_id': checklist_id or _new_checklist_id(),
        'revision': max(1, int(revision or 1)),
        'parent_todo_id': parent_todo_id,
        'todos': copy.deepcopy(todos),
    }


def _normalise_state(raw, legacy_todos=None) -> dict:
    """Return a safe v2 state, migrating the old flat-list mirror if needed."""
    if not isinstance(raw, dict) or not isinstance(raw.get('stack'), list):
        state = _empty_state()
        todos = _normalize_todos(legacy_todos)
        if todos:
            state['stack'] = [_frame(todos)]
            state['root_completed'] = not incomplete_todos(todos)
        return state

    state = _empty_state()
    state['update_count'] = max(0, int(raw.get('update_count') or 0))
    state['root_completed'] = bool(raw.get('root_completed'))
    history = raw.get('history')
    if isinstance(history, list):
        state['history'] = copy.deepcopy([h for h in history if isinstance(h, dict)])
    for i, item in enumerate(raw.get('stack') or []):
        if not isinstance(item, dict):
            continue
        todos = _normalize_todos(item.get('todos'))
        cid = item.get('checklist_id')
        if not isinstance(cid, str) or not cid.strip():
            cid = _new_checklist_id()
        parent_id = item.get('parent_todo_id') if i else None
        state['stack'].append(_frame(
            todos,
            parent_todo_id=str(parent_id).strip() if parent_id else None,
            checklist_id=cid.strip(),
            revision=item.get('revision') or 1,
        ))
    if state['stack']:
        state['root_completed'] = (
            len(state['stack']) == 1
            and not incomplete_todos(state['stack'][0]['todos'])
        )
    return state


def public_todo_state(raw) -> dict:
    """Return a JSON-safe defensive copy of a todo state."""
    return copy.deepcopy(_normalise_state(raw))


def todo_state_from_task(task: dict | None) -> dict:
    task = task or {}
    return _normalise_state(task.get('_todoState'), task.get('_todos'))


def _active_frame(state: dict) -> dict | None:
    stack = state.get('stack') or []
    return stack[-1] if stack else None


def _duplicate_ids(todos: list[dict]) -> list[str]:
    seen = set()
    duplicates = []
    for item in todos:
        tid = item.get('id')
        if tid in seen and tid not in duplicates:
            duplicates.append(tid)
        seen.add(tid)
    return duplicates


def _rejection(state: dict, operation: str, reason: str) -> dict:
    active = _active_frame(state)
    return {
        'state': state,
        'todos': copy.deepcopy(active.get('todos') if active else []),
        'operation': operation,
        'rejected': True,
        'no_op': False,
        'reason': reason,
        'auto_popped': [],
    }


def _cascade_completed(state: dict) -> list[str]:
    """Pop completed children and atomically complete their bound parent."""
    popped = []
    stack = state['stack']
    while len(stack) > 1 and not incomplete_todos(stack[-1]['todos']):
        child = stack.pop()
        state['history'].append({
            'kind': 'child_completed',
            'checklist_id': child['checklist_id'],
            'revision': child['revision'],
            'parent_todo_id': child.get('parent_todo_id'),
            'todos': copy.deepcopy(child['todos']),
        })
        popped.append(child['checklist_id'])
        parent = stack[-1]
        parent_id = child.get('parent_todo_id')
        for item in parent['todos']:
            if item.get('id') == parent_id:
                item['status'] = 'completed'
                break
        parent['revision'] += 1
    state['root_completed'] = bool(
        len(stack) == 1 and not incomplete_todos(stack[0]['todos'])
    )
    return popped


def apply_todo_operation(current_state: dict | None, fn_args: dict | None) -> dict:
    """Pure state transition for the revisioned checklist protocol.

    The returned dict always carries the resulting state and the active list.
    Invalid transitions are receipts, not exceptions: ``rejected`` is true and
    the prior state is returned unchanged so the model can repair its call.
    """
    state = _normalise_state(current_state)
    args = fn_args if isinstance(fn_args, dict) else {}
    operation = args.get('operation') or 'sync'
    if operation not in VALID_OPERATIONS:
        return _rejection(state, str(operation), 'operation must be sync, push, or replan')

    todos = _normalize_todos(args.get('todos'))
    dupes = _duplicate_ids(todos)
    if dupes:
        return _rejection(state, operation,
                          'todo ids must be unique: ' + ', '.join(dupes))

    active = _active_frame(state)
    if operation == 'push':
        if active is None:
            return _rejection(state, operation,
                              'push requires an active parent checklist; create it with sync first')
        if state.get('root_completed'):
            return _rejection(state, operation, 'the root checklist is already completed')
        parent_id = args.get('parent_todo_id')
        parent_id = parent_id.strip() if isinstance(parent_id, str) else ''
        parent_item = next((t for t in active['todos'] if t.get('id') == parent_id), None)
        if not parent_item or parent_item.get('status') == 'completed':
            return _rejection(state, operation,
                              'parent_todo_id must name an unfinished item in the active checklist')
        if not todos:
            return _rejection(state, operation, 'a child checklist cannot be empty')
        for item in active['todos']:
            if item.get('status') == 'in_progress':
                item['status'] = 'pending'
        parent_item['status'] = 'in_progress'
        active['revision'] += 1
        state['stack'].append(_frame(todos, parent_todo_id=parent_id))
        state['update_count'] += 1
    elif active is None:
        if operation == 'replan':
            return _rejection(state, operation,
                              'replan requires an active checklist; create it with sync first')
        if todos:
            state['stack'].append(_frame(todos))
            state['update_count'] += 1
        else:
            state['root_completed'] = True
            return {
                'state': state, 'todos': [], 'operation': operation,
                'rejected': False, 'no_op': True, 'reason': '',
                'auto_popped': [],
            }
    elif operation == 'sync':
        # Exact repeats are idempotent even after the root completed. Models
        # sometimes emit the final snapshot twice in one batch; rejecting the
        # second copy would turn a harmless duplicate into a repair loop.
        if todos == active['todos']:
            return {
                'state': state, 'todos': copy.deepcopy(todos),
                'operation': operation, 'rejected': False, 'no_op': True,
                'reason': '', 'auto_popped': [],
            }
        if state.get('root_completed'):
            return _rejection(state, operation,
                              'the root checklist is already completed; use replan with a reason to replace it')
        old_by_id = {t['id']: t for t in active['todos']}
        new_by_id = {t['id']: t for t in todos}
        missing = [t['id'] for t in active['todos']
                   if t.get('status') != 'completed' and t['id'] not in new_by_id]
        reopened = [tid for tid, old in old_by_id.items()
                    if old.get('status') == 'completed'
                    and tid in new_by_id
                    and new_by_id[tid].get('status') != 'completed']
        if missing:
            return _rejection(
                state, operation,
                'sync cannot remove unfinished items (' + ', '.join(missing)
                + '); use replan with a reason')
        if reopened:
            return _rejection(state, operation,
                              'sync cannot reopen completed items: ' + ', '.join(reopened))
        active['todos'] = copy.deepcopy(todos)
        active['revision'] += 1
        state['update_count'] += 1
    else:  # replan
        reason = args.get('reason')
        reason = reason.strip() if isinstance(reason, str) else ''
        if not reason:
            return _rejection(state, operation, 'replan requires a non-empty reason')
        old = copy.deepcopy(active['todos'])
        new_ids = {t['id'] for t in todos}
        superseded = [copy.deepcopy(t) for t in old
                      if t.get('status') != 'completed' and t['id'] not in new_ids]
        state['history'].append({
            'kind': 'replan',
            'checklist_id': active['checklist_id'],
            'revision': active['revision'],
            'reason': reason,
            'todos': old,
            'superseded': superseded,
        })
        active['todos'] = copy.deepcopy(todos)
        active['revision'] += 1
        state['update_count'] += 1

    popped = _cascade_completed(state)
    active = _active_frame(state)
    return {
        'state': state,
        'todos': copy.deepcopy(active.get('todos') if active else []),
        'operation': operation,
        'rejected': False,
        'no_op': False,
        'reason': '',
        'auto_popped': popped,
    }


def _result_text(outcome: dict) -> str:
    todos = outcome.get('todos') or []
    if outcome.get('rejected'):
        prefix = 'Checklist update rejected: ' + outcome.get('reason', 'invalid transition') + '.'
    elif outcome.get('no_op'):
        prefix = 'Checklist unchanged (duplicate update; no new revision).'
    else:
        total = len(todos)
        done = sum(1 for t in todos if t.get('status') == 'completed')
        blocked = sum(1 for t in todos if t.get('status') == 'blocked')
        active = _active_frame(outcome['state'])
        prefix = f'Checklist updated: {done}/{total} completed'
        if blocked:
            prefix += f', {blocked} blocked'
        if active:
            prefix += (f' (revision {active["revision"]}, '
                       f'depth {len(outcome["state"]["stack"])}).')
        else:
            prefix += '.'
        if outcome.get('auto_popped'):
            prefix += (f' Completed {len(outcome["auto_popped"])} child checklist(s) '
                       'and restored the parent automatically.')
        if outcome['state'].get('root_completed'):
            prefix += ' Root checklist complete.'
    body = render_todo_list(todos)
    return prefix + (f'\n{body}' if body else '')


def apply_todo_write_to_task(task: dict, fn_args: dict | None) -> tuple[list[dict], str, dict]:
    """Atomically apply one tool call and update the compatibility mirror."""
    lock = task.get('_todo_lock')
    if lock is None:
        lock = task.setdefault('_todo_lock', threading.RLock())
    with lock:
        current = todo_state_from_task(task)
        outcome = apply_todo_operation(current, fn_args)
        task['_todoState'] = outcome['state']
        task['_todos'] = copy.deepcopy(outcome['todos'])
        if not outcome.get('rejected') and not outcome.get('no_op'):
            task.pop('_todo_blocked', None)
    return outcome['todos'], _result_text(outcome), outcome


def compact_todo_rounds_for_replay(rounds: list[dict] | None) -> list[dict]:
    """Keep one effective ``todo_write`` carrier in model-visible replay.

    The original list is never mutated and remains the durable audit history.
    Prefer the newest accepted state-changing revision; if a turn contains
    only rejected/no-op todo calls, retain the newest receipt so the protocol
    still has a valid assistant/tool pair.
    """
    source = list(rounds or [])
    todo_indexes = [i for i, row in enumerate(source)
                    if isinstance(row, dict) and row.get('toolName') == 'todo_write']
    if len(todo_indexes) <= 1:
        return source
    effective = []
    for idx in todo_indexes:
        results = source[idx].get('results') or []
        meta = results[0] if results and isinstance(results[0], dict) else {}
        if not meta.get('todoRejected') and not meta.get('todoNoop'):
            effective.append(idx)
    keep = effective[-1] if effective else todo_indexes[-1]
    todo_set = set(todo_indexes)
    return [row for idx, row in enumerate(source)
            if idx not in todo_set or idx == keep]
