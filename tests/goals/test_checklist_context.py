"""Authoritative checklist identity reaches the measured, provider-bound request."""

from __future__ import annotations

import copy
import json

import pytest

from lib.llm.llm_errors import ContextCompactionError, PromptTooLongError
from lib.tasks_pkg.compaction import _prompt_admission as admission
from lib.tasks_pkg.context_composer.checklist import CHECKLIST_HEADING
from lib.tools import todo

pytestmark = pytest.mark.unit


def _task(items=None):
    items = items or [
        {'content': 'Inspect the failure', 'status': 'completed'},
        {'content': 'Repair the context boundary', 'status': 'in_progress'},
        {'content': 'Verify the regression', 'status': 'pending'},
    ]
    task = {'id': 'checklist-context', '_userId': 7, 'config': {'model': 'test-model'}}
    todo.apply_todo_write_to_task(task, {'todos': items})
    return task


def _text(messages):
    return '\n'.join(
        message['content'] if isinstance(message.get('content'), str) else
        '\n'.join(block.get('text', '') for block in message.get('content') or []
                  if isinstance(block, dict))
        for message in messages
    )


def _assert_current(messages, task):
    assert todo.render_todo_list(task['_todos']) in _text(messages)


def _admit(messages, task, round_num=0):
    return admission.enforce_dispatch_prompt_limit(
        messages, [], task, round_num=round_num, model='test-model')


def _measure_chars(monkeypatch, ceiling=100_000):
    monkeypatch.setattr(admission, '_resolved_ceiling',
                        lambda *a, **k: (ceiling, ceiling))
    monkeypatch.setattr(admission, '_count_tokens_authoritative',
                        lambda messages, *a, **k: (len(_text(messages)), 'test_chars'))


def test_round_zero_admission_restores_snapshot_before_measurement(monkeypatch):
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue the interrupted repair.'}]
    original = copy.deepcopy(messages)
    state = copy.deepcopy(task['_todoState'])
    _measure_chars(monkeypatch)

    measured = _admit(messages, task)

    _assert_current(messages, task)
    assert measured['totalTokens'] == len(_text(messages))
    assert messages[:len(original)] == original
    assert task['_todoState'] == state


@pytest.mark.parametrize('compacted', [True, False])
def test_final_summary_cannot_remove_snapshot_before_the_same_dispatch(monkeypatch, compacted):
    task = _task()
    messages = [{'role': 'user', 'content': 'x' * 20_000}]
    _measure_chars(monkeypatch, ceiling=10_000)
    # Other provider recomposition is irrelevant to this required task block.
    monkeypatch.setattr(admission, 'recompose_context_after_compaction', lambda *a, **k: None)

    def summarize(working, **kwargs):
        _assert_current(working, task)
        working[:] = [{'role': 'user', 'content': 'Summary omitted all checklist calls.'}]
        return compacted

    monkeypatch.setattr(admission, 'force_compact_if_needed', summarize)
    measured = _admit(messages, task, round_num=272)

    _assert_current(messages, task)
    assert measured['totalTokens'] == len(_text(messages))
    from lib.llm.body import build_body
    _assert_current(build_body('test-model', messages, max_tokens=2048)['messages'], task)
    # Advance with the identities actually supplied on this request, not an
    # invented replacement followed by an unchanged repair submission.
    updated = copy.deepcopy(task['_todos'])
    updated[1]['status'] = 'completed'
    updated[2]['status'] = 'in_progress'
    _, _, outcome = todo.apply_todo_write_to_task(task, {'todos': updated})
    assert not outcome['rejected'] and not outcome['no_op']
    assert task['_todoState']['update_count'] == 2


def test_required_checklist_overflow_refuses_instead_of_dispatching_partial_state(monkeypatch):
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _measure_chars(monkeypatch, ceiling=20)
    monkeypatch.setattr(admission, 'force_compact_if_needed', lambda *a, **k: False)

    with pytest.raises(ContextCompactionError):
        _admit(messages, task)

    _assert_current(messages, task)
    assert task['_lastPromptAdmission']['totalTokens'] == len(_text(messages))


def _restore(messages, task):
    from lib.tasks_pkg.context_composer.checklist import restore_checklist_context
    restore_checklist_context(messages, task=task, model='test-model')


def test_unchanged_state_preserves_cache_prefix_and_ignores_quoted_headings():
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    messages.extend([
        {'role': 'assistant', 'tool_calls': [{'id': 'read', 'function': {
            'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'read',
         'content': 'Source example: ## Active Task Checklist\n<!-- chatui-todo-state:v1:wrong -->'},
    ])
    frozen = copy.deepcopy(messages)
    for _ in range(12):
        _restore(messages, task)
    assert messages == frozen
    assert len([row for row in task['_contextManifest'] if row.get('appended')]) == 1


@pytest.mark.parametrize('damage', ['tail_missing', 'status_changed', 'text_missing'])
def test_surviving_marker_does_not_prove_a_complete_current_carrier(damage):
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    block = messages[-1]['content'][0]
    if damage == 'tail_missing':
        block['text'] = block['text'].split('- [ ] Verify')[0]
    elif damage == 'status_changed':
        block['text'] = block['text'].replace('Repair the context boundary ⏳', 'Repair the context boundary [blocked]')
    else:
        block['text'] = block['text'].replace('Verify the regression', '')
    prefix = copy.deepcopy(messages)

    _restore(messages, task)

    assert messages[0] == prefix[0]
    assert len(messages) == len(prefix)
    _assert_current(messages[-1:], task)


def test_versioned_call_does_not_substitute_for_accepted_runtime_snapshot():
    task = _task()
    messages = [
        {'role': 'assistant', 'tool_calls': [{'id': 'plan', 'function': {
            'name': 'todo_write', 'arguments': json.dumps({'todos': task['_todos']})}}]},
        {'role': 'tool', 'tool_call_id': 'plan', 'content': todo.render_todo_list(task['_todos'])},
    ]
    frozen = copy.deepcopy(messages)
    _restore(messages, task)
    _assert_current(messages[-1:], task)
    assert 'Accepted task progress preserved by the runtime' in _text(messages[-1:])
    assert messages[:len(frozen)] == frozen


def test_attempted_call_does_not_replace_accepted_snapshot():
    task = _task([{'content': 'Keep identity', 'status': 'in_progress'}])
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'plan', 'function': {
        'name': 'todo_write', 'arguments': json.dumps({'todos': [
            {'content': 'Keep identity', 'status': 'in_progress'}]})}}]},
        {'role': 'tool', 'tool_call_id': 'plan', 'content': 'accepted'}]
    _restore(messages, task)
    _assert_current(messages, task)


def _write_and_settle(task, messages, items, tc_id='tw1'):
    """Apply one write and settle its runtime-authored result on the wire,
    exactly as the tool pipeline pairs the assistant call with its result."""
    _, result_text, outcome = todo.apply_todo_write_to_task(task, {'todos': items})
    assert not outcome['rejected']
    messages.extend([
        {'role': 'assistant', 'tool_calls': [{'id': tc_id, 'function': {
            'name': 'todo_write', 'arguments': json.dumps({'todos': items})}}]},
        {'role': 'tool', 'tool_call_id': tc_id, 'content': result_text},
    ])
    return outcome


def test_result_text_contains_no_opaque_state_marker():
    task = _task()
    _, text, outcome = todo.apply_todo_write_to_task(task, {'todos': [
        {'content': 'Only step', 'status': 'in_progress'}]})
    assert not outcome['rejected'] and not outcome['no_op']
    assert text.endswith('- [ ] Only step ⏳')
    assert '<!--' not in text
    assert 'chatui' not in text
    assert text.startswith('Checklist updated: 0/1 completed.')


def test_accepted_todo_result_proves_visibility_without_reinjection():
    # Root fix for per-round redundancy: an update the model just performed
    # and saw acknowledged must NOT be re-injected at the next round's
    # message hygiene — the settled result already renders the full state.
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    updated = copy.deepcopy(task['_todos'])
    updated[1]['status'] = 'completed'
    updated[2]['status'] = 'in_progress'
    _write_and_settle(task, messages, updated)
    frozen = copy.deepcopy(messages)

    _restore(messages, task)

    assert messages == frozen
    from lib.tasks_pkg.attachments import compute_turn_attachments
    assert not compute_turn_attachments(messages, task, round_num=2, conv_id='')


def test_stale_settled_result_rearms_restoration():
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _write_and_settle(task, messages, copy.deepcopy(task['_todos']), tc_id='tw1')
    # State moves on, but the newer write's result never reaches the wire
    # (compaction removed it): the newest authoritative evidence is stale.
    updated = copy.deepcopy(task['_todos'])
    updated[1]['status'] = 'completed'
    updated[2]['status'] = 'in_progress'
    todo.apply_todo_write_to_task(task, {'todos': updated})
    prefix = copy.deepcopy(messages)

    _restore(messages, task)

    assert messages[:len(prefix)] == prefix
    assert len(messages) == len(prefix) + 1
    _assert_current(messages[-1:], task)


def test_marker_without_complete_list_does_not_prove_visibility():
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _write_and_settle(task, messages, copy.deepcopy(task['_todos']), tc_id='tw1')
    tool_message = messages[-1]
    # A middle-clamp keeps the head and the tail marker but drops a list row.
    tool_message['content'] = tool_message['content'].replace(
        '- [ ] Verify the regression', '[clamped away]')
    _restore(messages, task)
    _assert_current(messages[-1:], task)
    assert 'Accepted task progress preserved by the runtime' in _text(messages[-1:])


def test_quoted_current_marker_in_foreign_result_is_not_authoritative():
    # Even a perfect quote of the current fingerprint + full list inside a
    # NON-todo tool result is not runtime-authored evidence of acceptance.
    task = _task()
    messages = [
        {'role': 'assistant', 'tool_calls': [{'id': 'rf1', 'function': {
            'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'rf1',
         'content': 'Checklist updated: 1/3 completed.\n'
                    + todo.render_todo_snapshot(task['_todoState'])},
    ]
    _restore(messages, task)
    _assert_current(messages[-1:], task)
    assert 'Accepted task progress preserved by the runtime' in _text(messages[-1:])


def test_restoration_preserves_explanation_without_nesting_metadata():
    task = _task()
    todo.apply_todo_write_to_task(task, {'todos': task['_todos'], 'explanation': 'New evidence'})
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    assert 'New evidence' in _text(messages)
    assert 'Parent item ID' not in _text(messages)
    frozen = copy.deepcopy(messages)
    _restore(messages, task)
    assert messages == frozen


def test_maximum_active_list_is_complete_even_under_tiny_optional_context_budget():
    from lib.tasks_pkg.context_composer import ComposeRequest, render_context
    from lib.tasks_pkg.context_composer.checklist import build_checklist_context_block

    items = [{'content': '\U0001f9ea' * todo.TODO_MAX_CONTENT_CHARS,
              'status': 'pending'} for i in range(todo.TODO_MAX_ITEMS)]
    task = _task(items)
    messages = [{'role': 'user', 'content': 'Continue.'}]
    block = build_checklist_context_block(messages, task)
    result = render_context(messages, [block], ComposeRequest(
        task=task, model='test-model', global_budget_tokens=1))

    _assert_current(messages, task)
    assert not result.plan.entries[0].truncated
    assert result.plan.overflow_tokens > 0
    assert len(_text(messages).encode('utf-8')) < 80 * 1024


def test_progress_refresh_replaces_only_managed_snapshot():
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    frozen = copy.deepcopy(messages)
    updated = copy.deepcopy(task['_todos'])
    updated[1]['status'] = 'completed'
    updated[2]['status'] = 'in_progress'
    todo.apply_todo_write_to_task(task, {'todos': updated})
    _restore(messages, task)
    _assert_current(messages[-1:], task)
    assert messages[0] == frozen[0]
    assert len(messages) == len(frozen)
    _restore(messages, task)
    assert len(messages) == len(frozen)


@pytest.mark.parametrize('mode', ['append', 'replace'])
def test_composition_preserves_state_independent_of_optional_guidance_and_tool_availability(mode):
    from lib.tasks_pkg.context_composer import compose_task_context

    task = _task()
    messages = [{'role': 'system', 'content': 'Operator-owned policy.'},
                {'role': 'user', 'content': 'Continue.'}]
    compose_task_context(messages, user_id=7, task=task, model='test-model',
        has_real_tools=False, tool_names=set(), system_prompt_mode=mode,
        disabled_blocks={'active_checklist', 'todo', 'task_state_v1'})
    _assert_current(messages, task)
    assert messages[0]['content'] == 'Operator-owned policy.'
    assert 'Use only currently available tools' in _text(messages)
    assert task['_todoState']['update_count'] == 1


def test_owner_isolation_and_no_state_do_not_read_another_task():
    first = _task([{'content': 'Private task A', 'status': 'pending'}])
    second = _task([{'content': 'Private task B', 'status': 'pending'}])
    messages_a, messages_b = [], []
    _restore(messages_a, first)
    _restore(messages_b, second)
    assert 'Private task B' not in _text(messages_a)
    assert 'Private task A' not in _text(messages_b)
    empty = [{'role': 'user', 'content': 'No checklist.'}]
    frozen = copy.deepcopy(empty)
    _restore(empty, {'_userId': 9})
    assert empty == frozen


def test_optional_file_attachment_cannot_truncate_required_list():
    from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments

    task = _task([{'content': '验证' * 256, 'status': 'pending'}
                  for i in range(todo.TODO_MAX_ITEMS)])
    messages = [{'role': 'user', 'content': 'Continue.'}]
    attachments = ['<system-reminder>\n## Recently Modified Files\n' + 'file.py\n' * 4000 + '</system-reminder>']
    attachments += compute_turn_attachments(messages, task, round_num=1, conv_id='')
    inject_attachments(messages, attachments, task=task, model='test-model')
    _assert_current(messages, task)
    assert not compute_turn_attachments(messages, task, round_num=2, conv_id='')


def test_continue_resume_and_final_body_receive_current_snapshot(monkeypatch):
    import threading
    from lib.llm.body import build_body
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state

    checkpoint = _task()['_todoState']
    task = {'_userId': 7, 'content': '',
            'content_lock': threading.Lock(), 'config': {'model': 'test-model'}}
    messages = [{'role': 'user', 'content': 'Continue from summary without tool calls.'}]
    apply_resume_state(task=task, cfg={'checkpointTodoState': checkpoint},
                       messages=messages, model='test-model', tid='resume')
    _measure_chars(monkeypatch)
    measurement = _admit(messages, task, round_num=0)
    body = build_body('test-model', messages, max_tokens=2048,
                      precomputed_input_tokens=measurement['totalTokens'])
    _assert_current(body['messages'], task)
    assert task['_todoState'] == checkpoint
    assert all('_contextComposer' not in row for row in body['messages'])


@pytest.mark.parametrize('memory_pressure', [False, True])
@pytest.mark.parametrize('compacted', [False, True])
def test_reactive_retry_restores_state_before_building_the_same_model_body(monkeypatch, memory_pressure, compacted):
    from lib.cgroup_guard import MemoryPressureError
    from lib.tasks_pkg.compaction import api as compaction
    from lib.tasks_pkg.llm_fallback import _call as fallback

    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    state = copy.deepcopy(task['_todoState'])
    observed = []

    def recover(working, **kwargs):
        observed.append(kwargs.get('byte_target'))
        working[:] = [{'role': 'user', 'content': 'Compacted without checklist.'}]
        return compacted

    monkeypatch.setattr(compaction, 'reactive_compact', recover)
    cause = MemoryPressureError('memory headroom') if memory_pressure else PromptTooLongError('too long')
    body = fallback._prepare_reactive_retry_body(
        task, {'temperature': 0.3}, 'test-model', messages, [], 'medium', False,
        cause, 'test')
    assert body is not None
    _assert_current(body['messages'], task)
    assert body['model'] == 'test-model'
    assert body['_task_id'] == task['id']
    assert task['_todoState'] == state
    assert bool(observed[0]) is memory_pressure


def test_reactive_restoration_failure_never_builds_an_incomplete_body(monkeypatch):
    from lib.tasks_pkg.compaction import api as compaction
    from lib.tasks_pkg.context_composer import checklist
    from lib.tasks_pkg.llm_fallback import _call as fallback

    monkeypatch.setattr(compaction, 'reactive_compact', lambda *a, **k: True)

    def broken(*args, **kwargs):
        raise ValueError('invalid state')

    monkeypatch.setattr(checklist, 'restore_checklist_context', broken)
    monkeypatch.setattr(fallback, 'build_body', lambda *a, **k: pytest.fail('must not build without state'))
    cause = PromptTooLongError('original provider limit')
    assert fallback._prepare_reactive_retry_body(
        _task(), {}, 'test-model', [], [], 'medium', False, cause, 'test') is None


def test_cleared_versioned_state_replaces_stale_active_carrier_without_rewriting_history():
    task = _task()
    messages = [{'role': 'user', 'content': 'Change the plan.'}]
    _restore(messages, task)
    frozen = copy.deepcopy(messages)
    _, _, outcome = todo.apply_todo_write_to_task(task, {
        'explanation': 'The user cancelled this scope.', 'todos': []})
    assert not outcome['rejected']
    _restore(messages, task)
    assert 'The accepted todo list is empty.' in _text(messages[-1:])
    assert messages[0] == frozen[0]
    assert len(messages) == len(frozen)
    assert 'Repair the context boundary' not in _text(messages)


@pytest.mark.parametrize('profile', ['default', 'codex'])
def test_gpt56_responses_wire_preserves_complete_checklist_and_does_not_grant_tools(profile):
    from lib.llm.body import build_body
    from lib.llm.responses_outbound._to_responses import openai_body_to_responses

    task = _task()
    messages = [{'role': 'user', 'content': 'Continue the authorized work.'}]
    _restore(messages, task)
    body = build_body('gpt-5.6-sol', messages, max_tokens=2048, tools=[])
    wire, _ = openai_body_to_responses(body, profile=profile)
    _assert_current(wire['input'], task)
    assert not wire.get('tools')
    assert all('_contextComposer' not in row for row in wire['input'])


def test_reactive_restored_checklist_is_counted_by_final_transport_limit(monkeypatch):
    from lib.llm.body._admission import enforce_body_input_limit
    from lib.tasks_pkg.compaction import api as compaction
    from lib.tasks_pkg.compaction import _tokens
    from lib.tasks_pkg.llm_fallback import _call as fallback
    from lib.token_counter import count_tokens

    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    monkeypatch.setattr(compaction, 'reactive_compact', lambda *a, **k: False)
    body = fallback._prepare_reactive_retry_body(
        task, {}, 'test-model', messages, [], 'medium', False,
        PromptTooLongError('too long'), 'test')
    expected = count_tokens(body['messages'], model='test-model',
                            tools=body.get('tools'), mode='tiktoken')['tokens']
    assert enforce_body_input_limit(body, model='test-model') == expected
    assert expected > 1
    monkeypatch.setattr(_tokens, 'resolve_model_context_limit', lambda *a, **k: 1)
    with pytest.raises(PromptTooLongError):
        enforce_body_input_limit(body, model='test-model')
    _assert_current(body['messages'], task)



def test_later_non_checklist_injection_does_not_reinject_without_compaction():
    """Invariant guard: the dedup scan looks at EVERY composer message for the
    checklist snapshot, so a later modified_files/current_date injection (a NEW
    tail composer message, per the composer lifecycle split) must not push the
    checklist out of a narrow window and cause a spurious re-inject. Locks the
    scan-wide behaviour against a future regression that narrows it."""
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    after_checklist = copy.deepcopy(messages)
    # A different ambient injection lands as a NEW tail composer message,
    # pushing the checklist block out of the old one-message dedup window.
    messages.append({
        'role': 'user', '_contextComposer': True,
        'content': [{'type': 'text',
                     'text': '<system-reminder>\n## Recently Modified Files\n'
                             '  - a.py\n</system-reminder>'}],
    })
    _restore(messages, task)
    # No compaction, snapshot unchanged -> must NOT inject again.
    assert len([m for m in messages
                if m.get('_contextComposer') and m.get('role') == 'user'
                and CHECKLIST_HEADING
                in _text([m])]) == 1
    # The checklist composer message is still the original one, untouched.
    assert messages[1] == after_checklist[1]


@pytest.mark.parametrize('mode', ['calls', 'program'])
def test_gateway_accepted_receipt_avoids_restore_across_rounds(mode):
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    _, output, _ = todo.apply_todo_write_to_task(task, {
        'todos': task['_todos'], 'explanation': 'Updated evidence only.'})
    receipt = {'name': 'todo_write', 'status': 'done',
               'output_truncated': False, 'output': output}
    payload = {'results': [receipt]} if mode == 'calls' else {
        'program': {'status': 'ok', 'result': 'ignored', 'childResults': [receipt]}}
    tool_pair = [
        {'role': 'assistant', 'tool_calls': [{'id': 'gateway', 'function': {
            'name': 'execute_tools', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'gateway', 'content': json.dumps(payload)},
    ]
    messages.extend(copy.deepcopy(tool_pair))
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD
    notes = copy.deepcopy(task.get(INJECTED_NOTES_FIELD))
    for round_num in (2, 3, 17):
        task['_activeRoundNum'] = round_num
        _restore(messages, task)
    assert messages == [{'role': 'user', 'content': 'Continue.'}] + tool_pair
    assert task.get(INJECTED_NOTES_FIELD) == notes


@pytest.mark.parametrize('damage', ['truncated', 'failed', 'foreign', 'program_return', 'stale'])
def test_gateway_non_evidence_cannot_hide_missing_current_snapshot(damage):
    task = _task()
    _, output, _ = todo.apply_todo_write_to_task(task, {'todos': task['_todos']})
    receipt = {'name': 'todo_write', 'status': 'done', 'output': output}
    payload = {'results': [receipt]}
    if damage == 'truncated':
        receipt['output_truncated'] = True
    elif damage == 'failed':
        receipt['status'] = 'error'
    elif damage == 'foreign':
        receipt['name'] = 'read_files'
    elif damage == 'program_return':
        payload = {'program': {'result': receipt, 'childResults': []}}
    else:
        todo.apply_todo_write_to_task(task, {'todos': task['_todos'], 'explanation': 'Newer'})
    messages = [
        {'role': 'assistant', 'tool_calls': [{'id': 'gateway', 'function': {
            'name': 'execute_tools', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'gateway', 'content': json.dumps(payload)},
    ]
    frozen = copy.deepcopy(messages)
    _restore(messages, task)
    assert messages[:2] == frozen
    _assert_current(messages[-1:], task)


def test_recycled_call_id_does_not_make_foreign_quote_authoritative():
    task = _task()
    messages = []
    _write_and_settle(task, messages, task['_todos'], tc_id='same')
    output = messages.pop()['content']
    messages.extend([
        {'role': 'assistant', 'tool_calls': [{'id': 'same', 'function': {
            'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'same', 'content': output},
    ])
    _restore(messages, task)
    _assert_current(messages[-1:], task)


def test_mixed_carrier_removes_only_obsolete_checklist_section():
    from lib.tasks_pkg.context_composer import ComposeRequest, render_context, context_block
    from lib.tasks_pkg.context_composer.checklist import build_checklist_context_block
    task = _task()
    messages = [{'role': 'user', 'content': 'Keep the real user text.'}]
    block = build_checklist_context_block(messages, task)
    render_context(messages, [block, context_block('modified_files', 'Sibling file evidence')],
                   ComposeRequest(task=task))
    # Renderer may split by authority: explicitly exercise a merged managed carrier.
    managed = [m for m in messages if m.get('_contextComposer')]
    carrier = copy.deepcopy(managed[0])
    carrier['content'] = [{'type': 'text', 'text': '\n'.join(_text([m]) for m in managed)}]
    carrier['_contextBlocks'] = [b for m in managed for b in m['_contextBlocks']]
    messages = [messages[0], carrier]
    _write_and_settle(task, messages, [], tc_id='clear')
    history = copy.deepcopy(messages[2:])
    _restore(messages, task)
    assert 'Sibling file evidence' in _text(messages)
    assert CHECKLIST_HEADING not in _text(messages)
    assert messages[0]['content'] == 'Keep the real user text.'
    assert messages[2:] == history
    assert all(b['id'] != 'active_checklist' for b in messages[1]['_contextBlocks'])


def test_compaction_carries_state_without_emitting_another_intervention():
    from lib.tasks_pkg.context_composer.checklist import carry_checklist_through_compaction
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _restore(messages, task)
    notes = copy.deepcopy(task[INJECTED_NOTES_FIELD])
    candidate = [{'role': 'user', 'content': 'Compaction summary.'}]
    carry_checklist_through_compaction(candidate, task)
    _assert_current(candidate, task)
    for round_num in (9, 10):
        task['_activeRoundNum'] = round_num
        _restore(candidate, task)
    assert task[INJECTED_NOTES_FIELD] == notes
    assert _text(candidate).count(CHECKLIST_HEADING) == 1


def test_restored_explanation_identifies_accepted_update_not_injection_reason():
    task = _task()
    todo.apply_todo_write_to_task(task, {'todos': task['_todos'], 'explanation': 'Evidence A'})
    messages = []
    _restore(messages, task)
    assert 'Progress explanation from the accepted todo update: Evidence A' in _text(messages)
    assert 'chatui' not in _text(messages)


@pytest.mark.parametrize('gateway', [False, True])
@pytest.mark.parametrize('later', ['truncated', 'failed', 'unrelated'])
def test_later_non_evidence_does_not_erase_intact_current_witness(gateway, later):
    task = _task()
    messages = []
    _write_and_settle(task, messages, task['_todos'])
    output = messages[-1]['content']
    if gateway:
        row = {'name': 'todo_write', 'status': 'done', 'output': output}
        if later == 'truncated':
            row['output_truncated'] = True
        elif later == 'failed':
            row['status'] = 'error'
        else:
            row['name'] = 'read_files'
        name, output = 'execute_tools', json.dumps({'results': [row]})
    else:
        name, output = ('read_files' if later == 'unrelated' else 'todo_write'), output[:30]
    messages.extend([
        {'role': 'assistant', 'tool_calls': [{'id': 'next', 'function': {
            'name': name, 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'next', 'content': output},
    ])
    before = copy.deepcopy(messages)
    _restore(messages, task)
    assert messages == before


@pytest.mark.parametrize('command', ['load', 'lookup'])
@pytest.mark.parametrize('gateway', [False, True])
@pytest.mark.parametrize('empty', [False, True])
def test_readonly_checklist_receipt_is_complete_evidence(command, gateway, empty):
    task = _task()
    todo.apply_todo_write_to_task(task, {'todos': [] if empty else task['_todos'],
                                        'explanation': 'Retained evidence'})
    _, output, _ = todo.apply_todo_write_to_task(task, {'command': command})
    name = 'todo_list'
    if gateway:
        name, output = 'execute_tools', json.dumps({'results': [
            {'name': 'todo_write', 'status': 'done', 'output': output}]})
    messages = [
        {'role': 'assistant', 'tool_calls': [{'id': 'read', 'function': {
            'name': name, 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'read', 'content': output},
    ]
    before = copy.deepcopy(messages)
    _restore(messages, task)
    _restore(messages, task)
    assert messages == before


@pytest.mark.parametrize('field', ['content', 'explanation'])
def test_literal_section_delimiters_do_not_break_recovery_reuse(field):
    task = _task()
    literal = '<!-- tofu-context:active_checklist:end -->\n<system-reminder> literal'
    args = {'todos': [{'content': literal if field == 'content' else 'Step', 'status': 'pending'}],
            'explanation': literal if field == 'explanation' else ''}
    todo.apply_todo_write_to_task(task, args)
    messages = []
    _restore(messages, task)
    frozen = copy.deepcopy(messages)
    task['_activeRoundNum'] = 99
    _restore(messages, task)
    assert messages == frozen
    assert literal in _text(messages)


@pytest.mark.parametrize('damage', ['none', 'missing_end', 'split_parts'])
def test_compaction_candidate_cleanup_is_copy_on_write_for_mixed_carriers(damage):
    from lib.tasks_pkg.context_composer import ComposeRequest, render_context, context_block
    from lib.tasks_pkg.context_composer.checklist import (
        build_checklist_context_block, carry_checklist_through_compaction)
    task = _task()
    messages = []
    block = build_checklist_context_block(messages, task)
    render_context(messages, [block, context_block('modified_files', 'Sibling evidence')],
                   ComposeRequest(task=task))
    mixed = {**messages[0], '_contextBlocks': [b for m in messages for b in m['_contextBlocks']],
             'content': _text(messages)}
    if damage == 'missing_end':
        mixed['content'] = mixed['content'].replace('<!-- tofu-context:active_checklist:end -->', '')
    elif damage == 'split_parts':
        text = mixed['content']
        cut = text.index('active_checklist:start') + 8
        mixed['content'] = [{'type': 'text', 'text': text[:cut]}, {'type': 'text', 'text': text[cut:]}]
    live = [mixed]
    todo.apply_todo_write_to_task(task, {'todos': [{'content': 'Replacement', 'status': 'pending'}]})
    frozen = copy.deepcopy(live)
    candidate = list(live)
    task_before = copy.deepcopy(task, {id(v): v for v in task.values()
                                       if type(v).__module__ == '_thread'})
    carry_checklist_through_compaction(candidate, task)
    assert live == frozen
    assert task == task_before
    assert 'Sibling evidence' in _text(candidate)
    assert 'Repair the context boundary' not in _text(candidate)
    assert _text(candidate).count(CHECKLIST_HEADING) == 1


@pytest.mark.parametrize('layer', ['micro', 'advanced'])
def test_local_compaction_carries_lost_receipt_without_intervention(monkeypatch, layer):
    from lib.tasks_pkg.compaction import _layer1, _advanced
    from lib.tasks_pkg.context_composer.checklist import build_checklist_context_block
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD
    task = _task()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    _write_and_settle(task, messages, task['_todos'])
    assert build_checklist_context_block(messages, task) is None
    state = copy.deepcopy(task['_todoState'])
    def lose_evidence(steps, ctx, **kwargs):
        ctx.messages[:] = ctx.messages[:1]
        return 100
    module = _layer1 if layer == 'micro' else _advanced
    from lib.tasks_pkg.compaction import _steps
    monkeypatch.setattr(_steps if layer == 'micro' else module, 'run_steps', lose_evidence)
    if layer == 'micro':
        module.micro_compact(messages, task=task, steps=['compact_tool_results'], _dry_run=True)
    else:
        module.advanced_compact(messages, task=task, advanced_steps=['compact_tool_results'])
    _assert_current(messages, task)
    assert build_checklist_context_block(messages, task) is None
    assert not task.get(INJECTED_NOTES_FIELD)
    assert task['_todoState'] == state
    _restore(messages, task)
    assert not task.get(INJECTED_NOTES_FIELD)


@pytest.mark.parametrize('metric', ['tokens', 'bytes'])
def test_head_truncation_carries_dropped_todo_receipt_inside_budget(metric):
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate
    from lib.tasks_pkg.compaction._reactive._measure import _estimate_wire_bytes
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
    from lib.tasks_pkg.context_composer.checklist import build_checklist_context_block
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD
    task = _task()
    messages = [{'role': 'user', 'content': 'Opening request'}]
    _write_and_settle(task, messages, task['_todos'])
    messages += [{'role': 'assistant', 'content': 'old text ' * 1000} for _ in range(8)]
    messages += [{'role': 'assistant', 'tool_calls': [{'id': 'latest', 'function': {
        'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'latest', 'content': 'Recent evidence'},
        {'role': 'user', 'content': 'Current direction'}]
    kwargs = {'token_target': 2500} if metric == 'tokens' else {'byte_target': 12500}
    assert _head_truncate(messages, task, **kwargs) > 0
    assert not any(m.get('tool_call_id') == 'todo-1' for m in messages)
    _assert_current(messages, task)
    assert build_checklist_context_block(messages, task) is None
    measure = _estimate_total_tokens if metric == 'tokens' else _estimate_wire_bytes
    assert measure(messages) <= next(iter(kwargs.values()))
    assert not task.get(INJECTED_NOTES_FIELD)
    _restore(messages, task)
    assert not task.get(INJECTED_NOTES_FIELD)


@pytest.mark.parametrize('explanation', ['', 'Actual reason\nExplanation: verbatim reason'])
def test_recovery_provenance_label_only_applies_to_explanation_field(explanation):
    from lib.tasks_pkg.context_composer.checklist import _snapshot_body
    from lib.tools.todo import render_todo_snapshot

    content = 'Investigate\nExplanation: quoted user text'
    state = {'todos': [{'content': content, 'status': 'pending'}],
             'explanation': explanation, 'update_count': 1, 'version': 3}
    body = _snapshot_body(state)
    assert content in body
    assert content in render_todo_snapshot(state)
    label = 'Progress explanation from the accepted todo update: '
    assert body.count(label) == bool(explanation)
    if explanation:
        assert body.endswith(label + explanation)
    task = {'_todoState': state, 'convId': 'multiline-provenance', 'userId': 7}
    messages = [{'role': 'user', 'content': 'Keep working'}]
    _restore(messages, task)
    before = copy.deepcopy(messages)
    _restore(messages, task)
    assert messages == before
