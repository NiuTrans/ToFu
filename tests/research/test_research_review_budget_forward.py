"""Discovery wrappers preserve a caller's budget refusal and ordinary errors."""
import time
import pytest
from lib.research import recipe
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
pytestmark=pytest.mark.unit

def ctx(port=None):
    return {'direction':'synthetic','model_budget_admit':port,'artifacts':{
        'harvest':{},'survey':{},'ideate':{}}}

def test_real_evaluator_budget_refusal_escapes_review(monkeypatch):
    from lib.research import evaluation
    task={'id':'review-boundary','config':{'executionLimits':{
        'max_tokens':0,'max_tool_calls':0,'deadline':time.time()+60}}}
    port=model_budget_admission(task)
    monkeypatch.setenv('TOFU_RESEARCH_EVAL_JUDGES', '1')
    calls=[]
    def dispatch(messages,*,model_budget_admit,**kwargs):
        calls.append(model_budget_admit)
        model_budget_admit(input_tokens=1,max_output_tokens=1)
        pytest.fail('must never send')
    monkeypatch.setattr(evaluation,'dispatch_stream',dispatch)
    with pytest.raises(ModelBudgetAdmissionDenied):recipe._review_once(ctx(port))
    assert calls==[port]
    assert task['_executionBudget'].consumption_snapshot()==(0,0,True)

def test_ordinary_evaluator_error_still_degrades(monkeypatch):
    def fail(*args,**kwargs):raise RuntimeError('synthetic provider error')
    monkeypatch.setattr(recipe,'_evaluate_result',fail)
    result=recipe._review_once(ctx())
    assert result['degraded'] and not result['ok']

def test_budget_refusal_does_not_enter_revision_or_return_error_state():
    from lib.research.revision import run_revision_cycle
    task={'id':'revision-boundary','config':{'executionLimits':{
        'max_tokens':0,'max_tool_calls':0,'deadline':time.time()+60}}}
    port=model_budget_admission(task)
    def review(*args):port(input_tokens=1,max_output_tokens=1)
    with pytest.raises(ModelBudgetAdmissionDenied):
        run_revision_cycle({'accepted':[],'rejected':[]},review=review,
            revise=lambda c:pytest.fail('must not revise after policy refusal'),gate=lambda a:[])
