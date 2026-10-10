"""Explicit review allocation preserves original cumulative authorization."""
from copy import deepcopy
import time

import pytest

from tests.evolution.test_evolution_runs import OWNER, finish, review, runtime as runtime
from tests.evolution.test_evolution_programs import runtime as _program_runtime_fixture
program_runtime = _program_runtime_fixture

pytestmark = pytest.mark.unit
POLICY = {'budget': {'max_tokens': 240, 'max_tool_calls': 10, 'max_wall_clock_seconds': 120},
          'review_reserve': {'tokens': 70, 'tool_calls': 3, 'wall_clock_seconds': 30}}


def test_executing_slice_and_review_use_same_cumulative_budget(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Explain the observed result.', **POLICY}, user_id=OWNER)
    executor = tasks[0]
    assert executor['config']['executionLimits'] == {'max_tokens': 170, 'max_tool_calls': 7,
                                                    'deadline': run['created_at'] + 90}
    assert abs(executor['_invocationDeadline'] - time.monotonic() -
               (run['created_at'] + 90 - time.time())) < 1
    assert 'Final artifact declaration' in executor['messages'][0]['content']
    current = finish(runtime, executor, 'Observed explanation.')
    assert current['status'] == 'reviewing'
    assert current['actuals']['tokens'] == 15
    assert tasks[-1]['config']['executionLimits'] == {'max_tokens': 225, 'max_tool_calls': 0,
                                                     'deadline': run['created_at'] + 120}
    completed = finish(runtime, tasks[-1], review())
    assert completed['status'] == 'completed'
    assert completed['actuals']['tokens'] == 30
    assert completed['budget'] == POLICY['budget']
    assert runs.get_run(run['run_id'], user_id=OWNER)['review_reserve'] == POLICY['review_reserve']


def test_full_source_reviewer_receives_actual_remaining_tools(runtime):
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'Inspect the whole explanation.', **POLICY}, user_id=OWNER)
    current = finish(runtime, tasks[0], 'Complete original explanation. ' * 1000)
    assert current['status'] == 'reviewing' and current['result_source']['total_chars'] > 24000
    assert tasks[-1]['config']['executionLimits']['max_tool_calls'] == 10
    assert tasks[-1]['config']['toolSpecAllowlist'] == ['evolution_source', 'evolution_review']
    assert tasks[-1]['config']['executionLimits']['max_tokens'] == 225


def test_provider_overrun_uses_actual_remaining_without_topping_up_reserve(runtime):
    runs, _, tasks = runtime
    original = runs.submit_run({'objective': 'Preserve observed overrun.', **POLICY}, user_id=OWNER)
    executor = tasks[0]
    executor.update(status='done', finishReason='stop', content='Original final response.',
                    finished_at=executor['created_at'] + 1, usage={'input_tokens': 175, 'output_tokens': 5})
    current = runs.advance_run(original['run_id'], user_id=OWNER, terminal_task=executor)
    assert current['status'] == 'reviewing' and current['actuals']['tokens'] == 180
    assert tasks[-1]['config']['executionLimits']['max_tokens'] == 60
    assert current['review_reserve']['tokens'] == 70 and current['budget']['max_tokens'] == 240


def test_executing_slice_is_enforced_by_existing_request_and_tool_admission(runtime):
    from lib.agent_core.execution_budget import ExecutionBudget, ModelBudgetExceeded
    from lib.agent_core.execution_limits import admit_tool_execution
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'Use the original admission ports.', **POLICY}, user_id=OWNER)
    executor = tasks[0]
    account = ExecutionBudget(executor)
    lease = account.reserve_model(executor, input_tokens=169, max_output_tokens=20)
    assert lease.max_output_tokens == 1
    lease.settle(unsent=True)
    with pytest.raises(ModelBudgetExceeded, match='execution_token_budget_exhausted'):
        account.reserve_model(executor, input_tokens=170, max_output_tokens=20)
    assert [admit_tool_execution(executor) for _ in range(7)] == [None] * 7
    assert admit_tool_execution(executor) == 'execution_tool_budget_exhausted'
    assert executor['_executionToolsAdmitted'] == 7


def test_repair_reuses_reserve_without_resetting_consumption(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Keep original checks through repair.', **POLICY}, user_id=OWNER)
    finish(runtime, tasks[-1], 'Incomplete first explanation.')
    current = finish(runtime, tasks[-1], review('fail'))
    assert current['status'] == 'executing'
    assert current['actuals']['tokens'] == 30
    assert tasks[-1]['config']['executionLimits']['max_tokens'] == 140
    assert tasks[-1]['config']['executionLimits']['deadline'] == run['created_at'] + 90
    assert current['budget'] == POLICY['budget']
    assert len(current['receipts']) == 2


def test_unknown_usage_cannot_release_review_reserve(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Do not refund unknown usage.', **POLICY}, user_id=OWNER)
    tasks[0]['_executionActualsComplete'] = False
    current = finish(runtime, tasks[0], 'Uncertain original consumption.')
    assert current['status'] == 'needs_attention'
    assert current['error'] == 'execution_token_usage_unknown'
    assert current['actuals_complete'] is False
    assert current['review'] is None and len(tasks) == 1
    assert current['budget'] == run['budget']


def test_slice_exhaustion_does_not_promote_work_or_mint_review(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Declare and verify required files.', **POLICY}, user_id=OWNER)
    tasks[0]['error'] = 'execution_token_budget_exhausted'
    current = finish(runtime, tasks[0], '', status='error')
    assert current['status'] != 'completed' and current['review'] is None
    assert len(tasks) == 1 and current['budget'] == run['budget']
    assert not current.get('artifact_evidence_source')


@pytest.mark.parametrize('reserve,budget', [
    ({'tokens': 1}, {}), ({'tokens': 240}, POLICY['budget']),
    ({'tokens': 241}, POLICY['budget']), ({'tool_calls': 10}, POLICY['budget']),
    ({'wall_clock_seconds': 120}, POLICY['budget']), ({'tokens': True}, POLICY['budget']),
    ({'tokens': -1}, POLICY['budget']), ({'tokens': 1.5}, POLICY['budget']),
    ({'tokens': None}, POLICY['budget']), ({'foreign': 1}, POLICY['budget']),
    (None, POLICY['budget']), ([], POLICY['budget']),
])
def test_invalid_reserve_refuses_before_any_birth_or_store_write(runtime, reserve, budget):
    runs, store, tasks = runtime
    before = store.writes
    with pytest.raises(runs.EvolutionContractError):
        runs.submit_run({'objective': 'Invalid policy.', 'budget': budget,
                         'review_reserve': reserve}, user_id=OWNER)
    assert store.writes == before and not tasks


def test_reserve_is_immutable_under_idempotency(runtime):
    runs, _, tasks = runtime
    request = {'objective': 'Immutable review allocation.', **POLICY}
    original = runs.submit_run(request, user_id=OWNER, idempotency_key='reserve-policy')
    assert runs.submit_run(request, user_id=OWNER, idempotency_key='reserve-policy') == original
    with pytest.raises(runs.EvolutionStateError):
        runs.submit_run({**request, 'review_reserve': {'tokens': 71}}, user_id=OWNER,
                        idempotency_key='reserve-policy')
    assert len(tasks) == 1


@pytest.mark.parametrize('reserve', [{}, {'tokens': 0, 'tool_calls': 0, 'wall_clock_seconds': 0}])
def test_empty_and_zero_reserves_preserve_unlimited_execution(runtime, reserve):
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'No artificial default ceiling.', 'review_reserve': reserve}, user_id=OWNER)
    assert tasks[0]['config']['executionLimits'] == {'max_tokens': None, 'max_tool_calls': None, 'deadline': None}
    assert 'Review reserve within' not in tasks[0]['messages'][0]['content']


def test_absolute_program_deadline_is_shortened_only_for_executor(runtime):
    runs, _, tasks = runtime
    deadline = int(time.time()) + 110
    runs.submit_run({'objective': 'Respect the original program deadline.', **POLICY},
                    user_id=OWNER, deadline_at=deadline)
    assert tasks[0]['config']['executionLimits']['deadline'] == deadline - 30
    finish(runtime, tasks[0], 'Original execution output.')
    assert tasks[-1]['config']['executionLimits']['deadline'] == deadline


def test_program_persists_and_freezes_review_allocation(program_runtime):
    programs, _, launched = program_runtime
    request = {'objective': 'Explore the original goal.', **POLICY}
    program = programs.create_program(request, user_id=512, idempotency_key='program-reserve')
    assert program['execution_options']['review_reserve'] == POLICY['review_reserve']
    current = programs.command_program(program['program_id'], {'action': 'start'}, user_id=512)
    assert current['current']['payload']['review_reserve'] == POLICY['review_reserve']
    programs.advance_program(program['program_id'], user_id=512)
    assert launched[0]['payload']['review_reserve'] == POLICY['review_reserve']
    with pytest.raises(programs.EvolutionStateError):
        programs.create_program({**request, 'review_reserve': {'tokens': 71}}, user_id=512,
                                idempotency_key='program-reserve')


def test_program_refuses_reserve_that_remaining_cannot_fund(program_runtime):
    programs, store, launched = program_runtime
    program = programs.create_program({'objective': 'Keep the remaining budget intact.', **POLICY},
                                      user_id=512, idempotency_key='program-no-fund')
    key = programs.records.entity_key(program['program_id'], 512, kind='program')
    row = store.rows[(programs.PROGRAMS, key)]
    row['actuals']['tokens'] = 170
    before = deepcopy(row)
    writes = store.writes
    with pytest.raises(programs.EvolutionStateError, match='review reserve unavailable'):
        programs.command_program(program['program_id'], {'action': 'start'}, user_id=512)
    current = programs.get_program(program['program_id'], user_id=512)
    assert current == before and store.writes == writes
    assert current['current'] is None and not launched
    assert current['actuals']['tokens'] == 170 and current['execution_options']['budget'] == POLICY['budget']


@pytest.fixture
def durable_runtime(monkeypatch, request):
    from lib.storage import storage_status
    if not storage_status().get('ready'):
        request.getfixturevalue('chat_sidecar')
    from tests.conftest import _assert_isolated_storage
    _assert_isolated_storage('review reserve original stage durability')
    from lib.evolution import runs
    from lib.tasks_pkg import spawn
    monkeypatch.setattr(runs, 'resolve_run_model', lambda requested, **kwargs: requested)
    tasks = []
    monkeypatch.setattr(spawn, 'spawn_task', tasks.append)
    monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id:
                        next((task for task in tasks if task['id'] == tid and task['_userId'] == user_id), None))
    yield runs, None, tasks
    from lib.tasks_pkg.manager._registry import discard_task
    from lib.agent_core.execution_session import ExecutionPhase
    for task in tasks:
        task['_executionSession'].settle(ExecutionPhase.CANCELLED, cause='test_cleanup')
        discard_task(task['id'], '')


def test_original_reserve_and_both_stage_receipts_use_durable_store(durable_runtime):
    runs, _, tasks = durable_runtime
    original = runs.submit_run({'objective': 'Explain why the reserve is within the original budget.', **POLICY},
                               user_id=OWNER)
    finish(durable_runtime, tasks[-1], 'The executor cap excludes the reserve; review uses actual remaining.')
    completed = finish(durable_runtime, tasks[-1], review())
    reread = runs.get_run(original['run_id'], user_id=OWNER)
    assert completed == reread and completed['status'] == 'completed'
    assert completed['review_reserve'] == POLICY['review_reserve']
    assert completed['actuals']['tokens'] == 30 and completed['budget'] == POLICY['budget']
    assert [receipt['stage'] for receipt in completed['receipts']] == ['executing', 'reviewing']
