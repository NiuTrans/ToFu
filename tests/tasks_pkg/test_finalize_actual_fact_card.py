"""Final done events carry effective model, mode and user-turn facts.

Exercise the real finalizer with a detached publication port. Assertions target
emitted values, so moving projection code cannot weaken or break this contract.
"""
from copy import deepcopy
import time
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def finalized_event(monkeypatch):
    from lib.tasks_pkg.orchestrator import _finalize

    def run(*, config=None, fields=None, model='requested-model', depth=None,
            usage=None, api_rounds=None):
        events = []
        monkeypatch.setattr(_finalize, 'append_event',
                            lambda _task, event: events.append(deepcopy(event)))
        content = 'The requested work is complete and its results are ready.'
        task = {
            'id': 'finalize-facts-' + uuid4().hex, '_userId': 1,
            'convId': '', 'status': 'running', 'aborted': False,
            'content': content, 'thinking': '', 'error': None,
            'toolRounds': [], 'config': dict(config or {}),
            'created_at': time.time(), '_transientRuntime': True,
            '_invocationEventSink': lambda _event: None,
        }
        task.update(fields or {})
        current_usage = dict(usage or {})
        _finalize._finalize_and_emit_done(
            task, model=model, preset='medium', thinking_depth=depth,
            cfg=task['config'], last_finish_reason='stop',
            last_usage=current_usage, last_stream_result=None,
            accumulated_usage=current_usage, api_rounds=list(api_rounds or []),
            tool_call_happened=False, messages=[], original_messages=[],
            all_search_results_text=[], max_tokens=128, thinking_enabled=False,
            temperature=1.0, _loop_exit_reason='stop', _abort_detected_phase='',
            project_path='', project_enabled=False, round_num=0,
            assistant_msg={'role': 'assistant', 'content': task['content']},
        )
        done = [event for event in events if event['type'] == 'done']
        assert len(done) == 1
        assert task['status'] == 'done'
        return task, done[0]

    return run


@pytest.mark.parametrize('fallback', [None, 'effective-fallback-model'])
def test_actual_model_uses_effective_fallback(finalized_event, fallback):
    fields = ({'_fallback_model': fallback, '_fallback_from': 'requested-model',
               '_fallback_reason': 'provider unavailable', '_fallback_kind': 'route'}
              if fallback else {})
    _, event = finalized_event(fields=fields, depth='high')
    assert event['model'] == 'requested-model'
    assert event['actualModel'] == (fallback or 'requested-model')
    assert event['actualDepth'] == event['thinkingDepth'] == 'high'
    assert event['actualModes'] == []
    if fallback:
        assert event['fallbackModel'] == fallback
        assert event['fallbackFrom'] == 'requested-model'
        assert event['fallbackReason'] == 'provider unavailable'
        assert event['fallbackKind'] == 'route'
    else:
        assert 'fallbackModel' not in event


@pytest.mark.parametrize('depth', [None, '', 'medium'])
def test_actual_depth_is_present_only_when_applied(finalized_event, depth):
    _, event = finalized_event(depth=depth)
    if depth:
        assert event['actualDepth'] == event['thinkingDepth'] == depth
    else:
        assert 'actualDepth' not in event and 'thinkingDepth' not in event


@pytest.mark.parametrize(('config', 'expected'), [
    ({}, []),
    ({'autopilot': True}, [{'label': 'Goal Mode', 'tone': 'mode'}]),
    ({'activeFlow': 'Research'}, [{'label': 'Research', 'tone': 'mode'}]),
    ({'activeFlow': 'Research', 'autopilot': True},
     [{'label': 'Research', 'tone': 'mode'}]),
    ({'activeFlow': {}, 'autopilot': True},
     [{'label': 'Goal Mode', 'tone': 'mode'}]),
    ({'activeFlow': '', 'autopilot': False}, []),
])
def test_actual_modes_follow_current_configuration(finalized_event, config, expected):
    _, event = finalized_event(config=config)
    assert event['actualModes'] == expected


@pytest.mark.parametrize('message_id', ['', 'triggering-user-message'])
def test_fact_card_uses_the_triggering_user_turn(finalized_event, message_id):
    _, event = finalized_event(fields={'_userMsgId': message_id})
    if message_id:
        assert event['userMsgId'] == message_id
    else:
        assert 'userMsgId' not in event
