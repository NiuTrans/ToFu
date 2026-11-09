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


def test_tool_round_silence_reloads_with_empty_public_text_and_private_replay(router_turn):
    import threading
    from lib.tasks_pkg.segments import assemble_segments, segments_to_json
    from lib.tasks_pkg.stream_handler._router_silence import declare_silent_turn
    from lib.turn_lifecycle import get_turn, record_task_event
    owner, binding, _pair, task = router_turn
    task.update(content='[SILENT]', thinking='', content_lock=threading.Lock(),
                events=[], status='running', toolRounds=[])
    assert declare_silent_turn(task, {'content': '[SILENT]'}, [], llm_round=0) is True
    assert record_task_event(task, {'type': 'delta_reset', 'discard': True})
    task['toolRounds'] = [{
        'type': 'tool_start', 'toolCallId': 'call-read', 'toolName': 'read_files',
        'toolArgs': '{"path":"AGENTS.md"}', 'toolContent': 'Repository guidance',
        'status': 'done', 'roundNum': 1, 'llmRound': 0,
        'assistantModelContent': '[SILENT]'}]
    assert record_task_event(task, {'type': 'round_committed', 'roundNum': 1})
    task['segments'] = assemble_segments(task)
    task['status'] = 'done'
    assert record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    stored = get_turn(task['convId'], task['_turnId'], user_id=owner)
    projection = stored['projection']
    assert projection['content'] == ''
    assert any(segment.get('noteKind') == 'deliberate-silence'
               for segment in projection['segments'])
    assert any(segment.get('modelText') == '[SILENT]' and segment.get('text') == ''
               for segment in projection['segments'])
    assert all(segment.get('text') != '[SILENT]'
               for segment in projection['segments'])
    from lib.turn_lifecycle import build_api_messages
    replay = build_api_messages(binding['conversationId'], task['_turnId'], {
        'projectPath': binding['projectPath']}, user_id=owner, allow_model_replay=False)
    assert any(message.get('role') == 'assistant' and message.get('content') == '[SILENT]'
               and message.get('tool_calls', [{}])[0].get('id') == 'call-read'
               for message in replay)
    assert any(message.get('role') == 'tool' and message.get('tool_call_id') == 'call-read'
               and message.get('content') == 'Repository guidance' for message in replay)


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
