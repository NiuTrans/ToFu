"""Todo snapshot dispatch, durable replay and detached Continue recovery.

Progress survives compaction without reviving retired nesting or continuation
rules. Independent stream/settlement evidence still determines task outcome.
"""

import json
import threading
import time

import pytest

import lib.tools.todo as todo

pytestmark = pytest.mark.unit


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
    assert 'updated' in text.lower()
    assert all(set(item) == {'content', 'status'} for item in todos)
    assert 'id=' not in text and 'sync' not in text


def test_malformed_snapshot_is_rejected_instead_of_silently_repaired():
    result = todo.apply_todo_operation(None, {'todos': [
        {'content': 'Work', 'status': 'bogus'}, 'not-a-dict',
    ]})
    assert result['rejected']
    assert result['todos'] == []


def test_apply_todo_write_empty_clears():
    todos, text = todo.apply_todo_write({'todos': []})
    assert todos == []
    assert 'updated' in text.lower() or 'unchanged' in text.lower()


def test_incomplete_todos_filter():
    items = [
        {'id': '1', 'content': 'a', 'status': 'completed'},
        {'id': '2', 'content': 'b', 'status': 'pending'},
        {'id': '3', 'content': 'c', 'status': 'in_progress'},
    ]
    inc = todo.incomplete_todos(items)
    assert {t['id'] for t in inc} == {'2', '3'}
    assert todo.incomplete_todos([{'id': '1', 'content': 'a', 'status': 'completed'}]) == []


def test_snapshot_updates_have_no_completed_or_missing_item_preconditions():
    first = todo.apply_todo_operation(None, {'todos': [
        {'content': 'Old scope', 'status': 'pending'},
    ]})
    replacement = todo.apply_todo_operation(first['state'], {'todos': [
        {'content': 'New scope', 'status': 'completed'},
    ]})
    reopened = todo.apply_todo_operation(replacement['state'], {'todos': [
        {'content': 'New scope', 'status': 'pending'},
    ]})
    assert not replacement['rejected'] and not reopened['rejected']
    assert reopened['state']['update_count'] == 3
    duplicate = todo.apply_todo_operation(reopened['state'], {'todos': reopened['todos']})
    assert duplicate['no_op'] and duplicate['state'] == reopened['state']


def test_stale_push_does_not_silently_replace_parent_work():
    root = todo.apply_todo_operation(None, {'todos': [
        {'content': 'Build feature', 'status': 'in_progress'},
        {'content': 'Verify feature', 'status': 'pending'},
    ]})
    rejected = todo.apply_todo_operation(root['state'], {
        'operation': 'push', 'parent_todo_id': 'old-parent-id',
        'todos': [{'content': 'One child detail', 'status': 'pending'}],
    })
    assert rejected['rejected']
    assert rejected['state'] == root['state']


def test_same_round_todo_calls_execute_in_model_order(monkeypatch):
    """Same-batch full snapshots apply in model order, including Manual mode."""
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
        'explanation': 'Refined implementation plan',
        'todos': [{'content': 'Implement', 'status': 'in_progress'}],
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
    assert task['_todoState']['todos'] == child_args['todos']
    assert task['_todoState']['update_count'] == 2
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


def test_wire_replay_preserves_every_sent_todo_revision():
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
    assert [tc['id'] for tc in calls] == ['todo-0', 'todo-1', 'todo-2']
    assert [msg['tool_call_id'] for msg in results] == ['todo-0', 'todo-1', 'todo-2']


def test_continue_resume_restores_flat_snapshot_detached():
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    state = {'version': 3, 'todos': [{'content': 'Remaining work', 'status': 'pending'}],
             'update_count': 2, 'explanation': 'Scope changed'}
    task = {}
    apply_resume_state(task=task, cfg={'checkpointTodoState': state},
                       messages=[], model='test-model', tid='test')
    assert task['_todoState'] == state
    state['todos'].clear()
    assert task['_todos'][0]['content'] == 'Remaining work'


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


def test_live_task_metadata_exposes_snapshot_without_retired_blocked_verdict():
    from lib.tasks_pkg.manager import build_result_meta
    state = {'version': 3, 'todos': [], 'update_count': 2, 'explanation': 'Cleared'}
    blocked = {'reason': 'todo_items_blocked', 'incomplete': 1}
    meta = build_result_meta({'_todoState': state, '_todo_blocked': blocked})
    assert meta['todoState'] == state
    assert 'todoBlocked' not in meta


def test_compaction_attachment_preserves_flat_snapshot_and_deduplicates():
    from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments
    state = {'version': 3, 'todos': [{'content': 'Implement detail', 'status': 'pending'}],
             'update_count': 2, 'explanation': 'New evidence'}
    task = {'_todoState': state}
    messages = [{'role': 'user', 'content': 'continue'}]
    attachments = compute_turn_attachments(messages, task=task, round_num=4, conv_id='todo')
    assert len(attachments) == 1
    assert 'Implement detail' in attachments[0] and 'New evidence' in attachments[0]
    assert 'id=' not in attachments[0] and 'revision' not in attachments[0]
    inject_attachments(messages, attachments)
    assert compute_turn_attachments(messages, task=task, round_num=5, conv_id='todo') == []


def test_headless_result_is_not_ok_for_unrelated_incomplete_finish():
    from lib.tasks_pkg.entry import ChatResult

    result = ChatResult(status='done', finish_reason='incomplete')
    assert result.ok is False


# ══════════════════════════════════════════════════════════
#  2. Preserved intermediate prose
# ══════════════════════════════════════════════════════════


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
