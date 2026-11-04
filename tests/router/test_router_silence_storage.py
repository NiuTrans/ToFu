"""Deliberate silence follows durable Router Turn and steer identity."""
from __future__ import annotations

import time

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]


def _service():
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    never = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('identity fixtures never start an executor'))
    return ConversationTurnCommandService(
        build_user_message=never, was_aborted_after=lambda *args, **kwargs: False,
        start_task=never)


@pytest.fixture
def router_turn(semantic_authority, monkeypatch, tmp_path):
    from lib import agent_inbox
    from lib.router.binding import global_router
    from lib.turn_lifecycle import bind_task, create_turn_pair
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    binding = global_router(owner, create=True)
    pair = create_turn_pair(
        binding['conversationId'], command_id='channel-notice',
        input_projection={'content': 'No action required.', '_initiator': 'channel',
                          'origin': {'initiator': 'channel', 'channel': {
                              'channel': 'dx', 'thread': 'dx:gid:500', 'eventIds': ['dx:gid:500/1']}}},
        config={'projectPath': binding['projectPath']}, user_id=owner)
    bind_task(pair['attempt']['attemptId'], 'router-task', user_id=owner)
    task = {'id': 'router-task', '_userId': owner, '_turnId': pair['turn']['turnId'],
            '_attemptId': pair['attempt']['attemptId'], 'convId': binding['conversationId'],
            'config': {'projectPath': binding['projectPath']}}
    yield owner, binding, pair, task
    agent_inbox.reset_for_test(binding['conversationId'])


def test_real_channel_turn_allows_silence_and_owner_turn_does_not(router_turn):
    from lib.tasks_pkg.stream_handler._router_silence import silence_declaration_allowed
    from lib.turn_lifecycle import abort_attempt, create_turn_pair
    owner, binding, pair, task = router_turn
    assert silence_declaration_allowed(task, []) is True
    abort_attempt(pair['attempt']['attemptId'], user_id=owner)
    human = create_turn_pair(
        binding['conversationId'], command_id='human-request',
        input_projection={'content': 'Answer this question.'},
        config={'projectPath': binding['projectPath']}, user_id=owner)
    human_task = {**task, '_turnId': human['turn']['turnId'],
                  '_attemptId': human['attempt']['attemptId']}
    assert silence_declaration_allowed(human_task, []) is False


def test_real_owner_steer_revokes_silence_without_changing_the_notice_parent(router_turn):
    from lib.tasks_pkg.stream_handler._router_silence import silence_declaration_allowed
    owner, _binding, _pair, task = router_turn
    assert silence_declaration_allowed(task, []) is True
    result = _service().create_turn(task['convId'], owner, {
        'commandId': 'owner-steer', 'inputTurn': {'content': 'Answer me before doing anything.'},
        'config': {}, 'injectMode': 'steer'}, request_started_at=time.time()).value
    assert result['steered'] is True
    # The live carrier arrives before the next stream/terminal projection.
    from lib import agent_inbox
    items = agent_inbox.drain(task['convId'], modes=['user-steer'])
    task['_userSteerInjects'] = [
        {'commandId': item['injectionId'], 'author': item['steerAuthor']} for item in items]
    assert silence_declaration_allowed(task, []) is False


def test_real_channel_steer_does_not_make_the_notice_human(router_turn):
    from lib.tasks_pkg.stream_handler._router_silence import silence_declaration_allowed
    owner, _binding, _pair, task = router_turn
    result = _service().create_turn(task['convId'], owner, {
        'commandId': 'channel-steer',
        'inputTurn': {'content': 'Another notice', '_initiator': 'channel',
                      'origin': {'initiator': 'channel'}},
        'config': {}, 'injectMode': 'steer'}, request_started_at=time.time()).value
    assert result['steered'] is True
    from lib.turn_lifecycle import get_turn
    records = get_turn(task['convId'], task['_turnId'], user_id=owner)['projection']['_userSteerInjects']
    assert 'author' not in records[0]
    assert silence_declaration_allowed(task, []) is True
