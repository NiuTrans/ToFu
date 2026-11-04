"""Reprojecting known notes preserves identity without re-searching old rounds."""
import pytest

from lib.tasks_pkg.segments._assemble import merge_injected_note_segments

pytestmark = pytest.mark.unit


def _note(block, round_number, text, **scope):
    return dict(blockId=block, llmRound=round_number, text=text,
                kind='system-intervention', **scope)


def test_known_note_updates_do_not_search_the_tool_history():
    class ObservedTool(dict):
        round_reads = 0

        def get(self, key, default=None):
            if key == 'llmRound':
                self.round_reads += 1
            return super().get(key, default)

    tools = [ObservedTool(type='tool_use', blockId=f'tool:{i}', llmRound=i)
             for i in range(64)]
    notes = [_note(f'note:{i}', i, f'note {i}') for i in range(64)]
    original = merge_injected_note_segments(tools, notes)
    for tool in tools:
        tool.round_reads = 0
    updated = merge_injected_note_segments(original, [
        {**note, 'text': f'updated {i}'} for i, note in enumerate(notes)])
    assert [row['blockId'] for row in updated] == [row['blockId'] for row in original]
    assert [row['text'] for row in updated if row['type'] == 'system_note'] == [
        f'updated {i}' for i in range(64)]
    assert all(tool.round_reads == 0 for tool in tools)
    assert original[0]['text'] == 'note 0'


def test_insert_update_and_repeated_identity_keep_original_positions_and_scope():
    tools = [dict(type='tool_use', blockId=f't:{owner}', llmRound=3,
                  attemptId=owner, taskId=f'task:{owner}') for owner in ('a', 'b')]
    notes = [
        _note('same', 3, 'a first', attemptId='a', taskId='task:a'),
        _note('same', 3, 'b first', attemptId='b', taskId='task:b'),
        _note('earlier', 2, 'before a', attemptId='a', taskId='task:a'),
        _note('same', 9, 'a replaced', attemptId='a', taskId='task:a'),
        _note('same', 1, 'b replaced', attemptId='b', taskId='task:b'),
    ]
    result = merge_injected_note_segments(tools, notes)
    assert [row.get('text', row['blockId']) for row in result] == [
        'before a', 'a replaced', 't:a', 'b replaced', 't:b']
    assert len(tools) == 2


def test_duplicate_existing_identity_updates_only_its_first_occurrence():
    original = merge_injected_note_segments([], [_note('n', 0, 'old')])
    original += [dict(original[0])]
    result = merge_injected_note_segments(original, [_note('n', 1, 'new')])
    assert [row['text'] for row in result] == ['new', 'old']


def test_unchanged_note_keeps_immutable_row_for_downstream_projection_reuse():
    from lib.conversation_sync.settled_rows import settle
    note = _note('n', 0, 'unchanged')
    original = settle(merge_injected_note_segments([], [note]))
    result = merge_injected_note_segments(original, [note])
    assert result == original
    assert result[0] is original[0]


def test_producer_notes_share_frozen_rows_across_capture_and_public_projection():
    from lib.conversation_sync.settled_rows import SettledRowMutation, settle
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD, record_injected_note
    from lib.turn_lifecycle import capture_task_projection_snapshot
    from lib.turn_projection_segments import projection_with_stable_segments
    task = {'id': 'owner', '_attemptId': 'attempt', 'config': {}}
    note = record_injected_note(task, llm_round=0, kind='system-intervention', text='record')
    first, _ = capture_task_projection_snapshot(task, {'type': 'phase'})
    second, _ = capture_task_projection_snapshot(task, {'type': 'phase'})
    assert first[INJECTED_NOTES_FIELD][0] is note
    assert second[INJECTED_NOTES_FIELD][0] is note
    with pytest.raises(SettledRowMutation):
        note['text'] = 'changed after publication'
    segments = settle(merge_injected_note_segments([], [note]))
    projected = projection_with_stable_segments({'segments': segments}, injected_notes=[note])
    assert projected['segments'][0] is segments[0]


def test_capture_reconciles_note_rollback_replacement_and_legacy_mutable_rows():
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD, record_injected_note
    from lib.turn_lifecycle import capture_task_projection_snapshot
    task = {'config': {}}
    original = record_injected_note(task, llm_round=0, kind='system-intervention', text='old')
    first, _ = capture_task_projection_snapshot(task, {'type': 'phase'})
    task[INJECTED_NOTES_FIELD].clear()
    replacement = record_injected_note(task, llm_round=0, kind='system-intervention', text='new')
    legacy = _note('legacy', 1, 'legacy')
    task[INJECTED_NOTES_FIELD].append(legacy)
    second, _ = capture_task_projection_snapshot(task, {'type': 'phase'})
    legacy['text'] = 'later'
    third, _ = capture_task_projection_snapshot(task, {'type': 'phase'})
    assert first[INJECTED_NOTES_FIELD][0] == original
    assert second[INJECTED_NOTES_FIELD][0] is replacement
    assert second[INJECTED_NOTES_FIELD][1]['text'] == 'legacy'
    assert third[INJECTED_NOTES_FIELD][1]['text'] == 'later'


def test_pending_structural_captures_share_history_instead_of_multiplying_rows():
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD, record_injected_note
    from lib.turn_lifecycle import capture_task_projection_snapshot
    task = {'config': {}}
    for index in range(128):
        record_injected_note(task, llm_round=index, kind='system-intervention', text=f'note {index}')
    sources = [capture_task_projection_snapshot(task, {'type': 'phase'})[0] for _ in range(32)]
    assert len({id(row) for source in sources for row in source[INJECTED_NOTES_FIELD]}) == 128
    assert len({id(source[INJECTED_NOTES_FIELD]) for source in sources}) == 32


@pytest.mark.parametrize(('pending', 'transient'), [(False, False), (True, False), (False, True)])
def test_terminal_notes_release_only_after_durable_ack(pending, transient):
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD, record_injected_note
    from lib.turn_lifecycle import capture_task_projection_snapshot
    task = {'id': 'terminal', 'convId': 'conversation', '_attemptId': 'attempt',
            '_turnId': 'turn', 'status': 'done', 'config': {},
            '_terminalPersistencePending': pending, '_transientRuntime': transient}
    if transient:
        task.pop('_attemptId')
        task.pop('_turnId')
    note = record_injected_note(task, llm_round=0, kind='system-intervention', text='durable fact')
    source, _ = capture_task_projection_snapshot(task, {'type': 'phase'})
    _release_heavy_task_state(task)
    if pending or transient:
        assert task[INJECTED_NOTES_FIELD] == [note]
        assert task['_projectionCaptureRows']
    else:
        assert task[INJECTED_NOTES_FIELD] is None
        assert task['_projectionCaptureRows'] is None
    assert source[INJECTED_NOTES_FIELD] == [note], 'detached accepted frames retain their facts'
