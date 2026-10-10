"""All rubric workers use the same explicit budget and preserve refusal."""
import time
import pytest
from lib.paper import ideate
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
pytestmark=pytest.mark.unit

def test_all_parallel_judges_share_the_original_budget_port(monkeypatch):
    port=lambda **counts:None;seen=[]
    def score(*a,model_budget_admit,**k):seen.append(model_budget_admit);return None
    monkeypatch.setattr(ideate,'_score_idea',score)
    monkeypatch.setenv('TOFU_AGENT_WORKERS','1')
    rows=[ideate._PreparedIdeaJudge(i,{'title':str(i)},{},None,0) for i in range(4)]
    assert ideate._score_prepared_ideas(rows,lang='en',model_budget_admit=port)==[None]*4
    assert len(seen)==4 and all(p is port for p in seen)

def test_rubric_refusal_blocks_provider_sends_from_owned_candidates(monkeypatch):
    task={'id':'ideas','config':{'executionLimits':{'max_tokens':0,'max_tool_calls':0,'deadline':time.time()+60}}}
    port=model_budget_admission(task);seen=[]
    def dispatch(messages,*,model_budget_admit,**kwargs):
        seen.append(model_budget_admit);model_budget_admit(input_tokens=1,max_output_tokens=10)
        pytest.fail('no provider send')
    monkeypatch.setattr(ideate,'dispatch_stream',dispatch)
    rows=[ideate._PreparedIdeaJudge(i,{'title':str(i)},{},None,0) for i in range(4)]
    with pytest.raises(ModelBudgetAdmissionDenied):ideate._score_prepared_ideas(rows,lang='en',model_budget_admit=port)
    assert 1 <= len(seen) <= len(rows) and all(p is port for p in seen)
    assert task['_executionBudget'].consumption_snapshot()==(0,0,True)

@pytest.mark.parametrize('invalid',[False,1,'invalid'])
def test_public_generation_rejects_invalid_port(invalid):
    with pytest.raises(ValueError,match='callable'):ideate.generate_ideas('synthetic',{},model_budget_admit=invalid)

def test_public_generation_preserves_budget_refusal(monkeypatch):
    task={'id':'generation','config':{'executionLimits':{
        'max_tokens':0,'max_tool_calls':0,'deadline':time.time()+60}}}
    port=model_budget_admission(task);seen=[]
    def raw(*args,model_budget_admit,**kwargs):
        seen.append(model_budget_admit);model_budget_admit(input_tokens=1,max_output_tokens=10)
        pytest.fail('no generation send')
    monkeypatch.setattr(ideate,'_generate_raw_ideas',raw)
    with pytest.raises(ModelBudgetAdmissionDenied):
        ideate.generate_ideas('synthetic',{'open_gaps':[{'id':'g1','gap':'synthetic'}]},
            user_id=2,model_budget_admit=port)
    assert seen==[port]
