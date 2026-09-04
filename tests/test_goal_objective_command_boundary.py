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
    def create(*_args, **kwargs):
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
        lambda *_args, **_kwargs: {
            'attemptId': 'attempt-goal', 'taskId': 'task-goal'},
    )
    monkeypatch.setattr(
        command_module, 'get_conversation_revision',
        lambda *_args, **_kwargs: 3,
    )


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
    conflict = command_module.LifecycleConflict(
        'lane_busy', 'busy', {'turnId': 'live-turn'})
    monkeypatch.setattr(
        command_module,
        'create_turn_pair',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(conflict),
    )
    monkeypatch.setattr(
        command_module,
        'get_conversation_revision',
        lambda *_args, **_kwargs: 2,
    )
    queued = []

    def enqueue(*args, **kwargs):
        queued.append((args, kwargs))
        return {'queueId': 'queue-real', 'position': 1, 'kind': kwargs['kind']}

    monkeypatch.setattr('lib.message_queue.enqueue_message', enqueue)
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
    assert queued[0][1]['kind'] == 'real'
    assert '_goalContinuation' not in queued[0][0][1]['_user_msg']
