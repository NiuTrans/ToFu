"""Execution seals preserve observed output; summaries are never replacement logs."""

from copy import deepcopy

import pytest

from lib.agent_core.output_settlement import seal_output_projection
from lib.turn_projection_segments import projection_with_stable_segments

pytestmark = pytest.mark.unit


def _live():
    return projection_with_stable_segments({
        'content': 'Partial report', 'thinking': 'Check the source',
        'toolRounds': [{
            'toolCallId': 'call-a', 'toolName': 'read_files', 'roundNum': 1,
            'llmRound': 0, 'toolArgs': {'path': 'a.py'},
            'toolContent': 'first line\nimportant second line', 'status': 'done',
            'inspectorRef': {'taskId': 'invocation-a', 'roundNum': 1, 'turn': ''},
        }],
    }, status='running')


def test_seal_omissions_and_previews_preserve_live_output_and_identity():
    live = _live()
    original = deepcopy(live)
    completion = {'toolRounds': [{
        'toolCallId': 'call-a', 'toolContent': 'short preview',
        'query': 'read source',
    }]}
    sealed = seal_output_projection(live, completion)
    assert sealed['thinking'] == 'Check the source'
    assert sealed['content'] == 'Partial report'
    assert sealed['segments'] == live['segments']
    assert sealed['toolRounds'][0]['toolContent'] == 'first line\nimportant second line'
    assert sealed['toolRounds'][0]['query'] == 'read source'
    assert sealed['toolRounds'][0]['inspectorRef'] == live['toolRounds'][0]['inspectorRef']
    assert live == original
    sealed['toolRounds'][0]['toolArgs']['path'] = 'different'
    assert live == original


def test_new_completion_answer_appends_without_erasing_prior_text_and_seals_idempotently():
    live = _live()
    completion = {'content': 'Final report', 'thinking': ''}
    sealed = projection_with_stable_segments(seal_output_projection(live, completion))
    assert sealed['content'] == 'Final report'
    assert sealed['thinking'] == 'Check the source'
    blocks = sealed['segments']
    text = [block for block in blocks if block['type'] == 'text']
    assert [block['text'] for block in text] == ['Partial report', 'Final report']
    original = next(block for block in live['segments'] if block['type'] == 'text')
    assert text[0]['blockId'] == original['blockId']
    assert text[1]['blockId'] != original['blockId']
    assert not text[0].get('terminal') and text[1]['terminal']
    assert seal_output_projection(sealed, completion) == sealed


def test_identical_or_empty_final_does_not_duplicate_output():
    live = _live()
    for completion in ({}, {'content': ''}, {'content': live['content']}):
        assert seal_output_projection(live, completion) == live


def test_recorded_compaction_and_tool_failures_are_not_undone_by_summary():
    live = _live()
    live['toolRounds'][0].update(toolContent='[compacted]', status='error', compactionLayer='L1')
    sealed = seal_output_projection(live, {'toolRounds': [{
        'toolCallId': 'call-a', 'toolContent': 'uncompacted preview', 'status': 'done',
    }]})
    assert sealed['toolRounds'] == live['toolRounds']


def test_seal_work_and_size_are_linear_in_recorded_occurrences():
    live = {'content': 'final', 'thinking': '', 'toolRounds': [
        {'toolCallId': f'call-{i}', 'toolContent': 'x' * 2048} for i in range(256)
    ]}
    summary = {'content': 'final', 'toolRounds': [
        {'toolCallId': f'call-{i}', 'toolContent': 'preview'} for i in range(256)
    ]}
    sealed = seal_output_projection(live, summary)
    assert sealed == live
    for _ in range(3):
        sealed = seal_output_projection(sealed, summary)
    assert len(sealed['toolRounds']) == 256
    assert sum(len(row['toolContent']) for row in sealed['toolRounds']) == 256 * 2048


@pytest.mark.parametrize('difference', [
    {'roundNum': 2}, {'llmRound': 2}, {'attemptId': 'another-attempt'},
])
def test_recycled_provider_call_id_is_not_an_occurrence_identity(difference):
    first = {'toolCallId': 'recycled', 'roundNum': 1, 'llmRound': 1,
             'attemptId': 'original', 'toolContent': 'first'}
    second = {**first, **difference, 'toolContent': 'second'}
    completion = {'toolRounds': [second]}
    sealed = seal_output_projection({'toolRounds': [first]}, completion)
    assert [row['toolContent'] for row in sealed['toolRounds']] == ['first', 'second']
    assert seal_output_projection(sealed, completion) == sealed


def test_legacy_ordinal_occurrences_replay_once_without_content_deduplication():
    row = {'roundNum': 1, 'toolName': 'read_files', 'toolContent': 'same'}
    completion = {'toolRounds': [row, dict(row)]}
    sealed = seal_output_projection({}, completion)
    assert len(sealed['toolRounds']) == 2
    for _ in range(3):
        sealed = seal_output_projection(sealed, completion)
    assert len(sealed['toolRounds']) == 2
    with pytest.raises(ValueError, match='occurrence needs'):
        seal_output_projection({}, {'toolRounds': [{'toolName': 'unidentified'}]})


def test_completion_only_blocks_and_cumulative_reasoning_survive_normalization():
    completion = {'content': 'final', 'segments': [
        {'type': 'thinking', 'blockId': 'thinking:first', 'text': 'only evidence'},
        {'type': 'text', 'blockId': 'text:first', 'text': 'earlier narration'},
        {'type': 'text', 'blockId': 'text:last', 'text': 'final', 'terminal': True},
    ]}
    sealed = projection_with_stable_segments(seal_output_projection({}, completion))
    assert [block['text'] for block in sealed['segments']] == [
        'only evidence', 'earlier narration', 'final']
    assert seal_output_projection(sealed, completion) == sealed
    live = projection_with_stable_segments({'content': 'partial', 'thinking': 'first'})
    sealed = projection_with_stable_segments(seal_output_projection(
        live, {'content': 'partial extended', 'thinking': 'first then second'}))
    assert sealed['thinking'] == 'first then second'
    assert [b['text'] for b in sealed['segments'] if b['type'] == 'text'] == ['partial extended']
    assert [b['blockId'] for b in sealed['segments']] == [b['blockId'] for b in live['segments']]


@pytest.mark.parametrize('conflict', [{}, {'llmRound': 3}, {'attemptId': 'other'},
                                      {'toolName': 'write_file'}])
def test_legacy_ordinal_preview_preserves_siblings_and_explicit_conflicts(conflict):
    rows = [{'roundNum': 1, 'llmRound': 2, 'attemptId': 'original',
             'toolName': 'read_files', 'toolContent': text} for text in ('full', 'sibling')]
    preview = {'roundNum': 1, 'toolName': 'read_files', 'toolContent': 'preview', **conflict}
    completion = {'toolRounds': [preview, dict(preview)]}
    sealed = seal_output_projection({'toolRounds': rows}, completion)
    assert [row['toolContent'] for row in sealed['toolRounds'][:2]] == ['full', 'sibling']
    assert len(sealed['toolRounds']) == (4 if conflict else 2)
    assert seal_output_projection(sealed, completion) == sealed


def test_legacy_ordinal_matching_has_linear_comparison_budget(monkeypatch):
    import lib.agent_core.output_settlement as owner
    original = owner._compatible_occurrence
    comparisons = []
    def compatible(left, right):
        comparisons.append(1)
        return original(left, right)
    monkeypatch.setattr(owner, '_compatible_occurrence', compatible)
    rows = [{'roundNum': 1, 'llmRound': 2, 'toolName': 'read_files',
             'toolContent': str(i)} for i in range(512)]
    previews = [{'roundNum': 1, 'toolName': 'read_files', 'toolContent': 'preview'}] * 512
    assert seal_output_projection({'toolRounds': rows}, {'toolRounds': previews})['toolRounds'] == rows
    assert len(comparisons) <= 512


@pytest.mark.parametrize('finish_reason', ['stop', 'aborted', 'length', 'error'])
def test_terminal_metadata_seals_without_replacing_observed_evidence(finish_reason):
    live = _live()
    completion = {'finishReason': finish_reason,
                  'streamState': {'status': 'closed'},
                  'usage': {'outputTokens': 17}}
    sealed = seal_output_projection(live, completion)
    for key, value in completion.items():
        assert sealed[key] == value
    for key in ('content', 'thinking', 'toolRounds', 'segments'):
        assert sealed[key] == live[key]
    assert seal_output_projection(sealed, completion) == sealed
