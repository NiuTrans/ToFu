"""L2 carriers name the Turn range they fold and the exact-text recall call.

The recall cursor is get_conversation's 1-based ``before=N``: the window ends
at message N-1, so N = last covered main-lane ordinal + 2 (one more when the
folded range also holds that input's reply Turn).
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from lib.tasks_pkg.compaction import _coverage
from lib.tasks_pkg.compaction._handoff import (
    HANDOFF_FIELD, is_compaction_handoff, make_compaction_handoff,
)
from lib.tasks_pkg.compaction._layer2._prompt import _extract_prior_receipt
from tests.compaction.test_compaction_history_wire import compact, tool_round, user  # noqa: F401

pytestmark = pytest.mark.unit
SUMMARY = '## Context Compacted — Selective Summary\n\n### Objective\nCoordinate.'


def _task(**extra):
    return {'id': 'coverage', 'convId': 'conv-1', '_turnId': 'turn-live',
            '_userId': 7, 'config': {}, **extra}


@pytest.fixture
def ordinals(monkeypatch):
    import lib.turn_lifecycle as turn_lifecycle

    table = {'turn-0': 0, 'turn-1': 1, 'turn-2': 2, 'turn-3': 3}
    reads = []

    def get_turn(conv_id, turn_id, *, user_id):
        reads.append((conv_id, turn_id, user_id))
        if turn_id not in table:
            raise LookupError(turn_id)
        return {'turnId': turn_id, 'ordinal': table[turn_id], 'laneId': 'main'}

    monkeypatch.setattr(turn_lifecycle, 'get_turn', get_turn)
    return table, reads


def _rebuilt():
    """A view rebuilt from Turns, then a live reply without Turn identity."""
    return [user('Open the project', _turnId='turn-0'),
            {'role': 'assistant', 'content': 'Opened.', '_turnId': 'turn-1'},
            user('Plan the release', _turnId='turn-2'),
            *tool_round(1),
            {'role': 'assistant', 'content': 'Planned.'},
            user('Report status', _turnId='turn-live')]


def test_folded_rebuilt_turns_name_their_range_and_cursor(ordinals):
    _, reads = ordinals
    messages = _rebuilt()
    coverage = _coverage.summary_coverage(_task(), messages, messages[:3])
    assert coverage == {'conversationId': 'conv-1', 'firstTurnId': 'turn-0',
                        'lastTurnId': 'turn-2', 'throughReply': False, 'before': 4}
    assert reads == [('conv-1', 'turn-2', 7)]


def test_live_reply_rows_belong_to_the_reply_of_their_input(ordinals):
    messages = _rebuilt()
    coverage = _coverage.summary_coverage(_task(), messages, messages[:6])
    # turn-2 is the input; the folded live rows are its reply Turn (ordinal 3).
    assert (coverage['lastTurnId'], coverage['throughReply'], coverage['before']) == (
        'turn-2', True, 5)


def test_prior_carrier_keeps_the_original_first_turn(ordinals):
    prior = make_compaction_handoff(SUMMARY, phase='pre_turn', coverage={
        'conversationId': 'conv-1', 'firstTurnId': 'turn-0', 'lastTurnId': 'turn-1',
        'throughReply': False, 'before': 3})
    messages = [prior, user('Plan the release', _turnId='turn-2'),
                {'role': 'assistant', 'content': 'Planned.', '_turnId': 'turn-3'},
                user('Report status', _turnId='turn-live')]
    coverage = _coverage.summary_coverage(_task(), messages, messages[:3])
    assert (coverage['firstTurnId'], coverage['lastTurnId'], coverage['before']) == (
        'turn-0', 'turn-3', 5)


def test_unreadable_cursor_still_names_the_range(ordinals):
    messages = [user('Old', _turnId='turn-unknown'), {'role': 'assistant', 'content': 'Old reply'},
                user('Now', _turnId='turn-live')]
    coverage = _coverage.summary_coverage(_task(), messages, messages[:2])
    assert coverage == {'conversationId': 'conv-1', 'firstTurnId': 'turn-unknown',
                        'lastTurnId': 'turn-unknown', 'throughReply': True}
    line = make_compaction_handoff(SUMMARY, phase='pre_turn', coverage=coverage)['content']
    assert 'get_conversation(conversation_id="conv-1") pages this conversation with before=N' in line


@pytest.mark.parametrize('task', [
    _task(_transientRuntime=True), _task(convId=''), _task(_turnId=''), None,
], ids=['transient', 'no-conversation', 'no-turn', 'no-task'])
def test_views_without_durable_turn_identity_carry_no_coverage(ordinals, task):
    messages = _rebuilt()
    assert _coverage.summary_coverage(task, messages, messages[:3]) is None


def test_unidentified_folded_rows_carry_no_coverage(ordinals):
    messages = [user('Old'), {'role': 'assistant', 'content': 'Old reply'}, user('Now')]
    assert _coverage.summary_coverage(_task(), messages, messages[:2]) is None


def test_carrier_names_range_before_summary_and_never_carries_it_forward():
    coverage = {'conversationId': 'conv-1', 'firstTurnId': 'turn-0',
                'lastTurnId': 'turn-2', 'throughReply': True, 'before': 5}
    handoff = make_compaction_handoff(SUMMARY, phase='pre_turn', coverage=coverage)
    content = handoff['content']
    line = ('Covered history: Turns turn-0 through turn-2 and its reply. Their exact text '
            'stays in the durable transcript: get_conversation(conversation_id="conv-1", '
            'before=5) ends at the last covered Turn; a smaller before pages to older Turns.')
    assert line in content and content.index(line) < content.index(SUMMARY)
    assert handoff[HANDOFF_FIELD]['coverage'] == coverage
    # A later summary reads its prior receipt from the first summary heading.
    assert 'Covered history' not in _extract_prior_receipt([handoff])


def test_real_l2_carrier_is_schema_valid_and_names_its_recall(monkeypatch, compact, ordinals):  # noqa: F811
    from jsonschema import Draft202012Validator
    from lib.tasks_pkg.compaction._layer2 import _compact as l2

    messages = [user('Open the project', _turnId='turn-0'),
                {'role': 'assistant', 'content': 'old result ' * 7000, '_turnId': 'turn-1'},
                user('Plan the release', _turnId='turn-2')]
    for index in range(6):
        messages.extend(tool_round(index, size=10, count=2))
    messages.append(user('Report status', _turnId='turn-live'))
    original = deepcopy(messages)
    assert l2.force_compact_if_needed(
        messages, task=_task(), force=True, preserve_budget_tokens=1,
        _compaction_skip_archive=True, _result_meta={})
    handoff = next(m for m in messages if is_compaction_handoff(m))
    coverage = handoff[HANDOFF_FIELD]['coverage']
    assert coverage['conversationId'] == 'conv-1'
    assert coverage['firstTurnId'] in {'turn-0', 'turn-1'}
    assert coverage['lastTurnId'] == 'turn-2' and coverage['throughReply'] is True
    assert coverage['before'] == 5
    assert 'get_conversation(conversation_id="conv-1", before=5)' in handoff['content']
    schema = json.loads((Path(__file__).parents[2]
                         / 'contracts/compaction_handoff_v1.schema.json').read_text())
    Draft202012Validator(schema).validate(handoff)
    # Canonical rows are never rewritten by the carrier.
    assert original[-1] == messages[-1]
