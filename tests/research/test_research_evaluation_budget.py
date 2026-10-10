"""Independent judges cannot replace or restart the caller's physical budget."""
import json
import time
import threading
import pytest
from lib.research import evaluation as ev
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
from tests.research.test_research_evaluation import _result,_judgement
pytestmark=pytest.mark.unit

def test_parallel_and_tiebreak_share_budget(monkeypatch):
    port=lambda **counts:None
    calls=[];lock=threading.Lock()
    def dispatch(messages,*,on_content,model_budget_admit,**kwargs):
        with lock:
            i=len(calls);calls.append(model_budget_admit)
        raw=json.dumps(_judgement(2 if i==0 else 5,i!=0))
        on_content(raw)
        return {'role':'assistant','content':raw},'stop',{'prompt_tokens':5,'completion_tokens':5}
    monkeypatch.setattr(ev,'dispatch_stream',dispatch)
    monkeypatch.setenv('TOFU_AGENT_WORKERS','1')
    result=ev.evaluate_research_result('synthetic',_result(),model_budget_admit=port)
    assert result['judge_count']==3
    assert len(calls)==3 and all(p is port for p in calls)

def test_budget_refusal_prevents_provider_sends_from_owned_judges(monkeypatch):
    task={'id':'synthetic','config':{'executionLimits':{'max_tokens':0,'max_tool_calls':0,'deadline':time.time()+60}}}
    port=model_budget_admission(task);calls=[]
    def dispatch(messages,*,model_budget_admit,**kwargs):
        calls.append(True)
        model_budget_admit(input_tokens=1,max_output_tokens=10)
        pytest.fail('budget refusal must prevent provider send')
    monkeypatch.setattr(ev,'dispatch_stream',dispatch)
    monkeypatch.setenv('TOFU_AGENT_WORKERS','1')
    with pytest.raises(ModelBudgetAdmissionDenied):
        ev.evaluate_research_result('synthetic',_result(),model_budget_admit=port)
    assert 1 <= len(calls) <= 2
    assert task['_executionBudget'].consumption_snapshot()==(0,0,True)

@pytest.mark.parametrize('invalid',[False,1,'invalid'])
def test_invalid_port_refuses_before_judges(monkeypatch,invalid):
    monkeypatch.setattr(ev,'dispatch_stream',lambda *a,**k:pytest.fail('must refuse before dispatch'))
    with pytest.raises(ValueError,match='callable'):
        ev.evaluate_research_result('synthetic',_result(),model_budget_admit=invalid)

def test_parallel_judges_reserve_one_shared_allowance(monkeypatch):
    task={'id':'parallel-budget','config':{'executionLimits':{
        'max_tokens':12,'max_tool_calls':0,'deadline':time.time()+60}}}
    port=model_budget_admission(task);barrier=threading.Barrier(2)
    admitted=[]
    def dispatch(messages,*,on_content,model_budget_admit,**kwargs):
        barrier.wait(timeout=5)
        try:lease=model_budget_admit(input_tokens=2,max_output_tokens=10)
        except ModelBudgetAdmissionDenied:
            raise
        admitted.append(lease)
        lease.mark_dispatched()
        lease.settle({'prompt_tokens':2,'completion_tokens':10})
        raw=json.dumps(_judgement(4,True));on_content(raw)
        return {'role':'assistant','content':raw},'stop',{'prompt_tokens':2,'completion_tokens':10}
    monkeypatch.setattr(ev,'dispatch_stream',dispatch)
    monkeypatch.setenv('TOFU_AGENT_WORKERS','1')
    with pytest.raises(ModelBudgetAdmissionDenied):
        ev.evaluate_research_result('synthetic',_result(),model_budget_admit=port)
    assert len(admitted)==1
    assert task['_executionBudget'].consumption_snapshot()==(12,0,True)
