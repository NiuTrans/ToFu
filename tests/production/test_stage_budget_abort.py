"""Policy refusal stops stage admission while ordinary failures keep retries."""
import pytest
from lib.production.stages import Stage, run_stages, run_independent_stages, load_state, StageFailed
from lib.agent_core.execution_budget import model_budget_admission, ModelBudgetAdmissionDenied
from lib.llm_errors import AbortedError
pytestmark = pytest.mark.unit


def run(kind, stages, ctx, path):
    if kind == 'serial':
        return run_stages(stages, ctx, state_path=str(path), abort_exceptions=(AbortedError,))
    return run_independent_stages(stages, ctx, state_path=str(path), max_workers=1, abort_exceptions=(AbortedError,))


@pytest.mark.parametrize('kind', ['serial', 'independent'])
def test_budget_refusal_stops_retries_and_preserves_completed_checkpoint(kind, tmp_path):
    task = {'id':'production-budget', 'config':{'executionLimits':{
        'max_tokens':0, 'max_tool_calls':None, 'deadline':None}}}
    admit = model_budget_admission(task)
    calls = []
    def refused(ctx):
        calls.append('refused')
        admit(input_tokens=1, max_output_tokens=1)
        pytest.fail('provider must not be reached')
    def later(ctx):
        calls.append('later')
        return {'ok':True}
    stages = [Stage('completed', lambda ctx: {'ok':True}),
              Stage('refused', refused, retry=3), Stage('later', later)]
    path = tmp_path / 'state.json'
    # Independent siblings are all eligible; an already-started sibling can
    # settle after another reports an explicit owner budget refusal.
    run_stages([stages[0]], {}, state_path=str(path))
    with pytest.raises(ModelBudgetAdmissionDenied) as error:
        run(kind, stages, {}, path)
    assert error.value.budget is task['_executionBudget']
    assert calls.count('refused') == 1
    assert set(calls) <= {'refused', 'later'}
    retained = set(load_state(str(path))['stages'])
    assert 'completed' in retained and 'refused' not in retained
    if kind == 'serial':
        assert calls == ['refused'] and retained == {'completed'}
    assert task['_executionBudget'].consumption_snapshot() == (0,0,True)


@pytest.mark.parametrize('kind', ['serial', 'independent'])
def test_model_abort_preserves_original_exception_without_retry(kind, tmp_path):
    original = AbortedError('owner cancelled')
    calls = []
    def abort(ctx):
        calls.append(1)
        raise original
    with pytest.raises(AbortedError) as error:
        run(kind, [Stage('abort', abort, retry=3)], {}, tmp_path / 'state.json')
    assert error.value is original
    assert calls == [1]


@pytest.mark.parametrize('kind', ['serial', 'independent'])
def test_ordinary_transient_failure_retains_retry_policy(kind, tmp_path):
    calls = []
    def failed(ctx):
        calls.append(1)
        raise RuntimeError('ordinary provider failure')
    with pytest.raises(StageFailed):
        run(kind, [Stage('failure', failed, retry=2)], {}, tmp_path / 'state.json')
    assert len(calls) == 3
