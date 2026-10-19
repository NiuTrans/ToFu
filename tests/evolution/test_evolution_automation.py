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


def test_terminal_receipts_follow_demand_without_retaining_task_history(monkeypatch):
    import gc
    import threading
    import weakref
    from lib.evolution import automation
    assert automation.stop_terminal_checks(timeout=1)
    automation.start_terminal_checks()
    monkeypatch.setattr('runtime_guards.resolve_resource_budget', lambda *a, **kw: 1)
    entered = threading.Event()
    seen = []
    seen_lock = threading.Lock()
    def blocked(task, *, actuals, cancel_event):
        with seen_lock:
            seen.append((task, actuals))
            if len(seen) == 64:
                entered.set()
        cancel_event.wait(5)
    monkeypatch.setattr(automation, 'on_task_terminal', blocked)
    class Task(dict):
        pass
    try:
        task = Task(_task('done', content='private history' * 100_000))
        source = weakref.ref(task)
        for i in range(64):
            task['id'] = f'terminal-{i}'
            assert automation.enqueue_task_terminal(task)
        assert entered.wait(3)
        assert len(seen) == 64
        assert {receipt['id'] for receipt, _ in seen} == {f'terminal-{i}' for i in range(64)}
        assert all('private history' not in repr(receipt) for receipt, _ in seen)
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


def test_active_check_does_not_block_later_receipt_after_idle_deadline(monkeypatch):
    import threading
    from lib.evolution import automation
    assert automation.stop_terminal_checks(timeout=1)
    automation.start_terminal_checks()
    monkeypatch.setattr(automation, '_IDLE_SECONDS', 0.01)
    held, entered, later = threading.Event(), threading.Event(), threading.Event()
    def check(task, **kwargs):
        if task['id'] == 'held':
            entered.set()
            held.wait(3)
        else:
            later.set()
    monkeypatch.setattr(automation, 'on_task_terminal', check)
    try:
        assert automation.enqueue_task_terminal(_task('done', id='held'))
        assert entered.wait(1)
        assert not later.wait(0.05)
        assert automation.enqueue_task_terminal(_task('done', id='later'))
        assert later.wait(1)
    finally:
        held.set()
        assert automation.stop_terminal_checks(timeout=1)
        automation.start_terminal_checks()


def test_stop_timeout_retains_physical_owner_until_join(monkeypatch):
    import threading
    from lib.evolution import automation
    assert automation.stop_terminal_checks(timeout=1)
    automation.start_terminal_checks()
    entered, finish = threading.Event(), threading.Event()
    def check(*args, **kwargs):
        entered.set()
        finish.wait(3)
    monkeypatch.setattr(automation, 'on_task_terminal', check)
    try:
        assert automation.enqueue_task_terminal(_task('done'))
        assert entered.wait(1)
        owner = automation._worker
        assert not automation.stop_terminal_checks(timeout=0.01)
        assert automation._worker is owner and owner.is_alive()
        automation.start_terminal_checks()
        assert not automation.enqueue_task_terminal(_task('done', id='late'))
    finally:
        finish.set()
        assert automation.stop_terminal_checks(timeout=1)
        automation.start_terminal_checks()
    assert automation._worker is None


def test_legacy_terminal_binding_is_found_after_old_scan_limit(store, bound):
    from copy import deepcopy
    from lib.evolution import automation, get_episode
    from lib.evolution.records import NAMESPACE_EPISODES, entity_key
    original_key = entity_key(bound['episode_id'], USER, kind='episode')
    store.rows.pop((NAMESPACE_EPISODES, original_key))
    for i in range(205):
        episode = deepcopy(bound)
        episode.update(episode_id=f'ep-{i:04d}', task_id=f'unrelated-{i}')
        store.rows[(NAMESPACE_EPISODES, entity_key(episode['episode_id'], USER, kind='episode'))] = episode
    target = deepcopy(bound)
    target['episode_id'] = 'ep-zz-target'
    store.rows[(NAMESPACE_EPISODES, entity_key(target['episode_id'], USER, kind='episode'))] = target
    for key in store.rows:
        if key[0] == NAMESPACE_EPISODES:
            store.versions[key] = 1
    automation.on_task_terminal(_task('error'))
    assert get_episode(target['episode_id'], user_id=USER)['status'] == 'aborted'
    assert all(get_episode(f'ep-{i:04d}', user_id=USER)['status'] == 'running' for i in range(205))
