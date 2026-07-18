"""lib.tools.todo — structured task-checklist tool (``todo_write``).

Backport of OMC's TodoWrite / Claude Code's TodoWriteTool (Rec 1 of
``docs/omc-claude-code-backport-analysis.md``).  The model maintains a
machine-readable checklist on the live ``task`` dict as ``task['_todos']`` —
NOT in the message list — so it:

  * survives Layer-2 force-compaction (compaction rewrites ``messages``; the
    todo state lives on ``task`` and is never touched);
  * gives the continuation enforcer (``lib.tasks_pkg.stream_handler``) a hard,
    structured signal to detect a premature stop with unfinished DECLARED work
    — the case the zero-deliverable guard (INACTION) and suspicious-completion
    (content-shape heuristics) both structurally miss;
  * lets the frontend render live progress.

State model: a flat list of ``{id, content, status}`` where ``status`` is one
of ``pending`` / ``in_progress`` / ``completed``.  Each ``todo_write`` call
REPLACES the whole list (the model always sends the full current state — this
mirrors Claude Code and keeps the tool idempotent / stateless server-side).

The pure merge/validation logic lives here (``apply_todo_write``) so it is
unit-testable without a task/LLM; the dispatch handler in
``lib/tasks_pkg/handlers/misc.py`` is a thin wrapper that persists onto
``task['_todos']``.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

VALID_STATUSES = ('pending', 'in_progress', 'completed')

TODO_WRITE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'todo_write',
        'description': (
            'Maintain a structured checklist of the concrete steps required to '
            'finish the CURRENT task. Call this whenever you (a) plan a task of '
            '2+ non-trivial steps, (b) start working an item (mark it '
            'in_progress), or (c) finish an item (mark it completed). Send the '
            'FULL current list every time — it REPLACES the previous list. '
            'Keep exactly one item in_progress at a time. The checklist is '
            'tracked by the system: if you try to end your turn with pending or '
            'in_progress items, you will be reminded to finish them. Do NOT use '
            'this for a single trivial step.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'todos': {
                    'type': 'array',
                    'description': 'The complete, current checklist.',
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
    """Return the items that are NOT completed (pending / in_progress)."""
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
        marker = ' ⏳' if status == 'in_progress' else ''
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
    pending = total - done - in_prog

    if total == 0:
        return todos, 'Checklist cleared (no items).'

    summary = (f'Checklist updated: {done}/{total} completed'
               f'{f", {in_prog} in progress" if in_prog else ""}'
               f'{f", {pending} pending" if pending else ""}.')
    body = render_todo_list(todos)
    return todos, f'{summary}\n{body}'
