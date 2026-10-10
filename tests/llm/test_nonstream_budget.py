"""Physical non-stream calls account for success, refusal and ambiguous retries."""
import importlib
import time
from types import SimpleNamespace
import pytest
import requests
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
pytestmark=pytest.mark.unit

@pytest.fixture
def transport(monkeypatch):
    module=importlib.import_module('lib.llm.chat')
    from lib.desktop import egress
    monkeypatch.setattr(egress,'route_request',lambda *a,**k:'direct')
    monkeypatch.setattr(module,'retry_wait',lambda n:0)
    return module

def task(cap=10000):
    return {'id':'synthetic-nonstream','config':{'executionLimits':{
        'max_tokens':cap,'max_tool_calls':0,'deadline':time.time()+60}}}

def invoke(module,port,**kwargs):
    return module.chat([{'role':'user','content':'synthetic'}],model='glm-4-flash',
        api_key='unused',base_url='https://example.invalid/v1',max_tokens=32,
        model_budget_admit=port,**kwargs)

def response(usage):
    return SimpleNamespace(status_code=200,headers={},json=lambda:{'choices':[
        {'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}],'usage':usage})

def test_zero_budget_no_send(transport,monkeypatch):
    owner=task(0)
    monkeypatch.setattr(transport,'http_post',lambda *a,**k:pytest.fail('no send'))
    with pytest.raises(ModelBudgetAdmissionDenied):invoke(transport,model_budget_admission(owner))
    assert owner['_executionBudget'].consumption_snapshot()==(0,0,True)

def test_success_settles_once_and_private_port_never_on_wire(transport,monkeypatch):
    owner=task();seen=[]
    def send(*a,**k):seen.append(k);return response({'prompt_tokens':5,'completion_tokens':2})
    monkeypatch.setattr(transport,'http_post',send)
    content,usage=invoke(transport,model_budget_admission(owner),max_retries=0)
    assert content=='ok' and len(seen)==1
    assert '_model_budget_admit' not in str(seen)
    assert owner['_executionBudget'].consumption_snapshot()==(7,0,True)

def test_failed_attempt_unknown_prevents_retry_with_finite_budget(transport,monkeypatch):
    owner=task();seen=[]
    def send(*a,**k):
        from lib.llm_errors import RetryableAPIError
        seen.append(True);raise RetryableAPIError('synthetic')
    monkeypatch.setattr(transport,'http_post',send)
    with pytest.raises(ModelBudgetAdmissionDenied):invoke(transport,model_budget_admission(owner),max_retries=1)
    assert len(seen)==1
    assert owner['_executionBudget'].consumption_snapshot()==(0,0,False)

def test_missing_success_usage_is_unknown(transport,monkeypatch):
    owner=task();monkeypatch.setattr(transport,'http_post',lambda *a,**k:response({}))
    invoke(transport,model_budget_admission(owner),max_retries=0)
    assert owner['_executionBudget'].consumption_snapshot()==(0,0,False)

@pytest.mark.parametrize('invalid',[False,1,'invalid'])
def test_invalid_port_before_transport(transport,monkeypatch,invalid):
    monkeypatch.setattr(transport,'http_post',lambda *a,**k:pytest.fail('no send'))
    with pytest.raises(ValueError,match='callable'):invoke(transport,invalid)

@pytest.mark.parametrize('route',['adapter','egress'])
def test_zero_budget_no_relay(transport,monkeypatch,route):
    from lib.desktop import adapter,egress
    owner=task(0)
    monkeypatch.setattr(adapter,'relay_http',lambda *a,**k:pytest.fail('no relay'))
    monkeypatch.setattr(egress,'egress_http',lambda *a,**k:pytest.fail('no egress'))
    kwargs={}
    if route=='adapter':kwargs['adapter']={'agent_id':'synthetic','port':1234}
    else:monkeypatch.setattr(egress,'route_request',lambda *a,**k:'synthetic-agent')
    with pytest.raises(ModelBudgetAdmissionDenied):invoke(transport,model_budget_admission(owner),**kwargs)
    assert owner['_executionBudget'].consumption_snapshot()==(0,0,True)

def test_learned_limit_recursion_keeps_account_and_unknown_failure(transport,monkeypatch):
    owner=task();seen=[]
    def send(*a,**k):
        seen.append(True)
        return SimpleNamespace(status_code=400,headers={},content=b'synthetic token limit',
                               text='synthetic token limit',encoding='utf-8')
    monkeypatch.setattr(transport,'http_post',send)
    monkeypatch.setattr(transport,'_parse_token_limit_from_error',lambda *a:16)
    monkeypatch.setattr(transport,'_learn_model_limit',lambda *a:None)
    with pytest.raises(ModelBudgetAdmissionDenied):invoke(transport,model_budget_admission(owner),max_retries=0)
    assert len(seen)==1
    assert owner['_executionBudget'].consumption_snapshot()==(0,0,False)

def test_later_success_does_not_complete_unknown_earlier_attempt(transport,monkeypatch):
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.llm_errors import RetryableAPIError
    owner=task(None);owner['_executionBudget']=ExecutionBudget(owner);seen=[]
    def send(*a,**k):
        seen.append(True)
        if len(seen)==1:raise RetryableAPIError('synthetic unknown attempt')
        return response({'prompt_tokens':5,'completion_tokens':2})
    monkeypatch.setattr(transport,'http_post',send)
    content,usage=invoke(transport,model_budget_admission(owner),max_retries=1)
    assert content=='ok' and len(seen)==2
    assert owner['_executionBudget'].consumption_snapshot()==(7,0,False)
