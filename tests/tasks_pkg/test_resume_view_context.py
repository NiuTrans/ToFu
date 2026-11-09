"""Continue resumes the Turn's exact working view, then composes context on it.

The interrupted attempt's context carriers, rounds and system policy are already
in its checkpointed view. Composing against that view reuses every unchanged
block; only genuinely changed context is appended, at the resume point, and only
that change is recorded as a context note.
"""
from copy import deepcopy
import threading

import pytest

from tests.compaction.test_dispatch_context_recovery import _round
from tests.compaction.test_suspended_working_view import _Archives

pytestmark = pytest.mark.unit


@pytest.fixture
def archives(monkeypatch):
    import lib.agent_core.store
    store = _Archives()
    monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: store)
    return store


def _task(attempt):
    return {'id': f'task-{attempt}', 'convId': 'conversation', '_userId': 1,
            '_turnId': 'turn', '_attemptId': attempt, 'toolRounds': [],
            'config': {'model': 'gpt-6-astra', '_modelReplayBase': [['human', 'a' * 64]]},
            'content': '', 'thinking': '', 'content_lock': threading.Lock()}


def _compose(monkeypatch, task, messages, blocks):
    from lib.tasks_pkg import context_composer
    from lib.tasks_pkg.context_composer import ComposeRequest, compose_context
    monkeypatch.setattr(context_composer, 'collect_context_blocks',
                        lambda messages, request: list(blocks))
    return compose_context(messages, ComposeRequest(task=task, user_id=1, model='gpt-6-astra'))


def _blocks(date='Current date: 2026-11-07'):
    from lib.tasks_pkg.context_composer import context_block
    return [context_block('relevant_memories', '<relevant_memories>keep</relevant_memories>'),
            context_block('skills_index', '<available_skills>skills</available_skills>'),
            context_block('current_date', date)]


def _interrupt(monkeypatch, archives):
    """Run the first attempt to its interruption and checkpoint its view."""
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    from lib.tasks_pkg.segments._types import INJECTED_NOTES_FIELD
    first = _task('first')
    messages = [{'role': 'user', 'content': 'work'}]
    _compose(monkeypatch, first, messages, _blocks())
    assert {note['reason'] for note in first[INJECTED_NOTES_FIELD]} == {
        'relevant_memories', 'skills_index', 'current_date'}
    rounds = [{**_round(i), 'attemptId': 'first', 'taskId': 'task-first'} for i in range(2)]
    messages.extend(_reconstruct_tool_call_messages(rounds))
    first.update(toolRounds=rounds, messages=messages, _messages_include_current_run=True,
                 status='done', aborted=True, finishReason='aborted')
    preserve_suspended_working_view(first)
    return first, rounds


def _resume(archives, first, rounds):
    from lib.conversation_sync.command_service import _resume_executor_config
    from lib.tasks_pkg.compaction._resume_projection import (
        resume_background_digest, resume_execution_digest)
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state, restore_resume_view
    from lib.turn_lifecycle import _task_projection
    projection = _task_projection(first, {})
    config = deepcopy(first['config']) | _resume_executor_config('continue', projection)
    config['_resumeBackgroundDigest'] = resume_background_digest([])
    config['_resumeExecutionDigest'] = resume_execution_digest('', '')
    resumed = _task('second')
    resumed['config'] = config
    fresh = [{'role': 'user', 'content': 'work'}]  # cold history: no carriers
    prepared = prepare_resume_state(config)
    assert restore_resume_view(task=resumed, cfg=config, messages=fresh,
                               prepared_state=prepared)
    return resumed, config, fresh, prepared


def test_unchanged_context_is_reused_without_a_new_record(monkeypatch, archives):
    from lib.tasks_pkg.segments._types import INJECTED_NOTES_FIELD
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, _, view, _ = _resume(archives, first, rounds)
    assert view == archives.rows['archive-1']['post']
    before = deepcopy(view)
    _compose(monkeypatch, resumed, view, _blocks())
    assert view == before
    assert not resumed.get(INJECTED_NOTES_FIELD)


def test_changed_context_is_appended_at_the_resume_point_and_noted_alone(monkeypatch, archives):
    from lib.tasks_pkg.segments._types import INJECTED_NOTES_FIELD
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, _, view, _ = _resume(archives, first, rounds)
    before = deepcopy(view)
    _compose(monkeypatch, resumed, view, _blocks(date='Current date: 2026-11-08'))
    assert view[:len(before)] == before
    assert len(view) == len(before) + 1 and view[-1].get('_contextComposer')
    assert [row['id'] for row in view[-1]['_contextBlocks']] == ['current_date']
    assert [note['reason'] for note in resumed[INJECTED_NOTES_FIELD]] == ['current_date']


def test_turn_memory_selection_is_retained_not_reselected(monkeypatch, archives):
    from lib.tasks_pkg.orchestrator._memory_prefetch import maybe_run_memory_prefetch
    from lib.tasks_pkg.orchestrator._resume_state import resume_view_holds_turn_block
    from lib.tasks_pkg.context_composer import context_block
    from lib.tasks_pkg.segments._types import INJECTED_NOTES_FIELD
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, config, view, _ = _resume(archives, first, rounds)
    assert resume_view_holds_turn_block(resumed, view, 'relevant_memories')
    assert not resume_view_holds_turn_block(_task('cold'), view, 'relevant_memories')
    import lib.memory.prefetch as prefetch
    monkeypatch.setattr(prefetch, 'run_memory_prefetch',
                        lambda *a, **k: pytest.fail('the Turn selection was re-run'))
    maybe_run_memory_prefetch(task=resumed, cfg=config, messages=view, tool_list=[],
                              project_path=None, project_enabled=False, memory_enabled=True,
                              has_real_tools=True, injected_tool_calls=0,
                              turn_selection_retained=True)
    assert resumed['_memoryPrefetch'] == {'phase': 'skipped', 'reason': 'turn_selection_retained'}
    # The composer's own provider keeps the restored block for this outcome.
    retained = context_block('relevant_memories', '', reason='provider_unavailable')
    before = deepcopy(view)
    _compose(monkeypatch, resumed, view, [retained] + _blocks()[1:])
    assert view == before and not resumed.get(INJECTED_NOTES_FIELD)


def test_hydration_keeps_the_restored_view_and_seeds_round_counters(monkeypatch, archives):
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, config, view, prepared = _resume(archives, first, rounds)
    before = deepcopy(view)
    state = RoundState(model='gpt-6-astra', preset='default', thinking_enabled=False)
    apply_resume_state(task=resumed, cfg=config, messages=view, tid='second',
                       prepared_state=prepared, round_state=state)
    assert view == before  # rounds are already in the view: never replayed twice
    assert state.model_round_start == 2 and state.tool_call_happened
    assert resumed['_resumeContextHydrated'] is True


def test_continue_keeps_the_interrupted_attempts_context_record(monkeypatch, archives):
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.turn_lifecycle import _task_projection
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, config, view, prepared = _resume(archives, first, rounds)
    carried = config['checkpointInjectedNotes']
    assert {note['reason'] for note in carried} == {
        'relevant_memories', 'skills_index', 'current_date'}
    assert all(note['attemptId'] == 'first' for note in carried)
    state = RoundState(model='gpt-6-astra', preset='default', thinking_enabled=False)
    apply_resume_state(task=resumed, cfg=config, messages=view, tid='second',
                       prepared_state=prepared, round_state=state)
    segments = _task_projection(resumed, {})['segments']
    notes = [index for index, segment in enumerate(segments)
             if segment['type'] == 'system_note']
    first_round = next(index for index, segment in enumerate(segments)
                       if segment['type'] != 'system_note')
    # The interrupted attempt's record stays at the head of its own rounds.
    assert notes and max(notes) < first_round
    assert {segments[index]['attemptId'] for index in notes} == {'first'}


def test_interruption_during_the_first_model_call_still_resumes_its_view(monkeypatch, archives):
    from lib.conversation_sync.command_service import _resume_executor_config
    from lib.tasks_pkg.compaction._resume_projection import (
        preserve_suspended_working_view, resume_background_digest, resume_execution_digest)
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state, restore_resume_view
    from lib.turn_lifecycle import _task_projection
    first = _task('first')
    messages = [{'role': 'user', 'content': 'work'}]
    _compose(monkeypatch, first, messages, _blocks())
    first.update(messages=messages, _messages_include_current_run=True,
                 status='done', aborted=True, finishReason='aborted')
    preserve_suspended_working_view(first)
    config = deepcopy(first['config']) | _resume_executor_config(
        'continue', _task_projection(first, {}))
    assert 'checkpointToolRounds' not in config  # no round was retained
    config.update(_turnOperation='continue',
                  _resumeBackgroundDigest=resume_background_digest([]),
                  _resumeExecutionDigest=resume_execution_digest('', ''))
    resumed = _task('second')
    fresh = [{'role': 'user', 'content': 'work'}]
    assert restore_resume_view(task=resumed, cfg=config, messages=fresh,
                               prepared_state=prepare_resume_state(config))
    assert fresh == messages


def test_orphan_attempt_notes_are_not_carried():
    from lib.conversation_sync.command_service import _resume_executor_config
    projection = {'toolRounds': [{**_round(0), 'attemptId': 'kept', 'taskId': 't'}],
                  'segments': [
                      {'type': 'system_note', 'text': 'kept record', 'noteKind': 'context-injection',
                       'attemptId': 'kept', 'llmRound': 0, 'blockId': 'n1'},
                      {'type': 'system_note', 'text': 'orphan record', 'noteKind': 'context-injection',
                       'attemptId': 'orphan', 'llmRound': 0, 'blockId': 'n2'}]}
    carried = _resume_executor_config('continue', projection)['checkpointInjectedNotes']
    assert [note['text'] for note in carried] == ['kept record']
