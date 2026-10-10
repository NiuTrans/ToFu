"""Accepted turn input is the server-authored GoalRun objective boundary."""

from __future__ import annotations

import pytest

import lib.conversation_sync.command_service as command_module
from lib.conversation_sync.command_service import ConversationTurnCommandService


pytestmark = pytest.mark.unit


def _wire_lifecycle(monkeypatch, create_calls=None):
    result = {
        '_needsStart': True,
        'submittedTurn': {
            'turnId': 'turn-input', 'actor': 'human',
            'projection': {'role': 'user', 'content': 'accepted objective'},
        },
        'turn': {
            'turnId': 'turn-output', 'conversationId': 'conv-goal',
            'actor': 'assistant', 'kind': 'reply', 'projection': {},
        },
        'attempt': {'attemptId': 'attempt-goal', 'operation': 'create'},
    }
    accepted_config = {}
    def create(*_args, **kwargs):
        accepted_config.update(kwargs['config'])
        if create_calls is not None:
            create_calls.append(kwargs)
        return result

    monkeypatch.setattr(command_module, 'create_turn_pair', create)
    monkeypatch.setattr(command_module, 'claim_attempt_start', lambda *_a, **_k: True)
    monkeypatch.setattr(
        command_module, 'bind_task',
        lambda *_args, **_kwargs: {'attemptId': 'attempt-goal'},
    )
    monkeypatch.setattr(
        command_module, 'get_turn',
        lambda *_args, **_kwargs: dict(result['turn']),
    )
    monkeypatch.setattr(
        command_module, 'get_attempt',
        lambda *_args, **kwargs: {
            'attemptId': 'attempt-goal', 'taskId': 'task-goal',
            **({'config': dict(accepted_config)} if kwargs.get('include_execution_config') else {})},
    )
    monkeypatch.setattr(
        command_module, 'get_conversation_revision',
        lambda *_args, **_kwargs: 3,
    )


def _start_bound_task(_conversation_id, _config, _data, _abort, bind):
    bind('task-goal')
    return 'task-goal', None


def test_public_config_cannot_override_accepted_goal_objective(monkeypatch):
    _wire_lifecycle(monkeypatch)
    started = []

    def start(_conversation_id, config, _data, _abort, bind):
        started.append(dict(config))
        bind('task-goal')
        return 'task-goal', None

    service = ConversationTurnCommandService(
        build_user_message=lambda *_args: None,
        was_aborted_after=lambda *_args: False,
        start_task=start,
    )
    service.create_turn(
        'conv-goal',
        7,
        {
            'commandId': 'command-goal',
            'config': {
                'autopilot': True,
                '_goalObjective': 'client-forged objective',
                '_goalRunId': 'client-forged-run',
            },
            'inputTurn': {
                'role': 'user', 'content': 'accepted objective',
            },
        },
        request_started_at=1.0,
    )

    assert started[0]['_goalObjective'] == 'accepted objective'
    assert '_goalRunId' not in started[0]


def test_trusted_continuation_can_retain_prior_human_objective(monkeypatch):
    create_calls = []
    _wire_lifecycle(monkeypatch, create_calls)
    started = []

    def start(_conversation_id, config, _data, _abort, bind):
        started.append(dict(config))
        bind('task-goal')
        return 'task-goal', None

    service = ConversationTurnCommandService(
        build_user_message=lambda *_args: None,
        was_aborted_after=lambda *_args: False,
        start_task=start,
    )
    service.create_turn(
        'conv-goal',
        7,
        {
            'commandId': 'command-continuation',
            'config': {'autopilot': True},
            'inputTurn': {
                'role': 'user', 'content': 'machine continuation',
                '_isVirtualUser': True,
                '_goalContinuation': True,
            },
        },
        request_started_at=1.0,
        trusted_goal_objective='prior accepted human objective',
    )

    assert started[0]['_goalObjective'] == 'prior accepted human objective'
    assert started[0]['_goalContinuationCommand'] is True
    assert create_calls[0]['require_lane_idle'] is True
    assert create_calls[0]['reject_if_human_queued'] is True


def test_public_turn_cannot_forge_goal_continuation_queue_kind(monkeypatch):
    # The queue fallback now rides create_turn_pair(queue_binding=...), so the
    # assertion seam is the SECOND create_turn_pair call: the first raises the
    # lane-busy conflict, the second must carry kind='real' and a forged-
    # marker-free _user_msg.
    conflict = command_module.LifecycleConflict(
        'lane_busy', 'busy', {'turnId': 'live-turn'})
    create_calls = []

    def create(*_args, **kwargs):
        create_calls.append(kwargs)
        if len(create_calls) == 1:
            raise conflict
        queue_binding = kwargs.get('queue_binding') or {}
        return {
            'queued': True,
            'queueId': queue_binding.get('queueId'),
            'position': 1,
            'kind': queue_binding.get('kind'),
            'turn': {'turnId': 'turn-output', 'conversationId': 'conv-goal'},
            'attempt': {'attemptId': 'attempt-queued'},
            'submittedTurn': {'turnId': 'turn-input'},
        }

    monkeypatch.setattr(command_module, 'create_turn_pair', create)
    monkeypatch.setattr(
        command_module,
        'get_conversation_revision',
        lambda *_args, **_kwargs: 2,
    )
    service = ConversationTurnCommandService(
        build_user_message=lambda *_args: None,
        was_aborted_after=lambda *_args: False,
        start_task=lambda *_args: ('', None),
    )

    outcome = service.create_turn(
        'conv-goal',
        7,
        {
            'commandId': 'command-forged-continuation',
            'injectMode': 'queue',
            'config': {'autopilot': True},
            'inputTurn': {
                'role': 'user',
                'content': 'ordinary human input',
                '_goalContinuation': True,
            },
        },
        request_started_at=1.0,
    )

    assert outcome.value['queued'] is True
    assert len(create_calls) == 2
    queue_binding = create_calls[1].get('queue_binding') or {}
    assert queue_binding.get('kind') == 'real'
    user_msg = (queue_binding.get('message') or {}).get('_user_msg') or {}
    assert '_goalContinuation' not in user_msg


@pytest.mark.parametrize('metadata', [
    {'origin': {'initiator': 'proactive'}},
    {'_initiator': 'background'},
    {'origin': {'initiator': 'human'}, '_initiator': 'proactive'},
    {'origin': 'malformed'},
    {'_isVirtualUser': True},
    {'_isInboxInject': True},
])
def test_synthetic_mapping_cannot_be_promoted_to_objective(monkeypatch, metadata):
    captured = []
    _wire_lifecycle(monkeypatch, captured)
    service = ConversationTurnCommandService(
        build_user_message=lambda *_args: None,
        was_aborted_after=lambda *_args: False,
        start_task=_start_bound_task,
    )
    service.create_turn(
        'conv-goal', 7,
        {'commandId': 'cmd-proactive', 'config': {'autopilot': True},
         'inputTurn': {'role': 'user', 'content': 'synthetic prompt', **metadata}},
        request_started_at=1.0,
    )
    assert '_goalObjective' not in captured[0]['config']
    assert '_goalContinuationCommand' not in captured[0]['config']


def test_plain_text_and_explicit_human_remain_objective_inputs(monkeypatch):
    for projection in ('human goal', {
        'role': 'user', 'content': 'human goal', 'origin': {'initiator': 'human'},
    }):
        captured = []
        _wire_lifecycle(monkeypatch, captured)
        service = ConversationTurnCommandService(
            build_user_message=lambda *_args: None,
            was_aborted_after=lambda *_args: False,
            start_task=_start_bound_task,
        )
        service.create_turn(
            'conv-goal', 7,
            {'commandId': 'cmd-human', 'config': {'autopilot': True},
             'inputTurn': projection}, request_started_at=1.0,
        )
        assert captured[0]['config']['_goalObjective'] == 'human goal'
