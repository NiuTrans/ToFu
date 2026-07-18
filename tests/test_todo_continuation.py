"""tests/test_todo_continuation.py — structured todo tracking + continuation enforcer.

Backport of OMC TodoWrite + continuation enforcer / Claude Code TodoWriteTool
(Rec 1+2 of docs/omc-claude-code-backport-analysis.md). Three surfaces:

  1. ``lib.tools.todo`` — the pure ``todo_write`` core (normalize / summarize /
     incomplete filter). Unit-testable without a task or LLM.
  2. The continuation enforcer in ``lib.tasks_pkg.stream_handler.
     analyse_stream_result``: when the model tries to STOP (finish_reason=stop,
     real content, no tool calls) with incomplete ``task['_todos']``, it
     RE-DRIVES the loop (action='continue' + injected reminder) instead of
     breaking — bounded by a hard nudge cap. This is the flagship behavior;
     it gets a TRIPLE-NEUTER.
  3. ``task['_todos']`` MUST survive Layer-2 force-compaction (the epic's hard
     requirement) — it lives on the task dict, not in ``messages``.

Why the enforcer earns its keep (measured, see the epic): the zero-deliverable
guard fires on INACTION (no state-changing tool ran) and
``_check_suspicious_completion`` only LOGS content-shape anomalies. Neither
models "the agent DID work but left declared checklist items unfinished at
stop" — an agent editing files every turn never trips zero-deliverable yet can
stop at 3/10 todos. This enforcer covers exactly that gap, cheaply (mid-loop
reminder vs. a full Critic round).
"""

import threading

import pytest

import lib.tools.todo as todo
from lib.tasks_pkg.stream_handler import analyse_stream_result


def _task(**kw):
    """A minimal task dict with the event plumbing analyse_stream_result's
    append_event needs (events list + lock), like a real orchestrator task."""
    t = {'id': 'tttttttt', 'aborted': False,
         'events': [], 'events_lock': threading.Lock()}
    t.update(kw)
    return t


# ══════════════════════════════════════════════════════════
#  1. Pure todo_write core
# ══════════════════════════════════════════════════════════

def test_apply_todo_write_normalizes_and_summarizes():
    todos, text = todo.apply_todo_write({'todos': [
        {'id': '1', 'content': 'Read config', 'status': 'completed'},
        {'id': '2', 'content': 'Add retry', 'status': 'in_progress'},
        {'id': '3', 'content': 'Write test', 'status': 'pending'},
    ]})
    assert len(todos) == 3
    assert '1/3 completed' in text
    assert 'in progress' in text
    assert '[x] Read config' in text


def test_apply_todo_write_drops_malformed_and_defaults_status():
    todos, _ = todo.apply_todo_write({'todos': [
        {'id': 'a', 'content': 'valid', 'status': 'bogus'},   # bad status → pending
        {'id': 'b', 'content': '   '},                        # empty content → dropped
        'not-a-dict',                                          # dropped
        {'content': 'no id'},                                 # id synthesized
    ]})
    assert [t['status'] for t in todos] == ['pending', 'pending']
    assert todos[0]['content'] == 'valid'
    assert todos[1]['id']  # synthesized, non-empty


def test_apply_todo_write_empty_clears():
    todos, text = todo.apply_todo_write({'todos': []})
    assert todos == []
    assert 'cleared' in text.lower()


def test_incomplete_todos_filter():
    items = [
        {'id': '1', 'content': 'a', 'status': 'completed'},
        {'id': '2', 'content': 'b', 'status': 'pending'},
        {'id': '3', 'content': 'c', 'status': 'in_progress'},
    ]
    inc = todo.incomplete_todos(items)
    assert {t['id'] for t in inc} == {'2', '3'}
    assert todo.incomplete_todos([{'id': '1', 'content': 'a', 'status': 'completed'}]) == []


# ══════════════════════════════════════════════════════════
#  Enforcer harness
# ══════════════════════════════════════════════════════════

def _stop_msg(content='Here is my final answer.'):
    """An assistant message that would NORMALLY terminate the loop:
    finish_reason=stop, real content, no tool calls, no anomaly."""
    return {'role': 'assistant', 'content': content, 'reasoning_content': ''}


def _clean_usage():
    # No stream anomaly / empty-stop flags → the normal-exit path.
    return {'_stream_anomaly': False, '_empty_stop': False, '_chunks_received': 10}


def _run(task, messages, content='Final answer here.'):
    return analyse_stream_result(
        assistant_msg=_stop_msg(content),
        last_finish_reason='stop',
        task=task, tid='testtask', model='test-model',
        round_num=2, _premature_retry_count=0, messages=messages,
        usage=_clean_usage(),
    )


# ══════════════════════════════════════════════════════════
#  2. Continuation enforcer
# ══════════════════════════════════════════════════════════

def test_enforcer_redrive_on_incomplete_todos(monkeypatch):
    """★ Incomplete todos at stop → action='continue' + reminder injected."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    task = _task(_todos=[
        {'id': '1', 'content': 'done item', 'status': 'completed'},
        {'id': '2', 'content': 'unfinished item', 'status': 'pending'},
    ])
    messages = [{'role': 'user', 'content': 'do the thing'}]
    decision = _run(task, messages)
    assert decision['action'] == 'continue'
    assert task['_todo_continuation_count'] == 1
    # A reminder user-message was injected carrying the incomplete item.
    assert messages[-1]['role'] == 'user'
    assert 'TODO CONTINUATION' in messages[-1]['content']
    assert 'unfinished item' in messages[-1]['content']


def test_enforcer_allows_stop_when_all_complete(monkeypatch):
    """All todos completed → normal break, no injection."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    task = _task(_todos=[{'id': '1', 'content': 'done', 'status': 'completed'}])
    messages = [{'role': 'user', 'content': 'x'}]
    decision = _run(task, messages)
    assert decision['action'] == 'break'
    assert len(messages) == 1  # nothing injected


def test_enforcer_noop_without_todos(monkeypatch):
    """No checklist declared → enforcer never fires (plain turns unaffected)."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    task = _task()
    messages = [{'role': 'user', 'content': 'x'}]
    decision = _run(task, messages)
    assert decision['action'] == 'break'
    assert len(messages) == 1


def test_enforcer_bounded_by_cap(monkeypatch):
    """★ Runaway guard: after the cap, the stop is ALLOWED even with incomplete
    todos (a model that won't finish or update the list can't loop forever)."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    task = _task(_todo_continuation_count=3,  # cap already reached
                 _todos=[{'id': '2', 'content': 'still pending', 'status': 'pending'}])
    messages = [{'role': 'user', 'content': 'x'}]
    decision = _run(task, messages)
    assert decision['action'] == 'break'
    assert len(messages) == 1  # no further injection


def test_enforcer_disabled_by_env(monkeypatch):
    """TOFU_TODO_CONTINUATION_MAX=0 → enforcer disabled (fail-open)."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '0')
    task = _task(_todos=[{'id': '2', 'content': 'pending', 'status': 'pending'}])
    messages = [{'role': 'user', 'content': 'x'}]
    decision = _run(task, messages)
    assert decision['action'] == 'break'


def test_enforcer_only_on_real_content(monkeypatch):
    """An EMPTY stop with incomplete todos must NOT be hijacked by the enforcer
    — empty-stop has its own retry path; the enforcer needs real content
    (a genuine 'I'm done' answer) to fire."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    task = _task(_todos=[{'id': '2', 'content': 'pending', 'status': 'pending'}])
    messages = [{'role': 'user', 'content': 'x'}]
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '', 'reasoning_content': ''},
        last_finish_reason='stop',
        task=task, tid='t', model='m', round_num=2,
        _premature_retry_count=0, messages=messages,
        usage=_clean_usage(),
    )
    # Not a continue-for-todos: content was empty so the enforcer skipped it.
    assert decision['action'] == 'break'
    assert '_todo_continuation_count' not in task


# ── TRIPLE-NEUTER on the flagship enforcer ──
# Baseline (test_enforcer_redrive_on_incomplete_todos) proves it fires.
# NC-1: neuter the incomplete filter → nothing looks incomplete → no re-drive.
# NC-2: neuter the cap to 0 → disabled → no re-drive.
# (RESTORE is implicit — each test uses monkeypatch, auto-undone.)

def test_NC1_incomplete_filter_neutered(monkeypatch):
    """NC-1: force incomplete_todos→[] (as if all complete) → enforcer must NOT
    re-drive. Proves the incomplete detection is load-bearing."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    monkeypatch.setattr(todo, 'incomplete_todos', lambda todos: [])
    task = _task(_todos=[{'id': '2', 'content': 'pending', 'status': 'pending'}])
    messages = [{'role': 'user', 'content': 'x'}]
    decision = _run(task, messages)
    assert decision['action'] == 'break', 'neutered filter must not re-drive'


def test_NC2_cap_neutered_to_zero(monkeypatch):
    """NC-2: force the cap function to 0 (disabled) on the module the enforcer
    reads → no re-drive even with a real incomplete item."""
    import lib.tasks_pkg.stream_handler as sh
    monkeypatch.setattr(sh, '_todo_continuation_max', lambda: 0)
    task = _task(_todos=[{'id': '2', 'content': 'pending', 'status': 'pending'}])
    messages = [{'role': 'user', 'content': 'x'}]
    decision = _run(task, messages)
    assert decision['action'] == 'break', 'neutered cap must not re-drive'


# ══════════════════════════════════════════════════════════
#  3. _todos survives Layer-2 force-compaction
# ══════════════════════════════════════════════════════════

def test_todos_survive_force_compaction(monkeypatch):
    """★ Epic hard requirement: task['_todos'] lives on the task dict, not in
    messages, so a full L2 force-compaction (which rewrites messages) leaves it
    byte-identical."""
    import lib.tasks_pkg.compaction._layer2 as l2

    # Deterministic fake summary so no LLM is called.
    monkeypatch.setattr(l2, '_generate_query_aware_summary',
                        lambda *a, **k: '### summary of earlier work')
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: None)

    todos = [
        {'id': '1', 'content': 'first step', 'status': 'completed'},
        {'id': '2', 'content': 'second step', 'status': 'in_progress'},
        {'id': '3', 'content': 'third step', 'status': 'pending'},
    ]
    task = {'convId': 'c', 'id': 't', '_todos': todos}

    # Build a long message list so a boundary exists to compact.
    messages = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': 'the original objective'}]
    for i in range(30):
        messages.append({'role': 'assistant', 'content': f'work {i} ' + 'x' * 200})
        messages.append({'role': 'user', 'content': f'next {i}'})

    l2.execute_compact_tool(messages, task=task, preserve_budget_tokens=200)

    # The checklist is untouched by compaction.
    assert task['_todos'] == todos
    assert task['_todos'][1]['status'] == 'in_progress'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
