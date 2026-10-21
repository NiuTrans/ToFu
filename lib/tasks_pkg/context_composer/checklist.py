"""Accepted-checklist visibility and loss recovery, without changing todo state.

Authority is the runtime task; evidence is a paired todo result or a managed
context block. Opaque identities never belong in model-readable prose. A
partial, foreign, or superseded result cannot prove that current state survived.
"""
from __future__ import annotations

import json

from lib.tasks_pkg.context_composer._models import ComposeRequest, ContextBlock
from lib.tasks_pkg.context_composer.registry import context_block
from lib.tools.todo import render_todo_snapshot, todo_state_from_task

CHECKLIST_HEADING = '## Active Task Checklist'
_TODO_TOOL_NAMES = frozenset(('todo_write', 'todo_list'))
_GATEWAY_TOOL_NAMES = frozenset(('execute_tools', 'multi_tool_use.parallel'))


def checklist_context_block(content: str) -> ContextBlock:
    return context_block('active_checklist', content)


def _message_text(message: dict) -> str:
    content = message.get('content')
    if isinstance(content, str):
        return content
    return '\n'.join(
        block['text'] for block in content or []
        if isinstance(block, dict) and isinstance(block.get('text'), str)
    ) if isinstance(content, list) else ''


def _snapshot_body(state: dict) -> str:
    return '\n'.join((
        CHECKLIST_HEADING,
        'Accepted task progress preserved by the runtime. Restoration is not '
        'an update. Status reports progress, not proof of success. '
        'Use only currently available tools for any actual update.',
        render_todo_snapshot(state, explanation_label=(
            'Progress explanation from the accepted todo update')),
    ))


def _is_checklist_carrier(message: dict) -> bool:
    return (message.get('role') == 'user'
            and message.get('_contextComposer') is True
            and any(isinstance(block, dict) and block.get('id') == 'active_checklist'
                    for block in message.get('_contextBlocks') or []))


def _accepted_result(text: str, state: dict) -> bool:
    # Compare the complete snapshot, not a substring/hash that may survive
    # while explanation/list rows are clamped away. Prefix describes operation,
    # including rejected/no-op writes which still return the accepted state.
    if not isinstance(text, str):
        return False
    prefix, separator, body = text.partition('\n')
    accepted_prefix = prefix.startswith('Checklist ') or prefix in (
        'todo_list load: current checklist (no state change).',
        'todo_list lookup: current checklist (no state change).')
    return bool(separator and accepted_prefix and body == render_todo_snapshot(state))


def _gateway_result_visible(text: str, state: dict) -> bool | None:
    """Read only gateway receipt structure, never arbitrary nested tool data."""
    try:
        result = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(result, dict):
        return None
    # Direct calls expose receipts. JavaScript childResults carries output only
    # when the gateway witnessed the actual complete child receipt in its return.
    # Never inspect program.result: arbitrary program text is not provenance.
    program = result.get('program')
    rows = result.get('results') or (
        program.get('childResults') if isinstance(program, dict)
        and program.get('status') == 'ok' else None) or []
    if not isinstance(rows, list):
        return None
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if row.get('name') not in _TODO_TOOL_NAMES:
            continue
        if (row.get('status') == 'done' and not row.get('output_truncated')
                and not row.get('retentionError')
                and _accepted_result(row.get('output') or '', state)):
            return True
    return None


def _complete_carrier(message: dict, state: dict) -> bool:
    from lib.tasks_pkg.context_composer._render import _envelope
    block = checklist_context_block(_snapshot_body(state))
    # Exact complete envelopes, not a regex terminated by arbitrary todo text.
    # The amalgamated legacy form has a source line instead of its own wrapper.
    standalone = _envelope(block, block.content)
    merged = ('<!-- tofu-context:active_checklist:start -->\n'
              f'[source: {block.source}]\n{block.content}\n'
              '<!-- tofu-context:active_checklist:end -->')
    text = _message_text(message)
    return text.count(standalone) + text.count(merged) == 1


def _current_evidence(messages: list[dict], state: dict, *,
                      task: dict | None = None,
                      memoize: bool = True) -> dict | None:
    """Pair current history, reusing only unchanged receipt interpretations.

    Metadata is read on every projection: clipping, replacement, reused call
    ids and edited assistant declarations must not inherit a previous witness.
    Only parsing a paired receipt is memoized, keyed by its exact immutable
    text and interpretation kind within one round/list/accepted-state scope.
    Compaction candidates never read or publish the task's memo.
    """
    memo_key = _evidence_memo_scope(messages, state, task) if (
        memoize and isinstance(task, dict)) else None
    memo = task.get('_checklistEvidenceMemo') if memo_key is not None else None
    cached = memo.get('receipts') if (
        isinstance(memo, dict) and memo.get('key') == memo_key) else None
    retained = {} if memo_key is not None else None
    newest = _evidence_scan(messages, state,
                            cached=cached if isinstance(cached, dict) else {},
                            retained=retained)
    if memo_key is not None:
        # Publish only after the complete scan succeeds. Dropped receipts do
        # not accumulate: this replaces the previous projection's cache.
        task['_checklistEvidenceMemo'] = {'key': memo_key, 'receipts': retained}
    return newest


def _receipt_visible(message: dict, state: dict, kind: str,
                     cached: dict, retained: dict | None) -> bool:
    # Freeze text chunks rather than retaining a second joined payload. An
    # in-place edit to a list block must also invalidate the interpretation.
    text = message.get('content')
    if not isinstance(text, str):
        text = tuple(block['text'] for block in text
                     if isinstance(block, dict) and isinstance(block.get('text'), str)
                     ) if isinstance(text, list) else ()
    previous = cached.get(id(message))
    if previous is not None and previous[:2] == (kind, text):
        visible = previous[2]
    else:
        body = _message_text(message)
        visible = (_accepted_result(body, state) if kind == 'todo'
                   else bool(_gateway_result_visible(body, state)))
    if retained is not None:
        retained[id(message)] = (kind, text, visible)
    return visible


def _evidence_scan(messages: list[dict], state: dict, *,
                    cached: dict, retained: dict | None) -> dict | None:
    """Read current call batches; consume each declared occurrence once."""
    open_calls = {}
    newest = None
    for message in messages:
        role = message.get('role')
        if role == 'assistant':
            open_calls = {call.get('id'): (call.get('function') or {}).get('name', '')
                          for call in message.get('tool_calls') or []
                          if isinstance(call, dict) and call.get('id')}
        if _is_checklist_carrier(message):
            if _complete_carrier(message, state):
                newest = message
        elif role == 'tool':
            name = open_calls.pop(message.get('tool_call_id'), '')
            kind = 'todo' if name in _TODO_TOOL_NAMES else (
                'gateway' if name in _GATEWAY_TOOL_NAMES else '')
            if kind and _receipt_visible(message, state, kind, cached, retained):
                newest = message
    return newest


def _evidence_memo_scope(messages: list[dict], state: dict,
                         task: dict) -> tuple | None:
    """Stable round scope for memoizing the checklist evidence scan."""
    try:
        from lib.tasks_pkg.context_composer import _round_num
        round_num = _round_num(task)
    except Exception:
        return None
    # todo_state_from_task returns detached normalized snapshots. Their values,
    # including explanation and content, identify accepted progress, not id().
    state_key = (state['version'], state['update_count'], state['explanation'],
                 tuple((item['content'], item['status']) for item in state['todos']))
    return (round_num, id(messages), state_key)


def reconcile_checklist_context(messages: list[dict], task: dict | None,
                                *, memoize: bool = True) -> None:
    """Retain at most one current recovery carrier; keep historical tool rows.

    Only runtime-owned active_checklist sections may be removed. User text and
    neighboring managed sections are untouched. Durable UI audit notes are not
    messages and are never rewritten by this reconciliation. ``memoize=False``
    is the transient compaction-candidate path: it must not write the
    task-scoped evidence memo (rejected candidates leave no runtime effects).
    """
    if not task or not (isinstance(task.get('_todoState'), dict) or task.get('_todos')):
        return
    from lib.tasks_pkg.context_composer._sections import remove_managed_section
    evidence = _current_evidence(messages, todo_state_from_task(task),
                                 task=task, memoize=memoize)
    kept = []
    for message in messages:
        if _is_checklist_carrier(message) and message is not evidence:
            message = remove_managed_section(message, 'active_checklist')
        if message is not None:
            kept.append(message)
    messages[:] = kept


def build_checklist_context_block(messages: list[dict], task: dict | None,
                                  *, memoize: bool = True
                                  ) -> ContextBlock | None:
    if not task or not (isinstance(task.get('_todoState'), dict) or task.get('_todos')):
        return None
    state = todo_state_from_task(task)
    if _current_evidence(messages, state, task=task, memoize=memoize) is not None:
        return None
    return checklist_context_block(_snapshot_body(state))


def restore_checklist_context(messages: list[dict], *, task: dict | None,
                              model: str = '') -> None:
    """Restore only after actual evidence loss, never merely after a new update."""
    if task and task.get('aborted'):
        return
    reconcile_checklist_context(messages, task)
    block = build_checklist_context_block(messages, task)
    if block is None:
        return
    from lib.tasks_pkg.context_composer import append_context_blocks
    append_context_blocks(messages, [block], ComposeRequest(
        task=task, model=model, conv_id=str((task or {}).get('convId') or ''),
    ))


def carry_checklist_through_compaction(messages: list[dict], task: dict | None,
                                       model: str = '') -> None:
    """Project current progress into a candidate, before token/adoption checks.

    Compaction already rewrites the prefix and has its own audit event. Carry
    accepted state as part of that projection rather than delete it then emit
    another independent intervention next round. No task mutations or notes:
    rejected candidates must leave no runtime effects.
    """
    reconcile_checklist_context(messages, task, memoize=False)
    block = build_checklist_context_block(messages, task, memoize=False)
    if block is not None:
        from lib.tasks_pkg.context_composer._render import render_context
        render_context(messages, [block], ComposeRequest(model=model), replace_managed=False)
