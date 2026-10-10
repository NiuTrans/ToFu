"""Terminal listener: bound tasks settle their episode on the engine's path."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


@pytest.fixture
def bound(store):
    """One admitted goal, confirmed plan, running episode bound to task-1."""
    from lib import evolution
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': 'goal-1',
        'statement': 'make the check pass', 'domain': 'code',
        'exploratory': False,
        'criteria': [{'criterion_id': 'c1', 'kind': 'test_command',
                      'params': {'command': 'true'}}],
        'budget_envelope': {'max_tokens': 100, 'max_wall_clock_seconds': 10,
                            'max_tool_calls': 1},
        'status': 'draft', 'created_at': 1}, user_id=USER)
    evolution.save_plan({
        'schema_version': 1, 'plan_id': 'plan-1', 'goal_id': 'goal-1',
        'plan_revision': 1,
        'candidates': [{
            'path_id': 'path-a', 'title': 'P',
            'stages': [{'kind': 'execute', 'detail': 'd'}],
            'resource_refs': [],
            'cost_estimate': {
                'expected_tokens': 10, 'expected_wall_clock_seconds': 10,
                'expected_tool_calls': 1, 'failure_rate_prior': 0.5,
                'expected_information_gain': 0.5, 'expected_utility': 0.5,
                'priors_source': 'exploratory_flat'},
            'utility_over_cost': 1.0}],
        'selected_path_id': 'path-a', 'selection_rationale': 'r',
        'requires_confirmation': True, 'status': 'confirmed',
        'created_at': 1}, user_id=USER)
    episode = evolution.open_episode(goal_id='goal-1', plan_id='plan-1',
                                     selected_path_id='path-a',
                                     task_id='task-1', user_id=USER)
    return episode


def _task(status, **overrides):
    task = {
        'id': 'task-1', '_userId': USER, 'status': status,
        'created_at': 10.0, 'finished_at': 25.0,
        'usage': {'prompt_tokens': 5, 'completion_tokens': 3},
        'toolRounds': [{'tool_calls': [{'name': 'x'}]}],
        'config': {'evolution': {'goal_id': 'goal-1', 'plan_id': 'plan-1'}},
    }
    task.update(overrides)
    return task


def test_done_task_with_checkable_criteria_settles_success(store, bound):
    from lib import evolution
    from lib.evolution import automation
    automation.on_task_terminal(_task('done'))
    settled = evolution.get_episode(bound['episode_id'], user_id=USER)
    assert settled['status'] == 'success'
    assert settled['verdict']['assessed_by'] == 'checker'
    assert settled['actuals'] == {'tokens': 8, 'wall_clock_seconds': 15,
                                  'tool_calls': 1}


def test_error_task_settles_aborted_without_verdict(store, bound):
    from lib import evolution
    from lib.evolution import automation
    automation.on_task_terminal(_task('error'))
    settled = evolution.get_episode(bound['episode_id'], user_id=USER)
    assert settled['status'] == 'aborted'
    assert settled['verdict'] is None


def test_done_task_with_human_criteria_waits_for_explicit_verdict(store, bound):
    from lib import evolution
    from lib.evolution import automation
    goal = evolution.get_goal('goal-1', user_id=USER)
    goal['criteria'] = [{'criterion_id': 'c1', 'kind': 'explicit_human',
                         'params': {}}]
    from lib.evolution import records
    records.put_record(records.NAMESPACE_GOALS, f'{USER}:goal-1', goal,
                       kind='goal')
    automation.on_task_terminal(_task('done'))
    assert evolution.get_episode(bound['episode_id'],
                                 user_id=USER)['status'] == 'running'


def test_settlement_follows_goal_binding_then_durable_task_link(store, bound):
    """Config binding selects the listener; the episode's task_id closes it."""
    from lib import evolution
    from lib.evolution import automation
    # Launched shape: goal/plan in config without episode_id — the running
    # episode is found by its durable task_id link.
    automation.on_task_terminal(_task('done', config={
        'evolution': {'goal_id': 'goal-1', 'plan_id': 'plan-1'}}))
    assert evolution.get_episode(bound['episode_id'],
                                 user_id=USER)['status'] == 'success'


def test_unbound_foreign_and_unrelated_tasks_are_ignored(store, bound):
    from lib import evolution
    from lib.evolution import automation
    automation.on_task_terminal(_task('done', config={}))
    assert evolution.get_episode(bound['episode_id'],
                                 user_id=USER)['status'] == 'running'
    automation.on_task_terminal(_task('done', _userId=99))
    assert evolution.get_episode(bound['episode_id'],
                                 user_id=USER)['status'] == 'running'
    automation.on_task_terminal(_task('done', id='task-unrelated'))
    assert evolution.get_episode(bound['episode_id'],
                                 user_id=USER)['status'] == 'running'


def test_install_registers_once_on_the_engine_settlement_path(store):
    from lib.evolution import automation
    from lib.tasks_pkg.manager import _terminal
    automation._installed = False
    try:
        assert automation.install() is True
        assert automation.install() is False
        assert _terminal._TERMINAL_LISTENERS.count(
            automation.enqueue_task_terminal) == 1
    finally:
        _terminal.unregister_terminal_listener(automation.enqueue_task_terminal)
        automation._installed = False


def test_terminal_queue_bounds_receipts_and_never_retains_task_history(monkeypatch):
    import gc
    import threading
    import weakref
    from lib.evolution import automation
    assert automation.stop_terminal_checks(timeout=1)
    automation.start_terminal_checks()
    monkeypatch.setattr(automation, '_queue_capacity', 2)
    entered = threading.Event()
    seen = []
    def blocked(task, *, actuals, cancel_event):
        seen.append((task, actuals))
        entered.set()
        cancel_event.wait(2)
    monkeypatch.setattr(automation, 'on_task_terminal', blocked)
    class Task(dict):
        pass
    try:
        task = Task(_task('done', content='private history' * 100_000))
        source = weakref.ref(task)
        assert automation.enqueue_task_terminal(task)
        assert entered.wait(1)
        assert automation.enqueue_task_terminal(task)
        assert automation.enqueue_task_terminal(task)
        assert not automation.enqueue_task_terminal(task)
        assert len(automation._pending) == 2
        assert len(repr(automation._pending)) < 4096
        assert 'private history' not in repr(automation._pending)
        del task
        gc.collect()
        assert source() is None
        assert seen[0][1] == {'tokens': 8, 'wall_clock_seconds': 15, 'tool_calls': 1}
    finally:
        assert automation.stop_terminal_checks(timeout=1)
        automation.start_terminal_checks()
    assert not automation._pending
    assert automation._worker is None


def test_terminal_worker_retires_when_idle(monkeypatch):
    import threading
    from lib.evolution import automation
    entered = threading.Event()
    assert automation.stop_terminal_checks(timeout=1)
    automation.start_terminal_checks()
    monkeypatch.setattr(automation, '_IDLE_SECONDS', 0.01)
    monkeypatch.setattr(automation, 'on_task_terminal', lambda *a, **kw: entered.set())
    try:
        assert automation.enqueue_task_terminal(_task('done'))
        worker = automation._worker
        assert entered.wait(1)
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert automation._worker is None
    finally:
        automation.stop_terminal_checks(timeout=1)
        automation.start_terminal_checks()


def test_explicit_episode_binding_cannot_settle_another_task(store, bound):
    from lib import evolution
    from lib.evolution import automation
    automation.on_task_terminal(_task('error', id='different-task', config={
        'evolution': {'episode_id': bound['episode_id']}}))
    assert evolution.get_episode(bound['episode_id'], user_id=USER)['status'] == 'running'


def test_unsettled_physical_request_marks_terminal_consumption_incomplete(store, bound):
    from lib.evolution import automation, get_episode
    from lib.agent_core.execution_budget import ExecutionBudget
    task = _task('running')
    budget = ExecutionBudget(task)
    task['_executionBudget'] = budget
    lease = budget.reserve_model(task, input_tokens=20, max_output_tokens=30)
    lease.mark_dispatched()
    task['status'] = 'error'
    automation.on_task_terminal(task)
    settled = get_episode(bound['episode_id'], user_id=USER)
    assert settled['status'] == 'aborted' and settled['actuals_complete'] is False
    assert settled['actuals']['tokens'] == 8
    lease.settle({'input_tokens': 20, 'output_tokens': 5})
    assert get_episode(bound['episode_id'], user_id=USER)['actuals_complete'] is False
