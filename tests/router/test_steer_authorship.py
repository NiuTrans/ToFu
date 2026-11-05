"""Steer authorship: the facts a steered input keeps and how they are judged (R2)."""
from __future__ import annotations

import pytest

from lib.turn_initiation import require_human_turn, steer_author, steer_author_turn, steered_input_turn

pytestmark = pytest.mark.unit


def test_the_author_keeps_the_original_words_and_the_canonical_origin():
    assert steer_author({'content': 'translated', 'originalContent': '原文 '}, actor='human') == {
        'actor': 'human', 'text': '原文 '}
    owner_command = steer_author({'content': 'ship it', 'origin': {
        'initiator': 'human', 'channel': {'channel': 'daxiang', 'thread': 'dx:u:1'}}}, actor='human')
    assert owner_command['origin']['initiator'] == 'human'
    assert owner_command['origin']['channel'] == {'channel': 'daxiang', 'thread': 'dx:u:1'}
    require_human_turn(steer_author_turn(owner_command))


@pytest.mark.parametrize('message', [
    {'content': 'event batch', '_initiator': 'channel'},
    {'content': 'report', '_initiator': 'peer', 'origin': {'initiator': 'peer', 'sourceConversationId': 'p'}},
    {'content': 'peer', '_peerMessage': True},
    {'content': 'timer', '_timer': True},
    {'content': 'summary', '_isCompactionSummary': True},
    {'content': '   '},
    {'content': ['blocks']},
    'not a mapping',
])
def test_nonhuman_synthetic_and_textless_steers_record_no_author(message):
    assert steer_author(message, actor='human') is None


def test_a_forward_steered_into_a_project_router_keeps_its_pointer_and_needs_verification():
    author = steer_author({'content': 'x', 'origin': {'initiator': 'human', 'forwardedFrom': {
        'conversationId': 'router', 'turnId': 'reply', 'steerId': 's'}}}, actor='human')
    assert author['origin']['forwardedFrom'] == {'conversationId': 'router', 'turnId': 'reply', 'steerId': 's'}
    with pytest.raises(ValueError, match='owner-scoped verification'):
        require_human_turn(steer_author_turn(author))


def _reply(*records, actor='assistant'):
    return {'conversationId': 'c', 'turnId': 't', 'actor': actor,
            'projection': {'_userSteerInjects': list(records)}}


STEER = {'blockId': 'injection:user-steer:s', 'commandId': 's', 'deliveryState': 'delivered',
         'author': {'actor': 'human', 'text': 'owner words'}}


def test_a_steer_view_is_judged_exactly_like_a_human_turn():
    view = steered_input_turn(_reply(STEER), 's')
    assert view == {'conversationId': 'c', 'turnId': 't', 'steerId': 's', 'actor': 'human',
                    'projection': {'role': 'user', 'content': 'owner words'}}
    require_human_turn(view)
    pending = steered_input_turn(_reply({**STEER, 'deliveryState': 'pending'}), 's')
    assert pending['projection']['content'] == 'owner words'
    virtual = steered_input_turn(_reply({**STEER, 'author': {'actor': 'virtual_user', 'text': 'x'}}), 's')
    with pytest.raises(ValueError, match='not a human Turn'):
        require_human_turn(virtual)


@pytest.mark.parametrize('reply,match', [
    (_reply({**STEER, 'deliveryState': 'queued'}), 'queued'),
    (_reply({**STEER, 'author': None}), 'no recorded human author'),
    (_reply({key: value for key, value in STEER.items() if key != 'author'}), 'no recorded human author'),
    (_reply({**STEER, 'commandId': 'other'}), 'has no steered message'),
    (_reply(STEER, STEER), 'has no steered message'),
    (_reply(STEER, actor='human'), 'not a reply'),
    (_reply(), 'has no steered message'),
])
def test_missing_queued_duplicated_or_authorless_steers_have_no_view(reply, match):
    with pytest.raises(ValueError, match=match):
        steered_input_turn(reply, 's')
