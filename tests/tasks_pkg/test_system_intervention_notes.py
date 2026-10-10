"""Display-only interventions retain identity across checkpoints and reloads."""
import json
import pytest

from lib.tasks_pkg.segments import (
    assemble_segments, record_injected_note, segments_to_json,
    reconstruct_tool_messages_from_segments, tool_history_from_segments,
)
from lib.turn_projection_segments import projection_with_stable_segments

pytestmark = pytest.mark.unit


def test_record_is_bounded_idempotent_and_not_provider_input():
    task = {'id': 'task', '_attemptId': 'a', 'content': 'answer'}
    args = dict(llm_round=0, kind='system-intervention', action='stop',
                reason='r' * 200, text='<script>attack()</script>' + 'x' * 20000)
    note = record_injected_note(task, **args)
    assert record_injected_note(task, **args) is note
    assert len(note['text']) == 16384
    assert len(note['reason']) == 160
    task['_attemptId'] = 'b'
    assert record_injected_note(task, **args)['blockId'] != note['blockId']
    segments = assemble_segments(task)
    assert len([s for s in segments if s['type'] == 'system_note']) == 2
    assert 'attack' not in json.dumps(reconstruct_tool_messages_from_segments(segments))
    assert 'attack' not in json.dumps(tool_history_from_segments(segments))
    assert 'messages' not in task


@pytest.mark.parametrize('status', ['running', 'completed'])
def test_note_reload_and_repair_preserves_order(status):
    task = {'content': 'answer'}
    for action in ['prompt', 'tool-policy', 'stop']:
        record_injected_note(task, llm_round=0, kind='system-intervention',
                             text=action, action=action)
    segments = segments_to_json(assemble_segments(task))
    notes = [s for s in segments if s['type'] == 'system_note']
    projection = {'content': 'answer', 'segments': segments + [notes[0]],
                  'toolRounds': [{'toolCallId': 'later', 'toolName': 'test',
                                  'llmRound': 1, 'status': 'done', 'toolContent': 'ok'}]}
    normalized = projection_with_stable_segments(projection, status=status)
    reload = projection_with_stable_segments(json.loads(json.dumps(normalized)), status=status)
    carried = [s for s in reload['segments'] if s['type'] == 'system_note']
    assert carried == notes
    assert reload == normalized


def test_identity_free_historical_notes_do_not_overwrite_each_other():
    raw = {'segments': [
        {'type': 'system_note', 'noteKind': 'system-intervention', 'text': text}
        for text in ['first', 'second']
    ]}
    projection = projection_with_stable_segments(raw)
    notes = [s for s in projection['segments'] if s['type'] == 'system_note']
    assert [n['text'] for n in notes] == ['first', 'second']
    assert len({n['blockId'] for n in notes}) == 2


def test_unknown_persisted_kind_remains_safe_note_on_invalid_sibling_repair():
    note = {'type': 'system_note', 'noteKind': 'future-kind', 'blockId': 'note:1',
            'text': '<img src=x onerror=attack()>', 'action': 'stop'}
    projection = projection_with_stable_segments({'segments': [note, {'type': 'bad'}]})
    carried = next(s for s in projection['segments'] if s['type'] == 'system_note')
    assert carried['noteKind'] == 'system-intervention'
    assert carried['text'] == note['text']
    assert carried['action'] == 'stop'
