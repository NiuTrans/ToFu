"""Auxiliary summaries reuse physical grant admission; no provider/network calls."""
from copy import deepcopy

import pytest

from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from tests.project_mod.test_coordinator_request_budget import bind
from tests.project_mod.test_coordinator_continuation import run as run, ready, activate
from tests.tasks_pkg.test_live_summary_budget import summary_env as summary_env, result
from lib.tasks_pkg.compaction._layer2 import _summary as summary
from lib.llm.llm_errors import AbortedError, ModelAdmissionRefused

pytestmark = pytest.mark.unit


@pytest.fixture
def summary_runtime(run, summary_env, monkeypatch):
    r = run
    bind(r)
    ready(r)
    row = activate(r)
    r.task['model'] = 'model-a'
    dispatch, _, calls, _, _ = summary_env
    monkeypatch.setattr('lib.tasks_pkg.compaction._summary_routing.select_summary_route',
                        lambda task, **kw: {'modelId': 'model-a'})

    def stream(messages, **kwargs):
        # The real transport owns exactly this count/reserve/mark/settle protocol.
        # Substituting only provider transport keeps both durable observers real.
        lease = kwargs['model_budget_admit'](input_tokens=40, max_output_tokens=100)
        try:
            lease.mark_dispatched()
            calls.append(kwargs)
            response = result('### Summary\nThe workers delivered verified evidence.')
            response.usage.update(prompt_tokens=40, completion_tokens=20)
            return response
        finally:
            lease.settle({'prompt_tokens': 40, 'completion_tokens': 20})

    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    return r, row, calls


def test_continuation_summary_charges_existing_grant_and_shared_request(summary_runtime):
    r, row, calls = summary_runtime
    account = r.task['_executionBudget']
    before = deepcopy(r.c.repo.read(row['id']))
    source = deepcopy(r.turns['human-turn'])
    config = deepcopy(r.task['config'])
    binding = deepcopy(r.c.binding.value)
    assert summary._generate_query_aware_summary([], 'continue', task=r.task)
    after = r.c.repo.read(row['id'])
    assert len(calls) == 1 and account.consumption()[0] == 60
    assert r.task['_executionBudget'] is account
    assert after['grant'] == before['grant']
    assert after['request'] == before['request']
    assert after['toolReservations'] == before['toolReservations']
    assert after['modelAttempts'][0]['account_nonce'] == before['toolReservations']['accountNonce']
    assert after['modelAttempts'][0]['reported_tokens'] == 60
    budget_id = row['request']['delegation']['policy']['requestBudgetId']
    state = r.c.repo._load()[0]
    from lib.project_dispatch.request_budget import usage as request_usage
    pool = next(item for item in state['requestBudgets'] if item['id'] == budget_id)
    usage = request_usage(r.c.repo, state, budget_id)
    assert usage['tokens'] == 60 and usage['modelRequests'] == 1 and usage['usageComplete']
    assert len(pool['accounts']) == 1  # No new source account or replenishment.
    assert r.turns['human-turn'] == source and r.c.binding.value == binding
    assert r.task['config'] == config


@pytest.mark.parametrize('violation', ['missing-account', 'foreign-account', 'withdrawn'])
def test_invalid_coordinator_summary_never_reaches_provider(summary_runtime, violation):
    r, row, calls = summary_runtime
    if violation == 'missing-account':
        r.task.pop('_executionBudget')
    elif violation == 'foreign-account':
        from lib.agent_core.execution_budget import ExecutionBudget
        r.task['_executionBudget'] = ExecutionBudget({'id': 'unrelated-account'})
    else:
        r.c.repo.revoke(row['id'], expected_revision=1)
    before = deepcopy(r.c.repo._load()[0])
    with pytest.raises((AbortedError, PermissionError, ValueError)):
        summary._generate_query_aware_summary([], 'continue', task=r.task)
    assert not calls and r.c.repo._load()[0] == before


def test_account_swapped_after_summary_preparation_is_rejected(summary_runtime, monkeypatch):
    r, _, calls = summary_runtime
    dispatch = __import__('lib.llm_dispatch', fromlist=['dispatch_stream'])
    original = dispatch.dispatch_stream

    def changed(messages, **kwargs):
        from lib.agent_core.execution_budget import ExecutionBudget
        r.task['_executionBudget'] = ExecutionBudget({'id': 'unrelated-account'})
        return original(messages, **kwargs)

    monkeypatch.setattr(dispatch, 'dispatch_stream', changed)
    assert summary._generate_query_aware_summary([], 'continue', task=r.task) is None
    assert not calls


def test_original_request_summary_uses_already_registered_source_account(runtime, summary_env, monkeypatch):
    r = runtime
    bind(r)
    dispatch, _, calls, _, _ = summary_env
    monkeypatch.setattr('lib.tasks_pkg.compaction._summary_routing.select_summary_route',
                        lambda task, **kw: {'modelId': 'model-a'})
    account = r.task['_executionBudget']

    def stream(messages, **kwargs):
        lease = kwargs['model_budget_admit'](input_tokens=40, max_output_tokens=100)
        lease.mark_dispatched()
        lease.settle({'prompt_tokens': 40, 'completion_tokens': 20})
        calls.append(kwargs)
        response = result('### Summary\nContinue the same request.')
        response.usage.update(prompt_tokens=40, completion_tokens=20)
        return response

    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    assert summary._generate_query_aware_summary([], 'continue', task=r.task)
    pool = r.c.repo._load()[0]['requestBudgets'][0]
    assert len(calls) == len(pool['modelAttempts']) == len(pool['accounts']) == 1
    assert pool['modelAttempts'][0]['account_nonce'] == pool['accounts'][0]['nonce']
    assert pool['modelAttempts'][0]['reported_tokens'] == 60
    assert r.task['_executionBudget'] is account


def test_coordinator_turn_without_request_account_summarizes_on_its_task_account(
        runtime, summary_env, monkeypatch):
    """Regression for coordinator-1a390889: a retried continuation Turn died at compaction.

    Synthetic input (a continuation brief, report relay or channel event) opens
    no request account, and the summary refusal then settled the Turn as a
    silent user Stop before any model round. Its summaries charge the canonical
    task account its model rounds use, and still open no request account.
    """
    from lib.agent_core.execution_budget import ExecutionBudget
    r = runtime
    r.c.turn['actor'] = 'virtual_user'
    assert bind(r) is None
    assert r.task.get('_coordinatorDispatchScope') and r.task.get('_executionBudget') is None
    dispatch, _, calls, _, _ = summary_env
    monkeypatch.setattr('lib.tasks_pkg.compaction._summary_routing.select_summary_route',
                        lambda task, **kw: {'modelId': 'model-a'})
    r.task['model'] = 'model-a'

    def stream(messages, **kwargs):
        lease = kwargs['model_budget_admit'](input_tokens=40, max_output_tokens=100)
        lease.mark_dispatched()
        lease.settle({'prompt_tokens': 40, 'completion_tokens': 20})
        calls.append(kwargs)
        response = result('### Summary\nContinue the original project request.')
        response.usage.update(prompt_tokens=40, completion_tokens=20)
        return response

    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    assert summary._generate_query_aware_summary([], 'continue', task=r.task)
    account = r.task['_executionBudget']
    assert isinstance(account, ExecutionBudget) and account.consumption()[0] == 60
    assert len(calls) == 1 and not r.task.get('aborted')
    assert r.task.get('_coordinatorRequestBudget') is None
    assert not r.c.repo._load()[0].get('requestBudgets')


def test_missing_bound_request_account_is_a_stated_refusal_not_a_stop(runtime):
    """A bare AbortedError settles the Turn as a silent user Stop at its compaction row."""
    from lib.tasks_pkg.compaction._summary_accounting import summary_model_admission
    r = runtime
    assert bind(r)
    r.task.pop('_executionBudget')
    with pytest.raises(ModelAdmissionRefused, match='execution account'):
        summary_model_admission(r.task)
    assert not r.task.get('aborted')


def test_repeated_summaries_record_on_the_unchanged_ceiling_free_grant(summary_runtime):
    from lib.project_dispatch.request_budget import usage as request_usage
    r, row, calls = summary_runtime
    assert row['request']['budget'] == {}
    for number in range(3):
        assert summary._generate_query_aware_summary([], f'new evidence {number}', task=r.task)
    after = r.c.repo.read(row['id'])
    assert len(calls) == 3 and after['request'] == row['request']
    budget_id = row['request']['delegation']['policy']['requestBudgetId']
    usage = request_usage(r.c.repo, r.c.repo._load()[0], budget_id)
    assert usage['modelRequests'] == 3 and usage['tokens'] == 180


def test_unknown_summary_usage_is_a_lower_bound_and_never_blocks_the_next_call(summary_runtime, monkeypatch):
    r, row, calls = summary_runtime

    def failed(messages, **kwargs):
        lease = kwargs['model_budget_admit'](input_tokens=40, max_output_tokens=100)
        lease.mark_dispatched()
        calls.append(kwargs)
        lease.settle(usage_complete=False)
        raise OSError('provider reply lost')

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', failed)
    assert summary._generate_query_aware_summary([], 'continue', task=r.task) is None
    assert summary._generate_query_aware_summary([], 'different evidence', task=r.task) is None
    attempts = r.c.repo.read(row['id'])['modelAttempts']
    assert len(calls) == len(attempts) == 2
    assert all(attempt['reserved_tokens'] == 140 and attempt['usage_complete'] is False
               for attempt in attempts)
    from lib.project_dispatch.request_budget import usage as request_usage
    state = r.c.repo._load()[0]
    usage = request_usage(r.c.repo, state, row['request']['delegation']['policy']['requestBudgetId'])
    assert usage['modelRequests'] == 2 and not usage['usageComplete']


@pytest.fixture
def worker_summary(runtime, summary_env, monkeypatch):
    from tests.project_mod.test_coordinator_workers import _worker_task
    worker, row = _worker_task(runtime)
    worker['model'] = row['request']['model']
    dispatch, _, calls, _, _ = summary_env
    monkeypatch.setattr('lib.tasks_pkg.compaction._summary_routing.select_summary_route',
                        lambda task, **kw: {'modelId': row['request']['model']})

    def stream(messages, **kwargs):
        lease = kwargs['model_budget_admit'](input_tokens=40, max_output_tokens=100)
        try:
            lease.mark_dispatched()
            calls.append(kwargs)
            response = result('### Summary\nThe worker verified the migration.')
            response.usage.update(prompt_tokens=40, completion_tokens=20)
            return response
        finally:
            lease.settle({'prompt_tokens': 40, 'completion_tokens': 20})

    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    return runtime, worker, row, calls


def test_worker_summary_charges_only_its_own_grant(worker_summary):
    r, worker, row, calls = worker_summary
    account = worker['_executionBudget']
    before = deepcopy(r.c.repo.read(row['id']))
    others = {item['id']: deepcopy(item) for item in r.c.repo.list() if item['id'] != row['id']}
    source = deepcopy(r.turns['human-turn'])
    assert summary._generate_query_aware_summary([], 'continue', task=worker)
    after = r.c.repo.read(row['id'])
    assert len(calls) == 1 and account.consumption()[0] == 60
    assert worker['_executionBudget'] is account and not worker.get('aborted')
    assert after['grant'] == before['grant'] and after['request'] == before['request']
    assert [attempt['reported_tokens'] for attempt in after['modelAttempts']] == [60]
    # The delegating Coordinator gains nothing: no other grant or source changes.
    assert {item['id']: item for item in r.c.repo.list() if item['id'] != row['id']} == others
    assert r.turns['human-turn'] == source


@pytest.mark.parametrize('violation', ['missing-account', 'foreign-account', 'withdrawn'])
def test_invalid_worker_summary_never_reaches_provider(worker_summary, violation):
    r, worker, row, calls = worker_summary
    if violation == 'missing-account':
        worker.pop('_executionBudget')
    elif violation == 'foreign-account':
        from lib.agent_core.execution_budget import ExecutionBudget
        worker['_executionBudget'] = ExecutionBudget({'id': 'unrelated-account'})
    else:
        r.c.repo.revoke(row['id'], expected_revision=row['grant']['revision'])
    before = deepcopy(r.c.repo._load()[0])
    with pytest.raises((AbortedError, PermissionError, ValueError)):
        summary._generate_query_aware_summary([], 'continue', task=worker)
    assert not calls and r.c.repo._load()[0] == before
    assert worker['aborted'] is True  # Authority loss stops the worker, not just the summary.


def test_changed_noncoordinator_carrier_is_rejected_at_physical_admission(summary_runtime):
    from lib.project_dispatch.admission import KEY
    from lib.tasks_pkg.compaction._summary_accounting import summary_model_admission
    r, row, calls = summary_runtime
    admit = summary_model_admission(r.task)
    account = r.task['_executionBudget']
    worker = next(item for item in r.c.repo.list() if item['request']['profile']['mode'] == 'project')
    # A different otherwise-valid carrier can classify as a worker before the
    # continuation helper reaches validate_task. None is not continuation proof.
    r.task['config'][KEY] = r.c.repo.admission(worker['id'])
    with pytest.raises(AbortedError, match='Project dispatch|dispatch grant'):
        admit(input_tokens=40, max_output_tokens=100)
    assert r.task['_executionBudget'] is account and not calls
    assert not r.c.repo.read(row['id'])['modelAttempts']


def test_nonstream_summary_receives_same_physical_account_port(summary_runtime, monkeypatch):
    r, row, calls = summary_runtime
    r.task.pop('aborted', None)

    def chat(messages, **kwargs):
        lease = kwargs['model_budget_admit'](input_tokens=40, max_output_tokens=100)
        lease.mark_dispatched()
        usage = {'prompt_tokens': 40, 'completion_tokens': 20}
        lease.settle(usage)
        calls.append(kwargs)
        return '### Summary\nVerified same-grant summary.', {**usage, 'finish_reason': 'stop'}

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', chat)
    assert summary._generate_query_aware_summary([], 'continue', task=r.task)
    attempts = r.c.repo.read(row['id'])['modelAttempts']
    assert len(calls) == len(attempts) == 1 and attempts[0]['reported_tokens'] == 60
