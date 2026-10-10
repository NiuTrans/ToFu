"""Specify path-aware display exclusion without weakening model evidence.

All rows are synthetic, non-secret JSON. Only settled_witness is exercised;
no application, model, network, or database fixture is requested. UI round
normalization must not discard toolRounds or walk arbitrary user JSON.
"""
from copy import deepcopy

import pytest

from lib.storage.model_continuation import settled_witness


pytestmark = pytest.mark.unit

TOP_LEVEL_DISPLAY_FIELDS = ('timingTrace', 'translation', 'usage')
ROUND_DISPLAY_FIELDS = TOP_LEVEL_DISPLAY_FIELDS + ('_swarmSnapshot', '_swarm')
DISPLAY_BEFORE = {
    'timingTrace': {'modelMs': 1, 'events': ['synthetic-start']},
    'translation': {'content': 'Synthetic display translation A'},
    'usage': {'input_tokens': 5, 'output_tokens': 6},
    '_swarmSnapshot': {
        'settled': False,
        'agents': [{'id': 'synthetic-agent', 'status': 'running'}],
    },
    '_swarm': True,
}
DISPLAY_AFTER = {
    'timingTrace': {'modelMs': 9, 'events': ['synthetic-finish']},
    'translation': {'content': 'Synthetic display translation B'},
    'usage': {'input_tokens': 50, 'output_tokens': 60},
    '_swarmSnapshot': {
        'settled': True,
        'agents': [{'id': 'synthetic-agent', 'status': 'completed'}],
    },
    '_swarm': False,
}
ROUND_PATHS = (
    pytest.param(('projection', 'toolRounds', 0), id='toolRounds-first'),
    pytest.param(('projection', 'toolRounds', 1), id='toolRounds-second'),
    pytest.param(('projection', 'segments', 1, '_round'), id='segments-first'),
    pytest.param(('projection', 'segments', 2, '_round'), id='segments-second'),
)


def _synthetic_round(index):
    return {
        'roundNum': index + 1,
        'toolName': 'synthetic_echo',
        'content': f'Synthetic assistant action {index}',
        'toolContent': f'Synthetic full tool receipt {index}',
        'toolModelContent': f'Synthetic model-visible tool receipt {index}',
        'toolArgs': {'message': f'Synthetic tool argument {index}'},
        'toolCallId': f'synthetic-call-{index}',
        'status': 'completed',
        **deepcopy(DISPLAY_BEFORE),
    }


@pytest.fixture
def settled_row():
    return {
        'turnId': 'synthetic-settled-turn',
        'actor': 'assistant',
        'laneId': 'synthetic-lane',
        'parentTurnId': 'synthetic-parent-A',
        'status': 'completed',
        'projection': {
            'role': 'assistant',
            'content': 'Synthetic final answer A',
            'toolRounds': [_synthetic_round(0), _synthetic_round(1)],
            'segments': [
                {'type': 'text', 'content': 'Synthetic assistant preface'},
                {'type': 'tool', '_round': _synthetic_round(0)},
                {'type': 'tool', '_round': _synthetic_round(1)},
            ],
            '_inboxInjects': [
                {'role': 'user', 'content': 'Synthetic inbox message A'},
            ],
            **{field: deepcopy(DISPLAY_BEFORE[field])
               for field in TOP_LEVEL_DISPLAY_FIELDS},
        },
    }


def _at_path(value, path):
    for part in path:
        value = value[part]
    return value


def _witness_without_mutation(row):
    original = deepcopy(row)
    witness = settled_witness(row)
    assert row == original, 'settled_witness must not mutate its input'
    return witness


def _display_edit_pair(row, path, field, edit):
    before = deepcopy(row)
    if edit == 'add':
        del _at_path(before, path)[field]
    after = deepcopy(before)
    target = _at_path(after, path)
    if edit == 'remove':
        del target[field]
    else:
        target[field] = deepcopy(DISPLAY_AFTER[field])
    return before, after


@pytest.mark.parametrize('field', TOP_LEVEL_DISPLAY_FIELDS)
@pytest.mark.parametrize('edit', ('add', 'replace', 'remove'))
def test_top_level_display_edits_do_not_change_witness(settled_row, field, edit):
    before, after = _display_edit_pair(
        settled_row, ('projection',), field, edit,
    )
    assert _witness_without_mutation(before) == _witness_without_mutation(after)


@pytest.mark.parametrize('round_path', ROUND_PATHS)
@pytest.mark.parametrize('field', ROUND_DISPLAY_FIELDS)
@pytest.mark.parametrize('edit', ('add', 'replace', 'remove'))
def test_ui_round_display_edits_do_not_change_witness(
    settled_row, round_path, field, edit,
):
    before, after = _display_edit_pair(settled_row, round_path, field, edit)
    assert _witness_without_mutation(before) == _witness_without_mutation(after)


@pytest.mark.parametrize('round_path', ROUND_PATHS)
@pytest.mark.parametrize('field,replacement', (
    ('content', 'Synthetic assistant action changed'),
    ('toolContent', 'Synthetic full tool receipt changed'),
    ('toolModelContent', 'Synthetic model-visible tool receipt changed'),
    ('toolArgs', {'message': 'Synthetic tool argument changed'}),
    ('toolCallId', 'synthetic-call-changed'),
    ('status', 'failed'),
))
def test_each_ui_round_retains_model_evidence(
    settled_row, round_path, field, replacement,
):
    changed = deepcopy(settled_row)
    _at_path(changed, round_path)[field] = deepcopy(replacement)
    assert _witness_without_mutation(settled_row) != _witness_without_mutation(changed)


@pytest.mark.parametrize('field,replacement', (
    ('status', 'failed'),
    ('parentTurnId', 'synthetic-parent-B'),
))
def test_settled_turn_identity_and_status_remain_evidence(
    settled_row, field, replacement,
):
    changed = deepcopy(settled_row)
    changed[field] = replacement
    assert _witness_without_mutation(settled_row) != _witness_without_mutation(changed)


@pytest.mark.parametrize('field,replacement', (
    ('content', 'Synthetic final answer B'),
    ('_inboxInjects', [{'role': 'user', 'content': 'Synthetic inbox message B'}]),
))
def test_projection_content_and_inbox_injections_remain_evidence(
    settled_row, field, replacement,
):
    changed = deepcopy(settled_row)
    changed['projection'][field] = deepcopy(replacement)
    assert _witness_without_mutation(settled_row) != _witness_without_mutation(changed)


@pytest.mark.parametrize('container', ('toolRounds', 'segments'))
@pytest.mark.parametrize('edit', ('append', 'remove', 'reverse'))
def test_round_presence_and_order_remain_evidence(settled_row, container, edit):
    changed = deepcopy(settled_row)
    rounds = changed['projection'][container]
    if edit == 'append':
        rounds.append(deepcopy(rounds[-1]))
    elif edit == 'remove':
        rounds.pop()
    else:
        rounds.reverse()
    assert _witness_without_mutation(settled_row) != _witness_without_mutation(changed)


@pytest.mark.parametrize('round_path', (ROUND_PATHS[0], ROUND_PATHS[2]))
@pytest.mark.parametrize('user_path', (
    pytest.param(('record',), id='ordinary-user-object'),
    pytest.param(('toolRounds', 0), id='user-toolRounds-not-ui'),
    pytest.param(('segments', 0, '_round'), id='user-segment-round-not-ui'),
))
@pytest.mark.parametrize('field', ROUND_DISPLAY_FIELDS)
def test_nested_user_json_display_names_remain_model_evidence(
    settled_row, round_path, user_path, field,
):
    # Even round-shaped JSON below toolArgs is user data, not a UI round.
    user_record = {
        'toolName': 'synthetic_user_document',
        'toolCallId': 'synthetic-user-document-id',
        'content': 'Synthetic literal user JSON',
        **deepcopy(DISPLAY_BEFORE),
    }
    before = deepcopy(settled_row)
    _at_path(before, round_path)['toolArgs']['payload'] = {
        'record': deepcopy(user_record),
        'toolRounds': [deepcopy(user_record)],
        'segments': [{'type': 'tool', '_round': deepcopy(user_record)}],
    }
    after = deepcopy(before)
    user_payload = _at_path(after, round_path)['toolArgs']['payload']
    _at_path(user_payload, user_path)[field] = deepcopy(DISPLAY_AFTER[field])
    assert _witness_without_mutation(before) != _witness_without_mutation(after)


def test_witness_is_repeatable_and_leaves_original_input_unchanged(settled_row):
    original = deepcopy(settled_row)
    first = _witness_without_mutation(settled_row)
    second = _witness_without_mutation(settled_row)
    assert first == second
    assert settled_row == original


@pytest.mark.parametrize('before,after', [(1, True), (0, False), (True, 1), (False, 0), (1, 1.0)])
def test_protected_json_types_are_real_model_context_changes(before, after):
    from lib.storage.model_continuation import model_projections_equal
    left = {'toolRounds': [{'toolArgs': {'payload': [{'value': before}]}, 'toolContent': 'same'}]}
    right = {'toolRounds': [{'toolArgs': {'payload': [{'value': after}]}, 'toolContent': 'same'}]}
    assert model_projections_equal(left, right) is False
    assert model_projections_equal(left, deepcopy(left)) is True


def test_terminal_witness_binds_semantic_revision_and_current_attempt(settled_row):
    row = settled_row | {'projectionRevision': 10, 'modelContextRevision': 4,
                         'currentAttemptId': 'accepted-attempt'}
    display = deepcopy(row)
    display['projectionRevision'] += 1
    display['projection']['toolRounds'][0]['_swarmSnapshot']['settled'] = True
    assert settled_witness(row) == settled_witness(display)
    reverted_edit = deepcopy(display)
    reverted_edit['modelContextRevision'] += 2
    assert settled_witness(row) != settled_witness(reverted_edit)
    next_attempt = row | {'currentAttemptId': 'new-continue-attempt'}
    assert settled_witness(row) != settled_witness(next_attempt)


@pytest.mark.parametrize('version,reason', [(1, 'legacy_proof'), (2, 'proof_requires_authority')])
def test_plain_list_fallback_cannot_reuse_compact_history_proof(monkeypatch, settled_row, version, reason):
    from lib.tasks_pkg.model_replay import restore_prefix, policy_witness
    from lib.storage.model_continuation import ModelContinuationStore
    config = {'model': 'gpt-6.1-sol'}
    root = settled_row | {'turnId': 'root', 'laneId': 'main', 'parentTurnId': None}
    rows = [root, {'turnId': 'target', 'laneId': 'main', 'projection': {}}]
    cached = {'policy': policy_witness(config), 'turns': [settled_witness(root)],
              'messages': [{'role': 'assistant', 'content': 'cached summary must stay hidden'}],
              'historyProof': {'version': version, 'count': 0, 'digest': '0' * 64}}
    monkeypatch.setattr(ModelContinuationStore, 'get', lambda self, **kwargs: cached)
    assert restore_prefix(rows, config, conversation_id='conv', user_id=1) == ([], 0)
    assert config['_modelReplayStatus'] == reason


@pytest.mark.parametrize('kind', ['text', 'thinking'])
def test_translation_overlay_witness_matches_real_writer(settled_row, kind):
    from lib.translate.commit import _merge_translation
    row = deepcopy(settled_row)
    row['projection']['segments'].append({'type': kind, 'blockId': 'source:block',
                                         'text': 'source text', 'deliverable': False})
    after = deepcopy(row)
    after['projection'], changed = _merge_translation(
        row['projection'], field='translatedContent', translated_text='display answer',
        model='synthetic translator', segment_translations={'source:block': 'display segment'},
        segment_sources={'source:block': 'source text'})
    assert changed
    assert after['projection']['segments'][-1]['translatedText'] == 'display segment'
    assert _witness_without_mutation(row) == _witness_without_mutation(after)
    after['projection']['segments'][-1]['text'] = 'edited source'
    assert _witness_without_mutation(row) != _witness_without_mutation(after)


@pytest.mark.parametrize('field', ['translatedText', 'translatedContent', '_translateDone'])
def test_translation_names_inside_tool_evidence_remain_protected(settled_row, field):
    after = deepcopy(settled_row)
    after['projection']['toolRounds'][0]['toolArgs'][field] = 'new evidence'
    assert _witness_without_mutation(settled_row) != _witness_without_mutation(after)


def test_unknown_segment_translation_fields_are_not_silently_excluded(settled_row):
    for kind in ('unknown', ['malformed']):
        row = deepcopy(settled_row)
        row['projection']['segments'].append({'type': kind, 'translatedText': 'one'})
        after = deepcopy(row)
        after['projection']['segments'][-1]['translatedText'] = 'two'
        assert _witness_without_mutation(row) != _witness_without_mutation(after)
