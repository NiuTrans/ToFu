"""Display-only interventions retain identity across checkpoints and reloads."""
import json
import random

import pytest

from lib.tasks_pkg.segments import (
    assemble_segments, merge_injected_note_segments, record_injected_note,
    segments_to_json, reconstruct_tool_messages_from_segments,
    tool_history_from_segments,
)
from lib.tasks_pkg.segments._types import NOTE_KINDS
from lib.tool_results.tool_round_identity import execution_identity
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


def _linear_note_placement(segments, entries):
    """The anchor rule as a plain scan of the whole list for every entry."""
    segments = list(segments)
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        kind, text = entry.get('kind'), entry.get('text')
        text = text if isinstance(text, str) else ''
        if not isinstance(kind, str) or not text.strip():
            continue
        lr = entry.get('llmRound')
        lr = lr if isinstance(lr, int) and not isinstance(lr, bool) else None
        identity = {field: entry[field] for field in ('attemptId', 'taskId')
                    if isinstance(entry.get(field), str) and entry[field]}
        note = {'type': 'system_note', 'text': text, 'llmRound': lr, **identity,
                'blockId': entry.get('blockId') or f'system-note:{kind}-{position}'}
        existing = next((i for i, current in enumerate(segments)
                         if current.get('type') == 'system_note'
                         and current.get('blockId') == note['blockId']
                         and execution_identity(current) == execution_identity(note)),
                        None)
        if existing is not None:
            segments[existing] = note
            continue
        index = len(segments)
        for i, current in enumerate(segments):
            if any(current.get(field) and value != current[field]
                   for field, value in identity.items()):
                continue
            if current.get('terminal'):
                index = i
                break
            round_ = current.get('llmRound')
            if (lr is None or not isinstance(round_, int)
                    or isinstance(round_, bool) or round_ < lr):
                continue
            if round_ == lr and (
                    current.get('type') == 'system_note'
                    or str(current.get('blockId') or '').startswith(
                        ('thinking:continuation-', 'text:continuation-'))):
                continue
            index = i
            break
        segments.insert(index, note)
    return segments


def _random_turn(rng):
    rounds = [None, True, 'legacy', 0, 1, 2, 3, 4, 5]
    attempts, tasks = [None, 'a1', 'a2'], [None, 't1', 't2']
    segments = []
    for index in range(rng.randrange(24)):
        if rng.random() < 0.2:
            segment = {'type': 'system_note', 'blockId': f'note:{rng.randrange(4)}',
                       'text': f'old-{index}', 'llmRound': rng.choice(rounds)}
        else:
            kind = rng.choice(['thinking', 'text', 'tool_use'])
            continuation = kind != 'tool_use' and rng.random() < 0.25
            segment = {'type': kind, 'text': f'block-{index}',
                       'llmRound': rng.choice(rounds),
                       'blockId': f'{kind}:{"continuation-" if continuation else ""}{index}'}
            if rng.random() < 0.1:
                segment['terminal'] = True
        for field, pool in (('attemptId', attempts), ('taskId', tasks)):
            if value := rng.choice(pool):
                segment[field] = value
        segments.append(segment)
    entries = []
    for position in range(rng.randrange(10)):
        if rng.random() < 0.1:
            entries.append(rng.choice([None, 'note', {'kind': 'system-intervention', 'text': ' '}]))
            continue
        entry = {'kind': rng.choice(sorted(NOTE_KINDS)), 'text': f'note-{position}',
                 'llmRound': rng.choice(rounds + [6, 7])}
        if rng.random() < 0.7:
            entry['blockId'] = f'note:{rng.randrange(4)}'
        for field, pool in (('attemptId', attempts + ['', 7]), ('taskId', tasks + [''])):
            if (value := rng.choice(pool)) is not None:
                entry[field] = value
        entries.append(entry)
    return segments, entries


def test_note_placement_matches_the_linear_anchor_rule():
    def view(values):
        return [(s.get('type'), s.get('blockId'), s.get('text'), s.get('llmRound'),
                 s.get('attemptId'), s.get('taskId')) for s in values]

    rng = random.Random(20261104)
    for _ in range(3000):
        segments, entries = _random_turn(rng)
        before = list(segments)
        merged = merge_injected_note_segments(segments, entries)
        assert view(merged) == view(_linear_note_placement(segments, entries))
        assert segments == before
        ordinary = [s for s in segments if s['type'] != 'system_note']
        assert all(a is b for a, b in zip(
            [s for s in merged if s['type'] != 'system_note'], ordinary, strict=True))
