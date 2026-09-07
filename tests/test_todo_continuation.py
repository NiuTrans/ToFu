"""tests/test_todo_continuation.py — structured todo tracking + continuation enforcer.

Backport of OMC TodoWrite + continuation enforcer / Claude Code TodoWriteTool
(Rec 1+2 of docs/modules/task_engine.md). Three surfaces:

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

import json
import threading
import time

import pytest

import lib.tools.todo as todo
from lib.tasks_pkg.stream_handler.api import analyse_stream_result


def _task(**kw):
    """A minimal task dict with the event plumbing analyse_stream_result's
    append_event needs (events list + lock), like a real orchestrator task."""
    t = {'id': 'tttttttt', '_userId': 1, 'aborted': False,
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
    assert 'reuse each id exactly in later sync calls' in text
    assert 'id="1"' in text


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


def test_sync_revises_one_checklist_and_duplicate_is_noop():
    first = todo.apply_todo_operation(None, {'todos': [
        {'id': 'a', 'content': 'A', 'status': 'in_progress'},
        {'id': 'b', 'content': 'B', 'status': 'pending'},
    ]})
    frame = first['state']['stack'][0]
    checklist_id = frame['checklist_id']
    revision = frame['revision']

    duplicate = todo.apply_todo_operation(first['state'], {'todos': first['todos']})
    assert duplicate['no_op'] is True
    assert duplicate['state']['stack'][0]['checklist_id'] == checklist_id
    assert duplicate['state']['stack'][0]['revision'] == revision

    advanced = todo.apply_todo_operation(duplicate['state'], {'todos': [
        {'id': 'a', 'content': 'A', 'status': 'completed'},
        {'id': 'b', 'content': 'B', 'status': 'in_progress'},
    ]})
    assert advanced['state']['stack'][0]['checklist_id'] == checklist_id
    assert advanced['state']['stack'][0]['revision'] == revision + 1

    completed = todo.apply_todo_operation(advanced['state'], {'todos': [
        {'id': 'a', 'content': 'A', 'status': 'completed'},
        {'id': 'b', 'content': 'B', 'status': 'completed'},
    ]})
    final_duplicate = todo.apply_todo_operation(
        completed['state'], {'todos': completed['todos']})
    assert final_duplicate['state']['root_completed'] is True
    assert final_duplicate['no_op'] is True
    assert final_duplicate['rejected'] is False


def test_sync_cannot_drop_unfinished_but_replan_can_with_reason():
    initial = todo.apply_todo_operation(None, {'todos': [
        {'id': 'keep', 'content': 'Keep', 'status': 'in_progress'},
        {'id': 'drop', 'content': 'Drop', 'status': 'pending'},
    ]})
    rejected = todo.apply_todo_operation(initial['state'], {
        'operation': 'sync',
        'todos': [{'id': 'keep', 'content': 'Keep', 'status': 'in_progress'}],
    })
    assert rejected['rejected'] is True
    assert {x['id'] for x in rejected['todos']} == {'keep', 'drop'}

    no_reason = todo.apply_todo_operation(initial['state'], {
        'operation': 'replan', 'todos': [],
    })
    assert no_reason['rejected'] is True

    replanned = todo.apply_todo_operation(initial['state'], {
        'operation': 'replan', 'reason': 'Requirement changed',
        'todos': [{'id': 'new', 'content': 'New path', 'status': 'in_progress'}],
    })
    assert replanned['rejected'] is False
    audit = replanned['state']['history'][-1]
    assert audit['kind'] == 'replan'
    assert audit['reason'] == 'Requirement changed'
    assert {x['id'] for x in audit['superseded']} == {'keep', 'drop'}


def test_rejected_sync_repeats_authoritative_ids_for_model_repair():
    task = {'id': 'todo-id-repair'}
    todo.apply_todo_write_to_task(task, {'todos': [
        {'id': 'spec', 'content': 'Add regression', 'status': 'in_progress'},
        {'id': 'verify', 'content': 'Run tests', 'status': 'pending'},
    ]})

    todos, text, outcome = todo.apply_todo_write_to_task(task, {'todos': [
        {'id': 'test', 'content': 'Add regression', 'status': 'in_progress'},
        {'id': 'validate', 'content': 'Run tests', 'status': 'pending'},
    ]})

    assert outcome['rejected'] is True
    assert 'sync cannot remove unfinished items (spec, verify)' in text
    assert 'id="spec"' in text and 'id="verify"' in text
    assert 'id="test"' not in text and 'id="validate"' not in text
    assert [item['id'] for item in todos] == ['spec', 'verify']


def test_child_completion_auto_pops_and_completes_parent_item():
    root = todo.apply_todo_operation(None, {'todos': [
        {'id': 'parent', 'content': 'Build feature', 'status': 'in_progress'},
        {'id': 'verify', 'content': 'Verify', 'status': 'pending'},
    ]})
    child = todo.apply_todo_operation(root['state'], {
        'operation': 'push', 'parent_todo_id': 'parent',
        'todos': [
            {'id': 'c1', 'content': 'Implement', 'status': 'in_progress'},
            {'id': 'c2', 'content': 'Test', 'status': 'pending'},
        ],
    })
    assert len(child['state']['stack']) == 2
    child_id = child['state']['stack'][-1]['checklist_id']

    restored = todo.apply_todo_operation(child['state'], {'todos': [
        {'id': 'c1', 'content': 'Implement', 'status': 'completed'},
        {'id': 'c2', 'content': 'Test', 'status': 'completed'},
    ]})
    assert restored['auto_popped'] == [child_id]
    assert len(restored['state']['stack']) == 1
    parent = {x['id']: x for x in restored['todos']}
    assert parent['parent']['status'] == 'completed'
    assert parent['verify']['status'] == 'pending'


def test_same_round_todo_calls_execute_in_model_order(monkeypatch):
    """A child push emitted after its root sync cannot overtake that sync.

    The ordered-state lane is intentionally distinct from the external-write
    lane, so this also runs in attended Manual mode without asking approval.
    """
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.handlers.misc._human import _handle_todo_write
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline, parse_tool_calls

    original = tool_registry.lookup('todo_write')
    starts = []

    def delayed_first(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                      cfg, project_path, project_enabled, all_tools=None):
        starts.append(tc_id)
        if tc_id == 'todo-root':
            time.sleep(0.05)
        return original(
            task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
            project_path, project_enabled, all_tools=all_tools)

    monkeypatch.setitem(tool_registry._exact, 'todo_write', delayed_first)
    task = _task(
        convId='todo-order', status='running', model='test-model',
        toolRounds=[], _attended=True,
        _tool_schema=[todo.TODO_WRITE_TOOL],
    )
    root_args = {'todos': [
        {'id': 'feature', 'content': 'Build feature', 'status': 'in_progress'},
    ]}
    child_args = {
        'operation': 'push', 'parent_todo_id': 'feature',
        'todos': [
            {'id': 'impl', 'content': 'Implement', 'status': 'in_progress'},
        ],
    }
    assistant = {'content': '', 'tool_calls': [
        {'id': 'todo-root', 'type': 'function', 'function': {
            'name': 'todo_write', 'arguments': json.dumps(root_args)}},
        {'id': 'todo-child', 'type': 'function', 'function': {
            'name': 'todo_write', 'arguments': json.dumps(child_args)}},
    ]}
    parsed, _ = parse_tool_calls(
        assistant, task, round_num=0, tool_round_num=0,
        project_enabled=False)

    messages = []
    execute_tool_pipeline(
        task, parsed, cfg={'autoApply': False}, project_path=None,
        project_enabled=False, tool_list=[todo.TODO_WRITE_TOOL],
        messages=messages, all_search_results_text=[], round_num=0,
        model='test-model')

    assert starts == ['todo-root', 'todo-child']
    assert len(task['_todoState']['stack']) == 2
    assert task['_todoState']['stack'][-1]['parent_todo_id'] == 'feature'
    assert all('rejected' not in str(msg.get('content', '')).lower()
               for msg in messages)


def test_replay_compacts_todo_revisions_but_keeps_audit_input_unchanged():
    rounds = []
    for n in range(3):
        rounds.append({
            'toolName': 'todo_write', 'toolCallId': f't{n}',
            'results': [{'todoNoop': False, 'todoRejected': False}],
        })
    rounds.insert(1, {'toolName': 'read_files', 'toolCallId': 'read'})
    projected = todo.compact_todo_rounds_for_replay(rounds)
    assert [r['toolCallId'] for r in projected] == ['read', 't2']
    assert len(rounds) == 4


def test_replay_never_treats_failed_todo_execution_as_effective_state():
    rounds = [
        {
            'toolName': 'todo_write', 'toolCallId': 'accepted',
            'status': 'done',
            'results': [{'todoNoop': False, 'todoRejected': False}],
        },
        {
            'toolName': 'todo_write', 'toolCallId': 'failed',
            'status': 'error',
            'results': [{'type': 'error', 'content': 'handler crashed'}],
        },
    ]

    projected = todo.compact_todo_rounds_for_replay(rounds)

    assert [row['toolCallId'] for row in projected] == [
        'accepted', 'failed',
    ]
    assert [row['toolCallId'] for row in rounds] == ['accepted', 'failed']


def test_replay_recognizes_legacy_error_result_without_round_status():
    rounds = [
        {
            'toolName': 'todo_write', 'toolCallId': 'accepted',
            'results': [{'todoNoop': False, 'todoRejected': False}],
        },
        {
            'toolName': 'todo_write', 'toolCallId': 'legacy-failed',
            'results': [{'type': 'error', 'content': 'handler crashed'}],
        },
    ]

    projected = todo.compact_todo_rounds_for_replay(rounds)

    assert [row['toolCallId'] for row in projected] == [
        'accepted', 'legacy-failed',
    ]


def test_replay_drops_old_failure_after_a_new_accepted_revision():
    rounds = [
        {
            'toolName': 'todo_write', 'toolCallId': 'accepted-old',
            'status': 'done',
            'results': [{'todoNoop': False, 'todoRejected': False}],
        },
        {
            'toolName': 'todo_write', 'toolCallId': 'failed',
            'status': 'error',
            'results': [{'type': 'error', 'content': 'handler crashed'}],
        },
        {
            'toolName': 'todo_write', 'toolCallId': 'accepted-new',
            'status': 'done',
            'results': [{'todoNoop': False, 'todoRejected': False}],
        },
    ]

    projected = todo.compact_todo_rounds_for_replay(rounds)

    assert [row['toolCallId'] for row in projected] == ['accepted-new']


def test_wire_replay_contains_only_latest_effective_todo_revision():
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages

    rounds = []
    for n in range(3):
        rounds.append({
            'roundNum': n + 1, 'llmRound': n,
            'toolName': 'todo_write', 'toolCallId': f'todo-{n}',
            'toolArgs': '{"todos": []}',
            'toolContent': f'Checklist updated revision {n}',
            'status': 'done',
            'results': [{'todoNoop': False, 'todoRejected': False}],
        })
    wire = _reconstruct_tool_call_messages(rounds)
    calls = [tc for msg in wire if msg.get('role') == 'assistant'
             for tc in msg.get('tool_calls') or []]
    results = [msg for msg in wire if msg.get('role') == 'tool']
    assert [tc['id'] for tc in calls] == ['todo-2']
    assert [msg['tool_call_id'] for msg in results] == ['todo-2']


def test_continue_resume_restores_authoritative_checklist_stack():
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state

    root = todo.apply_todo_operation(None, {'todos': [
        {'id': 'parent', 'content': 'Parent', 'status': 'in_progress'},
    ]})
    child = todo.apply_todo_operation(root['state'], {
        'operation': 'push', 'parent_todo_id': 'parent',
        'todos': [{'id': 'child', 'content': 'Child', 'status': 'in_progress'}],
    })
    task = {'convId': 'c', 'content': '', 'content_lock': threading.Lock()}
    apply_resume_state(
        task=task,
        cfg={'checkpointTodoState': child['state']},
        messages=[], model='test-model', tid='testtask',
    )
    assert len(task['_todoState']['stack']) == 2
    assert task['_todos'][0]['id'] == 'child'


def test_continue_resume_rejects_malformed_authority_before_any_mutation():
    from lib.tasks_pkg.orchestrator._resume_state import (
        ContinueResumeStateProtocolError,
        apply_resume_state,
    )

    malformed_todo = {
        'version': todo.TODO_STATE_VERSION,
        'stack': [{
            'checklist_id': 'root',
            'revision': 'not-an-integer',
            'parent_todo_id': None,
            'todos': [{
                'id': 'one', 'content': 'Keep this work visible',
                'status': 'in_progress',
            }],
        }],
        'history': [],
        'update_count': 1,
        'root_completed': False,
    }
    invalid_fields = [
        ('contentPrefix', False),
        ('resumePrefill', []),
        ('checkpointToolRounds', {'0': 'not-a-list'}),
        ('checkpointToolRounds', [{
            'toolCallId': 'orphan', 'toolName': 'run_command',
            'toolArgs': '{}',
        }]),
        ('checkpointTodoState', malformed_todo),
        ('checkpointUsage', []),
        ('checkpointApiRounds', {}),
        ('checkpointModifiedFiles', True),
        ('checkpointModifiedFileList', {}),
    ]

    for field, value in invalid_fields:
        task = {
            'convId': 'atomic',
            'content': 'original',
            'content_lock': threading.Lock(),
            '_sentinel': object(),
        }
        original_task = dict(task)
        messages = [{'role': 'user', 'content': 'continue'}]
        original_messages = [dict(message) for message in messages]
        config = {
            'contentPrefix': 'must-not-apply',
            'resumePrefill': 'must-not-append',
            field: value,
        }

        with pytest.raises(ContinueResumeStateProtocolError) as raised:
            apply_resume_state(
                task=task,
                cfg=config,
                messages=messages,
                model='gpt-4o',
                tid='atomic',
            )

        assert raised.value.status_code == 422
        assert task == original_task
        assert messages == original_messages


def test_continue_resume_accepts_oversized_checkpoint_without_size_ceiling():
    # Beyond the retired 4096-round / 8 MB resume caps: snapshots are folded
    # by working-set compaction downstream, never rejected at this boundary.
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state

    rounds = [{
        'toolCallId': f'call-{index}', 'toolName': 'run_command',
        'toolArgs': '{}', 'toolContent': 'y' * 2048, 'status': 'done',
        'llmRound': index,
    } for index in range(4097)]

    prepared = prepare_resume_state({'checkpointToolRounds': rounds})

    assert len(prepared.checkpoint_tool_rounds) == 4097
    assert len(prepared.checkpoint_messages) > 0


def test_continue_resume_preserves_equal_tool_occurrences_by_position():
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state

    duplicate = {
        'toolCallId': 'provider-recycled-id',
        'toolName': 'run_command',
        'toolArgs': '{"command":"pwd"}',
        'toolContent': 'same receipt',
        'status': 'done',
    }
    task = {'convId': 'c', 'content': '', 'content_lock': threading.Lock()}
    apply_resume_state(
        task=task,
        cfg={'checkpointToolRounds': [dict(duplicate), dict(duplicate)]},
        messages=[], model='test-model', tid='testtask',
    )

    assert len(task['_checkpointToolRounds']) == 2
    assert task['_checkpointToolRounds'][0] == duplicate
    assert task['_checkpointToolRounds'][1] == duplicate
    assert task['_checkpointToolRounds'][0] is not task['_checkpointToolRounds'][1]


def test_continue_resume_tolerates_display_rows_in_checkpoint_rounds():
    """Identity-free display carriers never block, never reach the wire."""
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state

    rounds = [
        {'toolName': 'execute_tools', 'toolArgs': {'calls': []},
         'status': 'done'},
        {'toolCallId': 'call-1', 'toolName': 'run_command',
         'toolArgs': '{}', 'toolContent': 'ok', 'status': 'done',
         'llmRound': 0},
    ]

    prepared = prepare_resume_state({'checkpointToolRounds': rounds})

    assert len(prepared.checkpoint_tool_rounds) == 2
    # Only the execution receipt becomes wire messages; the display carrier
    # is transparent to protocol reconstruction.
    assert [message['role'] for message in prepared.checkpoint_messages] == [
        'assistant', 'tool',
    ]


def test_checkpoint_resume_replays_the_same_bytes_as_next_turn_history():
    """Checkpoint facts must be on-wire now and prefix-stable next turn."""
    import json

    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages,
    )
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state

    checkpoint = [{
        'roundNum': 1,
        'llmRound': 0,
        'attemptId': 'attempt-before-restart',
        'taskId': 'task-before-restart',
        'toolCallId': 'call-before-restart',
        'toolName': 'search_tools',
        'toolArgs': '{"query":"durable fact"}',
        'toolContent': 'the durable answer',
        'assistantContent': 'I will recover the fact.',
        'status': 'done',
    }]
    messages = [{'role': 'user', 'content': 'finish the interrupted task'}]
    task = {'convId': 'c', 'content': '', 'content_lock': threading.Lock()}

    apply_resume_state(
        task=task,
        cfg={'checkpointToolRounds': checkpoint},
        messages=messages,
        model='test-model',
        tid='resume',
    )

    reconstructed_next_turn = _reconstruct_tool_call_messages(checkpoint)
    assert reconstructed_next_turn
    resumed_suffix = messages[1:]
    canonical_bytes = lambda value: json.dumps(
        value, ensure_ascii=False, separators=(',', ':'), sort_keys=False,
    ).encode('utf-8')
    assert canonical_bytes(resumed_suffix) == canonical_bytes(
        reconstructed_next_turn)
    assert task['_checkpointToolRounds'] == checkpoint
    assert task['_checkpointToolRounds'] is not checkpoint


def test_live_task_metadata_exposes_todo_state_and_blocked_verdict():
    from lib.tasks_pkg.manager import build_result_meta

    state = todo.apply_todo_operation(None, {'todos': [
        {'id': 'blocked', 'content': 'Need credential', 'status': 'blocked'},
    ]})['state']
    blocked = {'reason': 'todo_items_blocked', 'incomplete': 1}
    meta = build_result_meta({'_todoState': state, '_todo_blocked': blocked})
    assert meta['todoState']['stack'][0]['todos'][0]['status'] == 'blocked'
    assert meta['todoBlocked'] == blocked


def test_compaction_attachment_carries_child_stack_breadcrumb_and_deduplicates():
    from lib.tasks_pkg.attachments import (compute_turn_attachments,
                                           inject_attachments)

    root = todo.apply_todo_operation(None, {'todos': [
        {'id': 'parent', 'content': 'Build feature', 'status': 'in_progress'},
    ]})
    child = todo.apply_todo_operation(root['state'], {
        'operation': 'push', 'parent_todo_id': 'parent',
        'todos': [{'id': 'child', 'content': 'Implement detail', 'status': 'in_progress'}],
    })
    task = {'_todoState': child['state'], '_todos': child['todos']}
    messages = [{'role': 'user', 'content': 'continue'}]
    attachments = compute_turn_attachments(
        messages, task=task, round_num=4, conv_id='nested-todo')
    assert len(attachments) == 1
    assert 'Checklist path: Root task > Build feature' in attachments[0]
    assert 'reuse each id exactly in later sync calls' in attachments[0]
    assert 'id="child"' in attachments[0]
    inject_attachments(messages, attachments)
    assert compute_turn_attachments(
        messages, task=task, round_num=5, conv_id='nested-todo') == []


def test_headless_result_is_not_ok_when_todos_forced_incomplete_finish():
    from lib.tasks_pkg.entry import ChatResult

    result = ChatResult(status='done', finish_reason='incomplete')
    assert result.ok is False


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
    # The completed assistant response must precede the reminder. Otherwise
    # the next LLM call loses its own immediately-prior reasoning and receives
    # a user/user adjacency.
    assert [item['role'] for item in messages] == [
        'user', 'assistant', 'user']
    assert messages[-2]['content'] == 'Final answer here.'
    # A reminder user-message was injected carrying the incomplete item.
    assert messages[-1]['role'] == 'user'
    assert messages[-1]['_isMeta'] is True
    assert 'TODO CONTINUATION' in messages[-1]['content']
    assert 'unfinished item' in messages[-1]['content']


def test_enforcer_snapshots_vetoed_answer_as_continuation_prose(monkeypatch):
    """★ The vetoed final answer is recorded on the task at nudge time.

    Regression pin for the 2026-09-02 lost-report incident: the model
    stopped with a long report while checklist items were incomplete; the
    enforcer re-drove the loop; the next tool round's _discard_pretool_prose
    zeroed task['content'] — and the report vanished from the turn.
    """
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    report = '全部完成。矩阵已恢复并验证通过，硬刷新即可用。'
    task = _task(_attemptId='attempt-1', _todos=[
        {'id': '2', 'content': 'unfinished item', 'status': 'pending'},
    ])
    messages = [{'role': 'user', 'content': 'do the thing'}]
    decision = _run(task, messages, content=report)
    assert decision['action'] == 'continue'
    entries = task.get('_continuation_prose')
    assert isinstance(entries, list) and len(entries) == 1
    assert entries[0]['content'] == report
    assert entries[0]['llmRound'] == 2          # _run uses round_num=2
    assert entries[0]['attemptId'] == 'attempt-1'


def test_continuation_prose_survives_pretool_discard_in_assembly():
    """★ Incident replay at the assembly boundary.

    Round 97 stops prose-only and is vetoed (snapshot taken); round 98
    streams thinking, then issues a tool call — the prelude stamps the batch
    and _discard_pretool_prose zeroes the accumulators. The assembled
    segments must still show the vetoed answer BETWEEN the two rounds, the
    deliverable projection must stay narration-free, and the replay rounds
    view must not re-attach it to the next round (the wire row appended at
    nudge time already carries it).
    """
    from lib.tasks_pkg.segments import (
        _rounds_view_from_segments, assemble_segments, derive_content,
        record_continuation_prose,
    )

    report = '全部完成。矩阵已恢复并验证通过，硬刷新即可用。'
    task = _task()
    record_continuation_prose(task, llm_round=97, content=report,
                              thinking='rep-think')
    task['toolRounds'] = [{
        'toolCallId': 'call_98', 'toolName': 'todo_write',
        'toolArgs': '{}', 'toolContent': 'ok', 'status': 'done',
        'llmRound': 98, 'thinking': 'r98-think',
    }]
    task['content'] = ''       # post-_discard_pretool_prose
    task['thinking'] = ''
    segments = assemble_segments(task)
    assert [(s['type'], s.get('llmRound')) for s in segments] == [
        ('thinking', 97), ('text', 97), ('thinking', 98), ('tool_use', 98)]
    assert segments[1]['text'] == report
    assert segments[1]['deliverable'] is False
    assert segments[1]['blockId'] == 'text:continuation-0'
    assert derive_content(segments) == ''
    rounds_view = _rounds_view_from_segments(segments)
    assert len(rounds_view) == 1
    assert rounds_view[0].get('assistantContent') is None
    assert rounds_view[0].get('thinking') == 'r98-think'

    # The terminal answer then lands AFTER the preserved record.
    task['content'] = 'final closer'
    task['thinking'] = 'final think'
    segments = assemble_segments(task)
    assert [s.get('text') for s in segments if s['type'] == 'text'] == [
        report, 'final closer']
    assert segments[-1]['deliverable'] is True
    assert segments[-1]['terminal'] is True
    assert derive_content(segments) == 'final closer'


def test_second_nudge_does_not_duplicate_first_round_prose(monkeypatch):
    """Two consecutive vetoes record each round ONCE (accumulator reset-free).

    task['content'] is NOT zeroed between continuations, so snapshotting the
    accumulator would duplicate round N's prose into round N+1's entry; the
    per-round source keeps entries disjoint and ordered.
    """
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    task = _task(_todos=[
        {'id': '2', 'content': 'unfinished item', 'status': 'pending'},
    ])
    messages = [{'role': 'user', 'content': 'x'}]
    first = analyse_stream_result(
        assistant_msg=_stop_msg('answer one'), last_finish_reason='stop',
        task=task, tid='testtask', model='test-model',
        round_num=2, _premature_retry_count=0, messages=messages,
        usage=_clean_usage())
    assert first['action'] == 'continue'
    second = analyse_stream_result(
        assistant_msg=_stop_msg('answer two'), last_finish_reason='stop',
        task=task, tid='testtask', model='test-model',
        round_num=3, _premature_retry_count=0, messages=messages,
        usage=_clean_usage())
    assert second['action'] == 'continue'
    entries = task['_continuation_prose']
    assert [e['content'] for e in entries] == ['answer one', 'answer two']
    assert [e['llmRound'] for e in entries] == [2, 3]


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
    """★ Runaway guard: after the cap, stop re-driving and return incomplete
    (a model that won't finish or update the list can't loop forever)."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    task = _task(_todo_continuation_count=3,  # cap already reached
                 _todos=[{'id': '2', 'content': 'still pending', 'status': 'pending'}])
    messages = [{'role': 'user', 'content': 'x'}]
    decision = _run(task, messages)
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'incomplete'
    assert task['_todo_blocked']['incomplete'] == 1
    assert len(messages) == 1  # no further injection


def test_enforcer_does_not_waste_nudges_on_explicit_blocker(monkeypatch):
    """A fully blocked checklist settles incomplete immediately."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '3')
    task = _task(_todos=[
        {'id': 'wait', 'content': 'Await unavailable credential',
         'status': 'blocked'},
    ])
    messages = [{'role': 'user', 'content': 'x'}]
    decision = _run(task, messages)
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'incomplete'
    assert task['_todo_blocked']['reason'] == 'todo_items_blocked'
    assert '_todo_continuation_count' not in task
    assert len(messages) == 1


def test_enforcer_disabled_by_env(monkeypatch):
    """TOFU_TODO_CONTINUATION_MAX=0 disables nudges, not completion honesty."""
    monkeypatch.setenv('TOFU_TODO_CONTINUATION_MAX', '0')
    task = _task(_todos=[{'id': '2', 'content': 'pending', 'status': 'pending'}])
    messages = [{'role': 'user', 'content': 'x'}]
    decision = _run(task, messages)
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'incomplete'


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
    import lib.tasks_pkg.stream_handler._budget as sh
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
    import lib.tasks_pkg.compaction._layer2._compact as l2

    # Deterministic fake summary so no LLM is called.
    monkeypatch.setattr(l2, '_generate_query_aware_summary',
                        lambda *a, **k: '### summary of earlier work')
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: None)

    todos = [
        {'id': '1', 'content': 'first step', 'status': 'completed'},
        {'id': '2', 'content': 'second step', 'status': 'in_progress'},
        {'id': '3', 'content': 'third step', 'status': 'pending'},
    ]
    task = {'convId': 'c', 'id': 't', '_userId': 1, '_todos': todos}

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
