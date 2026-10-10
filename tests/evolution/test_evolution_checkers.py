"""Criteria checkers: machine verdicts, human fallback, bounded commands."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _goal(criteria):
    return {'goal_id': 'g', 'criteria': criteria}


def test_all_pass_is_success_and_fail_mix_is_partial_or_failure():
    from lib.evolution.checkers import evaluate_goal
    goal = _goal([
        {'criterion_id': 'a', 'kind': 'test_command', 'params': {'command': 'true'}},
        {'criterion_id': 'b', 'kind': 'test_command', 'params': {'command': 'true'}},
    ])
    verdict = evaluate_goal(goal, user_id=12)
    assert verdict['outcome'] == 'success'
    assert verdict['assessed_by'] == 'checker'

    mixed = _goal([
        {'criterion_id': 'a', 'kind': 'test_command', 'params': {'command': 'true'}},
        {'criterion_id': 'b', 'kind': 'test_command', 'params': {'command': 'false'}},
    ])
    assert evaluate_goal(mixed, user_id=12)['outcome'] == 'partial'

    failing = _goal([
        {'criterion_id': 'a', 'kind': 'test_command', 'params': {'command': 'false'}},
    ])
    assert evaluate_goal(failing, user_id=12)['outcome'] == 'failure'


def test_unknown_kinds_and_bad_params_wait_for_a_human():
    from lib.evolution.checkers import evaluate_goal
    human = _goal([
        {'criterion_id': 'a', 'kind': 'explicit_human', 'params': {}},
    ])
    assert evaluate_goal(human, user_id=12) is None
    broken = _goal([
        {'criterion_id': 'a', 'kind': 'test_command', 'params': {'command': ''}},
    ])
    assert evaluate_goal(broken, user_id=12) is None
    missing_binary = _goal([
        {'criterion_id': 'a', 'kind': 'test_command',
         'params': {'command': 'definitely-not-a-binary-xyz'}},
    ])
    assert evaluate_goal(missing_binary, user_id=12) is None


def test_runner_is_injectable_and_evidence_is_bounded():
    from lib.evolution import checkers
    seen = []

    def fake_runner(params):
        seen.append(params['command'])
        return 'pass', 'x' * 9999

    goal = _goal([
        {'criterion_id': 'a', 'kind': 'test_command',
         'params': {'command': 'whatever'}}])
    verdict = checkers.evaluate_goal(goal, runner=fake_runner)
    assert seen == ['whatever']
    assert verdict['outcome'] == 'success'
    assert len(verdict['criterion_results'][0]['evidence_ref']) <= 500


@pytest.mark.parametrize('owner', [None, 0, True])
def test_checker_requires_explicit_owner_before_spawn(monkeypatch, owner):
    from lib.evolution import checkers
    monkeypatch.setattr(checkers, 'run_bounded_process',
                        lambda *a, **kw: pytest.fail('spawned without owner'))
    assert checkers.evaluate_goal(_goal([{'criterion_id': 'a', 'kind': 'test_command',
        'params': {'command': 'true'}}]), user_id=owner) is None


def test_checker_cumulative_deadline_stops_later_criteria(monkeypatch):
    from lib.evolution import checkers
    clock = [0.0]
    monkeypatch.setattr(checkers.time, 'monotonic', lambda: clock[0])
    calls = []
    def slow(params):
        calls.append(params)
        clock[0] += 301
        return 'pass', ''
    goal = _goal([{'criterion_id': str(i), 'kind': 'test_command',
                   'params': {'command': 'true'}} for i in range(2)])
    assert checkers.evaluate_goal(goal, runner=slow) is None
    assert len(calls) == 1
