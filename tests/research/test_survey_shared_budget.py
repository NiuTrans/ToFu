"""Hierarchical survey retains explicit physical admission and unknown costs."""
import json
import time
import pytest
from lib.research.survey_hierarchy import BoundedCalls
from lib.research.telemetry import ResearchUsageMeter
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
pytestmark=pytest.mark.unit

def calls(dispatch,port=None,workdir=None):
    return BoundedCalls(dispatch,json.loads,ResearchUsageMeter('survey',token_budget=100000,dispatch_budget=10),
        direction='synthetic',user_id=2,workdir=workdir,model_budget_admit=port)

def test_refusal_propagates_and_durable_unknown_usage_remains(tmp_path):
    task={'id':'survey','config':{'executionLimits':{'max_tokens':0,'max_tool_calls':0,'deadline':time.time()+60}}}
    port=model_budget_admission(task);seen=[]
    def dispatch(messages,*,model_budget_admit,**kwargs):
        seen.append(model_budget_admit)
        model_budget_admit(input_tokens=1,max_output_tokens=4000)
        pytest.fail('must never send')
    runner=calls(dispatch,port,tmp_path)
    with pytest.raises(ModelBudgetAdmissionDenied):runner('map',{'source':'synthetic'})
    assert seen==[port]
    assert task['_executionBudget'].consumption_snapshot()==(0,0,True)
    assert runner.meter.snapshot()['unmetered_calls']==1
    restored=calls(dispatch,port,tmp_path)
    assert restored.reserved_tokens==runner.reserved_tokens>0
    assert restored.meter.snapshot()['unmetered_calls']==1

def test_each_uncached_phase_receives_same_port(tmp_path):
    port=lambda **counts:None;seen=[]
    def dispatch(messages,*,model_budget_admit,on_content,**kwargs):
        seen.append(model_budget_admit);text='{"summary":"synthetic"}';on_content(text)
        return {'role':'assistant','content':text},'stop',{'prompt_tokens':1,'completion_tokens':1}
    runner=calls(dispatch,port,tmp_path)
    runner('map',{'source':'one'});runner('synthesis',{'source':'two'});runner('map',{'source':'one'})
    assert seen==[port,port] and runner.cache_hits==1

@pytest.mark.parametrize('invalid',[False,1,'invalid'])
def test_invalid_port_refuses_before_call(invalid):
    with pytest.raises(ValueError,match='callable'):calls(lambda *a,**k:pytest.fail('no send'),invalid)

def test_recipe_to_survey_to_hierarchy_preserves_refusal(monkeypatch):
    from lib.paper import survey
    from lib.research import recipe
    from tests.research.test_survey_hierarchy import papers
    task={'id':'survey-chain','config':{'executionLimits':{
        'max_tokens':0,'max_tool_calls':0,'deadline':time.time()+60}}}
    port=model_budget_admission(task);seen=[]
    monkeypatch.setattr(survey,'_load_paper_inputs',lambda *a,**k:papers(2))
    def dispatch(messages,*,model_budget_admit,**kwargs):
        seen.append(model_budget_admit)
        model_budget_admit(input_tokens=1,max_output_tokens=4000)
        pytest.fail('must never send')
    monkeypatch.setattr(survey,'dispatch_stream',dispatch)
    context={'direction':'synthetic','user_id':2,'model_budget_admit':port,
        'artifacts':{'harvest':{'folder_id':'synthetic-folder','arxiv_ids':['2501.00000','2501.00001']}}}
    with pytest.raises(ModelBudgetAdmissionDenied):recipe._run_survey(context)
    assert seen==[port]
