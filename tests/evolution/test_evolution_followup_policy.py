"""A linked coding iteration retains workspace, checks and explicit limits."""
from copy import deepcopy

import pytest

from lib.evolution import coordination, coordinator
from tests.evolution.test_evolution_coordination import world as world

pytestmark = pytest.mark.unit


def test_followup_preserves_execution_scope_and_checks_without_spent_authority(world, monkeypatch):
    _, run, _ = world
    policy = {
        'project_path': '/owned/project', 'executor_plugins': [],
        'executor_schema_budget_tokens': 4000,
        'budget': {'max_tokens': 2400, 'max_tool_calls': 30, 'max_wall_clock_seconds': 90},
        'review_reserve': {'tokens': 400, 'tool_calls': 5, 'wall_clock_seconds': 15},
        'max_attempts': 2, 'max_review_format_retries': 1,
        'acceptance_requirements': ['All regression tests pass.'],
        'require_requirement_originals': True, 'require_command_execution': True,
    }
    run.update(status='completed', **deepcopy(policy), absolute_deadline_at=1,
               actuals={'tokens': 1200}, episode_id='old-episode',
               scientific_protocol={'prior': 'not a new preregistration'})
    calls = []

    def submit(payload, **kwargs):
        calls.append((deepcopy(payload), kwargs))
        payload['budget']['max_tokens'] = 0  # Caller cannot mutate the parent.
        return {'run_id': 'next'}

    monkeypatch.setattr('lib.evolution.runs.submit_run', submit)
    body = {'kind': 'guidance', 'content': 'Extend the verified implementation.'}
    first = coordinator.start_followup('a', body, user_id=12, request_id='iteration')
    coordination.publish('a', {'kind': 'experiment', 'content': 'Late source report'},
                         user_id=12, event_id='late')
    second = coordinator.start_followup('a', body, user_id=12, request_id='iteration')
    assert first == second == {'run_id': 'next'}
    assert calls[0] == calls[1]
    payload, kwargs = calls[0]
    assert all(payload[key] == value for key, value in policy.items())
    assert run['budget'] == policy['budget']
    assert kwargs['user_id'] == 12 and kwargs['parent_run_id'] == 'a'
    assert not {'absolute_deadline_at', 'actuals', 'episode_id', 'task_id',
                'scientific_protocol'} & payload.keys()


def test_followup_without_project_does_not_invent_execution_scope(world, monkeypatch):
    world[1]['status'] = 'completed'
    calls = []
    monkeypatch.setattr('lib.evolution.runs.submit_run',
                        lambda payload, **kw: calls.append(payload) or {'run_id': 'next'})
    coordinator.start_followup('a', {'kind': 'guidance', 'content': 'Explain further.'},
                               user_id=12, request_id='explain')
    assert not {'project_path', 'executor_plugins', 'budget', 'review_reserve'} & calls[0].keys()
