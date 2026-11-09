"""Global Router deliberate-silence is a scoped terminal outcome, never a dropped reply."""
from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

pytest_plugins = ('tests._chat_sidecar',)

from lib.tasks_pkg.stream_handler._analyse import analyse_stream_result
from lib.tasks_pkg.stream_handler._intent_stall import is_silent_turn
from lib.tasks_pkg.stream_handler._router_silence import (
    declare_silent_turn, silence_declaration_allowed as silence_allowed,
)

pytestmark = pytest.mark.unit

NOTICE = {'role': 'user', 'content': 'channel event', '_initiator': 'channel'}
HUMAN = {'role': 'user', 'content': 'please respond'}


def _task():
    return {
        'id': 'router-task', '_userId': 7, '_turnId': 'out-1',
        'convId': 'coordinator-test', 'status': 'running',
        'content': '[SILENT]', 'thinking': 'reviewed privately',
        'aborted': False, 'events': [], 'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
    }


def _eligible(monkeypatch, value=True):
    monkeypatch.setattr('lib.router.policy.router_scope', lambda task: (
        {'project_path': '/router'} if value else None))


def _turns(monkeypatch, *, initiator='channel', human_steer=False):
    from lib.turn_initiation import steer_author
    human = {'role': 'user', 'content': 'channel event', 'origin': {'initiator': initiator}}
    records = []
    if human_steer:
        author = steer_author({'content': 'owner question'}, actor='human')
        records.append({'blockId': 'injection:user-steer:owner',
                        'commandId': 'owner', 'deliveryState': 'delivered', 'author': author})
    rows = {
        ('coordinator-test', 'in-1'): {
            'conversationId': 'coordinator-test', 'turnId': 'in-1',
            'actor': 'human', 'projection': human},
        ('coordinator-test', 'out-1'): {
            'conversationId': 'coordinator-test', 'turnId': 'out-1',
            'actor': 'assistant', 'parentTurnId': 'in-1',
            'projection': {'_userSteerInjects': records}},
    }
    monkeypatch.setattr('lib.turn_lifecycle.get_turn',
                        lambda conversation_id, turn_id, *, user_id, include_projection=True:
                        rows[(conversation_id, turn_id)])
    return rows


@pytest.mark.parametrize('text,expected', [
    ('[SILENT]', True), (' \n[SILENT]\n ', True),
    ('[SILENT]\n[END_TURN: done]', True),
    ('[SILENT] no action needed', False),
    ('discuss [SILENT] here', False), ('', False), ('[SILENT', False),
])
def test_only_an_exact_control_declaration_is_silence(text, expected):
    assert is_silent_turn(text) is expected


@pytest.mark.parametrize('initiator,expected_content,expected_calls', [
    ('channel', '', 1), ('human', 'I have the answer.', 2),
])
def test_complete_root_loop_settles_a_notice_silently_or_recovers_a_human_answer(
        tmp_path, monkeypatch, chat_sidecar, initiator, expected_content, expected_calls):
    from lib.router.binding import global_router
    from lib.turn_lifecycle import bind_task, create_turn_pair
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop
    from lib.tasks_pkg.orchestrator._config_resolution import resolve_and_seed_model_config
    from lib.tasks_pkg.orchestrator._post_loop import finalize_after_loop
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    from lib.tasks_pkg.manager import create_task
    from tests.llm.test_llm_replay_scenarios import _install_replay_route
    _install_replay_route()
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    binding = global_router(1, create=True)
    pair = create_turn_pair(
        binding['conversationId'], command_id=f'{initiator}-replay-input',
        input_projection={'content': 'Project completed; no action needed.',
                          '_initiator': initiator, 'origin': {'initiator': initiator}},
        config={'projectPath': binding['projectPath']}, user_id=1)
    task = create_task(binding['conversationId'], [{
        'role': 'user', 'content': 'Project completed; no action needed.', '_initiator': initiator}],
        {'model': 'llm-replay-model', 'webSearchEnabled': False, 'plugins': [],
         'projectPath': binding['projectPath'], '_turnId': pair['turn']['turnId']},
        user_id=1)
    bind_task(pair['attempt']['attemptId'], task['id'], user_id=1)
    task['_turnId'] = pair['turn']['turnId']
    task['_attemptId'] = pair['attempt']['attemptId']
    mcfg = resolve_and_seed_model_config(task['config'], task)
    tools, _ = assemble_round_tools(task['config'], task, mcfg)
    state = RoundState(model=mcfg['model'], preset=mcfg['preset'],
                       thinking_enabled=bool(mcfg['thinking_enabled']))
    seed = [{'role': 'system', 'content': 'Router replay system prompt'},
            {'role': 'user', 'content': 'Project completed; no action needed.', '_initiator': initiator}]
    messages = [dict(row) for row in seed]
    request = root_loop.RootLoopRequest(
        task=task, state=state, messages=messages, tool_list=tools,
        all_search_results_text=[], cfg=task['config'], tid=task['id'][:8],
        thinking_depth=mcfg['thinking_depth'], temperature=mcfg['temperature'],
        max_tokens=4096, response_format=None,
        project_path=binding['projectPath'], project_enabled=True, search_enabled=False)
    from lib.llm.stream_result import ProviderStreamResult
    calls = []
    def stream(body, **kwargs):
        calls.append(body)
        content = '[SILENT]' if len(calls) == 1 else 'I have the answer.'
        on_content = kwargs.get('on_content')
        for chunk in (content[:1], content[1:]):
            on_content(chunk)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': content}, 'stop',
            {'prompt_tokens': 1, 'completion_tokens': 1})
    monkeypatch.setattr('lib.llm.stream_chat', stream)
    monkeypatch.setattr('lib.tasks_pkg.stream_handler._budget._interruptible_sleep', lambda *_: None)
    result = root_loop.run_root_agent_loop(request)
    finalize_after_loop(
        task, cfg=task['config'], tid=request.tid, model=state.model, preset=state.preset,
        thinking_depth=mcfg['thinking_depth'], thinking_enabled=state.thinking_enabled,
        temperature=mcfg['temperature'], max_tokens=4096, messages=messages,
        original_messages=seed, tool_list=tools, assistant_msg=state.assistant_msg,
        round_num=result.last_round_num, accumulated_usage=state.accumulated_usage,
        api_rounds=state.api_rounds, last_finish_reason=state.last_finish_reason,
        last_usage=state.last_usage, last_stream_result=state.last_stream_result,
        tool_call_happened=state.tool_call_happened, all_search_results_text=[],
        project_path=request.project_path, project_enabled=request.project_enabled,
        loop_exit_reason=state.exit_reason, abort_detected_phase=state.abort_phase)
    assert task['status'] == 'done' and task['content'] == expected_content
    assert task.get('_silentTurn', False) is (initiator == 'channel')
    # After the terminal ACK the committed Turn owns accepted notes; the live
    # task's duplicate note list is released (docs/TASK_EVENT_DELIVERY.md).
    from lib.turn_lifecycle import get_turn
    segments = get_turn(binding['conversationId'], pair['turn']['turnId'],
                        user_id=1)['projection'].get('segments') or []
    assert any(segment.get('type') == 'system_note'
               and segment.get('noteKind') == 'deliberate-silence'
               for segment in segments) is (initiator == 'channel')
    assert len(calls) == expected_calls


def test_only_router_automatic_notices_can_declare_silence(monkeypatch):
    _eligible(monkeypatch)
    _turns(monkeypatch)
    message = {'role': 'assistant', 'content': '[SILENT]', 'reasoning_content': 'reviewed'}
    task = _task()
    assert declare_silent_turn(task, message, [NOTICE], llm_round=0) is True
    assert task['content'] == ''
    assert task['_silentTurn'] is True
    assert message['content'] == '[SILENT]'
    assert message['reasoning_content'] == 'reviewed'
    note = task['_injected_notes'][0]
    assert note['kind'] == 'deliberate-silence'
    assert note['action'] == 'stop'
    from lib.tasks_pkg.segments import assemble_segments
    task['segments'] = assemble_segments(task)
    assert next(segment for segment in task['segments']
                if segment.get('type') == 'system_note')['noteKind'] == 'deliberate-silence'
    assert all(segment.get('type') != 'text' for segment in task['segments'])

    for initiator, router in [('human', True), ('channel', False)]:
        _eligible(monkeypatch, router)
        _turns(monkeypatch, initiator=initiator)
        other = _task()
        reply = {'role': 'assistant', 'content': '[SILENT]'}
        assert declare_silent_turn(other, reply, [HUMAN if initiator == 'human' else NOTICE],
                                   llm_round=0) is False
        if initiator == 'human':
            assert other['content'] == reply['content'] == ''
        else:
            assert other['content'] == reply['content'] == '[SILENT]'
        assert '_silentTurn' not in other


def test_router_notice_settles_without_retry_and_empty_notice_still_retries(monkeypatch):
    _eligible(monkeypatch)
    _turns(monkeypatch)
    task = _task()
    decision = analyse_stream_result(
        {'role': 'assistant', 'content': '[SILENT]'}, 'stop', task,
        'router-task', 'model', 0, 0, [NOTICE], {})
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'stop'
    assert task['content'] == ''
    assert task['_silentTurn'] is True

    empty = _task()
    empty['content'] = ''
    with patch('lib.tasks_pkg.stream_handler._budget._interruptible_sleep', lambda *_: None):
        decision = analyse_stream_result(
            {'role': 'assistant', 'content': '', 'reasoning_content': 'thought'},
            'stop', empty, 'router-task', 'model', 0, 0, [NOTICE],
            {'_empty_stop': True, '_stream_anomaly': True,
             '_chunks_received': 20, '_stream_elapsed_ms': 1000})
    assert decision['action'] == 'continue'
    assert '_silentTurn' not in empty


@pytest.mark.parametrize('initiator,content,public,action', [
    ('channel', '[SILENT]\n[END_TURN: done]', '[SILENT]', 'break'),
    ('human', '[SILENT]', '', 'continue'),
])
def test_break_keeps_the_provider_declaration_in_terminal_stream_evidence(
        monkeypatch, initiator, content, public, action):
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.orchestrator._stream_decision import apply_stream_decision
    _eligible(monkeypatch)
    _turns(monkeypatch, initiator=initiator)
    message = {'role': 'assistant', 'content': content}
    state = RoundState(model='model', preset='p', thinking_enabled=False)
    state.assistant_msg = message
    state.last_finish_reason = 'stop'
    state.last_usage = {'prompt_tokens': 1, 'completion_tokens': 1}
    # Production shares one message between the public reply and provider evidence.
    state.last_stream_result = ProviderStreamResult.from_legacy(message, 'stop', state.last_usage)
    unchanged = state.last_stream_result
    with patch('lib.tasks_pkg.stream_handler._budget._interruptible_sleep', lambda *_: None):
        result, _ = apply_stream_decision(
            _task(), state, round_num=0, tid='router-task', premature_retry_count=0,
            messages=[NOTICE if initiator == 'channel' else HUMAN])
    assert result == action
    assert state.assistant_msg['content'] == public
    if action == 'break':
        assert state.last_stream_result.message['content'] == content
        assert state.last_stream_result.state is unchanged.state
    else:
        assert state.last_stream_result is unchanged


def test_python_stream_holds_a_candidate_then_discards_it_or_releases_text(monkeypatch):
    from lib.tasks_pkg.manager._delta_coalescer import TaskTextDeltaCoalescer

    _eligible(monkeypatch)
    _turns(monkeypatch)
    for chunks, expected_content, expected_events in [
        (('[', 'SIL', 'ENT]'), '', []),
        (('[SIL', 'ENT] ordinary answer'), '[SILENT] ordinary answer',
         ['[SILENT] ordinary answer']),
    ]:
        task = _task()
        task['content'] = ''
        task['thinking'] = ''
        task['messages'] = [NOTICE]
        events = []
        stream = TaskTextDeltaCoalescer(
            task, lambda _task, event: events.append(event),
            on_first_delta=lambda: None, on_after_delta=lambda: None,
            delay_s=60)
        for chunk in chunks:
            stream.on_content(chunk)
        stream.close()
        assert task['content'] == expected_content
        assert ''.join(event.get('content') or '' for event in events
                       if event.get('type') == 'delta') == ''.join(expected_events)


def test_human_steer_into_a_notice_turn_disables_silence_at_the_terminal(monkeypatch):
    _eligible(monkeypatch)
    _turns(monkeypatch)
    task = _task()
    from lib import agent_inbox
    agent_inbox.enqueue(task['convId'], 'owner follow-up', mode='user-steer',
                        extra={'steerAuthor': {'actor': 'human', 'text': 'Answer me'}})
    assert silence_allowed(task, []) is False
    consumed = agent_inbox.drain(task['convId'], modes=['user-steer'])
    assert silence_allowed(task, []) is False
    # The durable parent does not change; a later genuine owner steer does.
    _turns(monkeypatch, human_steer=True)
    task['_userSteerInjects'] = [{'commandId': 'owner-steer',
                                 'author': item['steerAuthor']} for item in consumed]
    reply = {'role': 'assistant', 'content': '[SILENT]'}
    assert declare_silent_turn(task, reply, [NOTICE], llm_round=0) is False
    assert task['content'] == '' and reply['content'] == ''
    assert '_silentTurn' not in task


def test_router_marker_prose_stays_prose_and_a_reset_drops_an_uncommitted_candidate(monkeypatch):
    from lib.tasks_pkg.manager._delta_coalescer import TaskTextDeltaCoalescer
    _eligible(monkeypatch)
    _turns(monkeypatch, initiator='human')
    task = _task()
    task['content'] = '[SILENT] Here is the answer'
    events = []
    reply = {'role': 'assistant', 'content': '[SILENT] Here is the answer'}
    assert declare_silent_turn(task, reply, [HUMAN], llm_round=0) is False
    assert task['content'] == reply['content'] == '[SILENT] Here is the answer'
    assert '_silentTurn' not in task
    assert events == []

    _turns(monkeypatch)
    task = _task()
    task.update(content='', thinking='', messages=[NOTICE])
    stream = TaskTextDeltaCoalescer(
        task, lambda _t, _e: None, on_first_delta=lambda: None,
        on_after_delta=lambda: None, delay_s=60)
    for chunk in ('[', 'SILENT]'):
        stream.on_content(chunk)
    stream.discard_held_content()
    stream.on_content('Replacement answer')
    stream.close()
    assert task['content'] == 'Replacement answer'


def test_native_text_and_terminal_share_the_same_policy(monkeypatch):
    from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy
    observed = []
    monkeypatch.setattr(NativeTaskTerminalPolicy, '_projection_identity', lambda self: observed.append(
        (self.task.get('_silentTurn'), self.task.get('_silentTextCandidate'))))
    import json
    from lib.agent_core.native_process import TerminalRequest, TextDelta
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.native_execution import project_native_text
    from lib.tasks_pkg.native_terminal import project_native_terminal

    _eligible(monkeypatch)
    _turns(monkeypatch)
    task = _task()
    task['content'] = ''
    task['thinking'] = ''
    task['messages'] = [NOTICE]
    events = []
    with patch('lib.tasks_pkg.manager.append_event', side_effect=lambda _t, e: events.append(e)):
        for index, chunk in enumerate(('[', 'SILENT]'), 1):
            project_native_text(task, TextDelta(
                PrincipalContext.user(subject_id='test', owner_user_id=7),
                'router-task', index, 'content', chunk))
    assert task['content'] == ''
    assert task['_silentTextCandidate'] == '[SILENT]'
    assert events == []

    event = {'type': 'done', 'status': 'done', 'result': {
        'messages': [NOTICE, {'role': 'assistant', 'content': '[SILENT]',
                              'reasoning_content': 'reviewed'}], 'usage': []}}
    request = TerminalRequest(
        PrincipalContext.user(subject_id='test', owner_user_id=7),
        'router-task', 'commit', 1, json.dumps(event).encode(),
        b'{"invariants_satisfied":true}')
    project_native_terminal(task, request)
    assert task['status'] == 'done' and task['finishReason'] == 'stop'
    assert task['content'] == '' and task['_silentTurn'] is True
    assert task['messages'][-1]['content'] == '[SILENT]'
    assert task['messages'][-1]['reasoning_content'] == 'reviewed'
    assert task['_injected_notes'][0]['kind'] == 'deliberate-silence'
    from lib.tasks_pkg.segments import assemble_segments
    assert next(segment for segment in assemble_segments(task)
                if segment.get('type') == 'system_note')['noteKind'] == 'deliberate-silence'


def test_native_human_only_control_text_is_not_an_empty_success(monkeypatch):
    import json
    from lib.agent_core.native_process import TerminalRequest
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.native_terminal import project_native_terminal

    for content in ('[SILENT]', '[SILENT][END_TURN: done]'):
        _eligible(monkeypatch)
        _turns(monkeypatch, initiator='human')
        task = _task()
        task.update(content='', thinking='', _silentTextCandidate=content)
        event = {'type': 'done', 'status': 'done', 'result': {
            'messages': [HUMAN, {'role': 'assistant', 'content': content}], 'usage': []}}
        project_native_terminal(task, TerminalRequest(
            PrincipalContext.user(subject_id='test', owner_user_id=7),
            'router-task', 'commit', 1, json.dumps(event).encode(),
            b'{"invariants_satisfied":true}'))
        assert task['content'] == '' and task['messages'][-1]['content'] == ''
        assert '_silentTurn' not in task and '_silentTextCandidate' not in task
        assert task['status'] == 'done' and task['finishReason'] == 'abnormal_stop'
        assert task['error']['kind'] == 'abnormal_stop'
        task['status'] = 'running'
        task.pop('error', None)
        task['content'] = content
        with patch('lib.tasks_pkg.stream_handler._budget._interruptible_sleep', lambda *_: None):
            decision = analyse_stream_result(
                {'role': 'assistant', 'content': content}, 'stop', task,
                'router-task', 'model', 0, 0, [HUMAN], {'_empty_stop': True})
        assert decision['action'] == 'continue' and task['content'] == ''
        assert task['messages'][-1]['content'] == ''


def test_tool_round_marker_is_private_replay_not_public_or_channel_text(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _parse
    _eligible(monkeypatch)
    _turns(monkeypatch)
    task = _task()
    task.update(content='', toolRounds=[], _tool_schema=[])
    assistant = {'role': 'assistant', 'content': '[SILENT]', 'tool_calls': [{
        'id': 'call-read', 'type': 'function', 'function': {
            'name': 'read_files', 'arguments': '{"path":"AGENTS.md"}'}}]}
    parsed, _ = _parse.parse_tool_calls(assistant, task, 0, 0, False)
    row = parsed[0][5]
    row.update(toolContent='AGENTS.md guidance', status='done')
    assert row.get('assistantContent', '') == ''
    assert row['assistantModelContent'] == '[SILENT]'
    from lib.tasks_pkg.segments import assemble_segments, segments_to_json, rehydrate_segments
    from lib.tasks_pkg.conv_message_builder._transform import _build_assistant_messages
    segments = assemble_segments(task)
    assert all(segment.get('text') != '[SILENT]' for segment in segments
               if segment.get('type') == 'text')
    public = {'role': 'assistant', 'content': '', 'toolRounds': task['toolRounds'],
              'segments': segments_to_json(segments)}
    replay = _build_assistant_messages(public)
    assert replay[0]['content'] == '[SILENT]'
    assert replay[0]['tool_calls'][0]['id'] == 'call-read'
    thin = segments_to_json(segments)
    assert _build_assistant_messages({**public, 'segments': thin})[0]['content'] == '[SILENT]'
    restored = rehydrate_segments(thin, task['toolRounds'])
    tool = next(segment for segment in restored if segment.get('type') == 'tool_use')
    assert tool['_round'] is task['toolRounds'][0]


def test_native_denied_or_partial_control_text_remains_an_ordinary_candidate(monkeypatch):
    import json
    from lib.agent_core.native_process import TerminalRequest
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.native_terminal import project_native_terminal
    from lib.tasks_pkg.stream_handler._intent_stall import strip_end_turn_marker
    for content, expected in [('[SILENT]', ''), ('[SILENT][END_TURN', '[SILENT][END_TURN')]:
        _eligible(monkeypatch)
        _turns(monkeypatch, initiator='human')
        task = _task()
        task.update(content='', thinking='', _silentTextCandidate=content)
        event = {'type': 'done', 'status': 'done', 'result': {
            'messages': [HUMAN, {'role': 'assistant', 'content': content}], 'usage': []}}
        project_native_terminal(task, TerminalRequest(
            PrincipalContext.user(subject_id='test', owner_user_id=7),
            'router-task', 'commit', 1, json.dumps(event).encode(),
            b'{"invariants_satisfied":true}'))
        public = expected
        assert task['content'] == public
        assert '_silentTurn' not in task
        assert '_silentTextCandidate' not in task
        assert strip_end_turn_marker(task['content']) == public


def test_registered_silence_note_is_required_and_structural_flush_releases_prose(monkeypatch):
    from lib.tasks_pkg.manager._delta_coalescer import TaskTextDeltaCoalescer
    _eligible(monkeypatch)
    _turns(monkeypatch)
    task = _task()
    monkeypatch.setattr('lib.tasks_pkg.segments.record_injected_note', lambda *a, **k: None)
    with pytest.raises(ValueError, match='registered system-note'):
        declare_silent_turn(task, {'content': '[SILENT]'}, [NOTICE], llm_round=0)
    assert '_silentTurn' not in task

    monkeypatch.undo()
    _eligible(monkeypatch)
    _turns(monkeypatch)
    task = _task()
    task.update(content='', thinking='', messages=[NOTICE])
    events = []
    stream = TaskTextDeltaCoalescer(
        task, lambda _t, event: events.append(event),
        on_first_delta=lambda: None, on_after_delta=lambda: None, delay_s=60)
    stream.on_content('[SILENT')
    stream.flush()
    assert task['content'] == '[SILENT'
    assert events[0]['content'] == '[SILENT'

    task = _task()
    task.update(content='', thinking='', messages=[NOTICE])
    events = []
    stream = TaskTextDeltaCoalescer(
        task, lambda _t, event: events.append(event),
        on_first_delta=lambda: None, on_after_delta=lambda: None, delay_s=60)
    stream.on_content('[SILENT][END_TURN')
    stream.close_after_error(RuntimeError('provider interrupted'))
    assert task['content'] == '[SILENT][END_TURN'
    assert events[0]['content'] == '[SILENT][END_TURN'


def test_nonrouter_candidates_stream_normally_and_human_marker_is_recovered(monkeypatch):
    from lib.tasks_pkg.manager._delta_coalescer import TaskTextDeltaCoalescer
    _eligible(monkeypatch, False)
    _turns(monkeypatch)
    task = _task()
    task.update(content='', thinking='', messages=[NOTICE])
    events = []
    stream = TaskTextDeltaCoalescer(
        task, lambda _task, event: events.append(event),
        on_first_delta=lambda: None, on_after_delta=lambda: None,
        delay_s=60)
    for chunk in ('[', 'SILENT]'):
        stream.on_content(chunk)
    stream.close()
    assert task['content'] == '[SILENT]'
    assert ''.join(event.get('content') or '' for event in events
                   if event.get('type') == 'delta') == '[SILENT]'

    _eligible(monkeypatch)
    _turns(monkeypatch, initiator='human')
    task = _task()
    reply = {'role': 'assistant', 'content': '[SILENT]\n[END_TURN: done]'}
    decision = analyse_stream_result(reply, 'stop', task, 'router-task', 'model',
                                     0, 0, [HUMAN], {'_empty_stop': True})
    assert decision['action'] == 'continue'
    assert task['content'] == reply['content'] == ''
    assert '_silentTurn' not in task
