"""Every model-visible context commit leaves a display-only timeline note.

Invariant under test: when the composer appends a round-scoped block to the
wire, ``append_context_blocks`` records a ``context-injection`` note; when a
conversation-level static block commits, ``compose_context`` records one
``session-context`` note. Reuse (content-addressed, ``appended=False``) emits
nothing, so steady-state turns stay quiet and only genuine commits surface.
"""
import pytest

from lib.tasks_pkg.context_composer import (
    ComposeRequest, append_context_blocks, compose_context, context_block,
)
from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD

pytestmark = pytest.mark.unit


def _task(**extra):
    return {'id': 'task', '_attemptId': 'a', 'toolRounds': [], **extra}


def _round_block(content):
    return context_block('modified_files', content)


def test_round_block_commit_emits_context_injection_note():
    task = _task()
    messages = []
    req = ComposeRequest(task=task, model='m')
    append_context_blocks(messages, [_round_block('## Files\n- a.py')], req)
    notes = task.get(INJECTED_NOTES_FIELD) or []
    assert len(notes) == 1
    assert notes[0]['kind'] == 'context-injection'
    assert notes[0]['reason'] == 'modified_files'
    assert 'a.py' in notes[0]['text']
    assert any(m.get('_contextComposer') for m in messages)


def test_reuse_emits_nothing_and_change_emits_once_per_commit():
    task = _task()
    messages = []
    req = ComposeRequest(task=task, model='m')
    append_context_blocks(messages, [_round_block('## Files\n- a.py')], req)
    # Identical bytes already present → reuse → no new note.
    append_context_blocks(messages, [_round_block('## Files\n- a.py')], req)
    assert len(task.get(INJECTED_NOTES_FIELD) or []) == 1
    # Changed bytes are a fresh commit → exactly one more note.
    append_context_blocks(messages, [_round_block('## Files\n- a.py\n- b.py')], req)
    notes = task.get(INJECTED_NOTES_FIELD) or []
    assert len(notes) == 2
    assert all(n['kind'] == 'context-injection' for n in notes)


def test_suppressed_or_disabled_block_emits_no_note():
    task = _task()
    messages = []
    req = ComposeRequest(task=task, model='m',
                         disabled_blocks=frozenset({'modified_files'}))
    append_context_blocks(messages, [_round_block('## Files\n- a.py')], req)
    assert not task.get(INJECTED_NOTES_FIELD)


def test_compose_static_commit_emits_session_context_note_once():
    task = _task()
    messages = [{'role': 'system', 'content': 'base'}]
    req = ComposeRequest(task=task, model='m', project_enabled=True)
    result = compose_context(messages, req)
    # Whatever static/policy blocks committed get ONE aggregate session note.
    if any(r.get('appended') and r.get('placement') == 'system'
           for r in result.manifest):
        kinds = [n['kind'] for n in task.get(INJECTED_NOTES_FIELD) or []]
        assert kinds.count('session-context') == 1
        # Re-running on the same unchanged prefix does not duplicate the note.
        compose_context(list(messages), req)
        kinds = [n['kind'] for n in task.get(INJECTED_NOTES_FIELD) or []]
        assert kinds.count('session-context') == 1


@pytest.mark.parametrize('active,expected', [(0, 0), (2, 2), (272, 272), (True, 0), (-1, 0), (None, 0)])
def test_note_uses_consuming_model_round_not_tool_count(active, expected):
    task = _task(_activeRoundNum=active, toolRounds=[
        {'roundNum': i, 'llmRound': 1} for i in range(7)])
    append_context_blocks([], [_round_block('File evidence')], ComposeRequest(task=task))
    assert task[INJECTED_NOTES_FIELD][0]['llmRound'] == expected


def test_consecutive_notes_land_before_their_actual_consuming_round():
    from lib.tasks_pkg.segments._assemble import _insert_injected_note_segments
    task = _task(_activeRoundNum=8)
    messages = []
    append_context_blocks(messages, [_round_block('First evidence')], ComposeRequest(task=task))
    task['_activeRoundNum'] = 9
    append_context_blocks(messages, [_round_block('Second evidence')], ComposeRequest(task=task))
    segments = [{'type': 'text', 'text': 'Round eight', 'llmRound': 8},
                {'type': 'text', 'text': 'Round nine', 'llmRound': 9}]
    _insert_injected_note_segments(segments, task)
    assert [(s['type'], s['llmRound']) for s in segments] == [
        ('system_note', 8), ('text', 8), ('system_note', 9), ('text', 9)]


def test_session_witness_watermark_follows_successful_recording(monkeypatch):
    from lib.tasks_pkg.context_composer import _record_session_context_note
    import lib.tasks_pkg.segments as segments
    task = _task()
    manifest = [{'id': 'platform_static', 'registryId': 'platform_static',
                 'appended': True, 'placement': 'system', 'hash': 'hash'}]
    original = segments.record_injected_note
    monkeypatch.setattr(segments, 'record_injected_note', lambda *args, **kwargs: None)
    _record_session_context_note(task, manifest)
    assert '_sessionContextNoteSig' not in task
    monkeypatch.setattr(segments, 'record_injected_note', original)
    _record_session_context_note(task, manifest)
    assert task['_sessionContextNoteSig'] == 'platform_static:hash'
    assert len(task[INJECTED_NOTES_FIELD]) == 1
    _record_session_context_note(task, manifest)
    assert len(task[INJECTED_NOTES_FIELD]) == 1


def test_snapshot_note_kinds_survive_assembly_and_thin_serialization():
    from lib.tasks_pkg.segments import record_injected_note
    from lib.tasks_pkg.segments._assemble import _insert_injected_note_segments
    from lib.tasks_pkg.segments._serde import segments_to_json
    task = _task(_activeRoundNum=272)
    for kind in ('session-context', 'context-injection'):
        assert record_injected_note(task, llm_round=272, kind=kind,
                                    reason='programmatic_execution', text=kind)
    values = []
    _insert_injected_note_segments(values, task)
    serialized = segments_to_json(values)
    assert [value['noteKind'] for value in serialized] == [
        'session-context', 'context-injection']
    assert all(value['llmRound'] == 272 for value in serialized)
