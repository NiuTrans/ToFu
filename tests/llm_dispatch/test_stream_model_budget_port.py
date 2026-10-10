"""Message-list dispatch preserves one physical budget across slot adaptation."""
import time
import pytest
from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
from lib.llm_dispatch.slot import Slot
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
from lib.llm._sse_core import prepare_request

pytestmark=pytest.mark.unit

def adapt(source,port,model='glm-5.3-flash'):
    slot=Slot(key_name='synthetic',api_key='unused',model=model,capabilities={'text'})
    return _adapt_stream_body_for_slot(slot,source,isinstance(source,dict),tools=None,
        max_tokens=32,temperature=0,thinking_enabled=False,preset='low',effort=None,
        model_budget_admit=port)

def task(cap):
    return {'id':'synthetic-budget','config':{'executionLimits':{
        'max_tokens':cap,'max_tool_calls':0,'deadline':time.time()+60}}}

@pytest.mark.parametrize('prebuilt',[False,True])
def test_physical_preparation_denies_same_account_without_send(prebuilt):
    owner=task(0);port=model_budget_admission(owner)
    messages=[{'role':'user','content':'synthetic'}]
    source={'model':'glm-5.3-flash','messages':messages,'max_tokens':32} if prebuilt else messages
    body=adapt(source,port)
    assert body['_model_budget_admit'] is port
    with pytest.raises(ModelBudgetAdmissionDenied):
        prepare_request(body,api_key='unused',base_url='https://example.invalid/v1')
    assert owner['_executionBudget'].consumption_snapshot()==(0,0,True)
    if prebuilt:assert '_model_budget_admit' not in source


def test_model_fallback_keeps_account_and_private_port_off_wire():
    owner=task(10000);port=model_budget_admission(owner)
    original=[{'role':'user','content':'synthetic'}]
    first=adapt(original,port)
    second=adapt(first,port,model='glm-4-flash')
    assert second['_model_budget_admit'] is port
    plan=prepare_request(second,api_key='unused',base_url='https://example.invalid/v1')
    assert '_model_budget_admit' not in plan.body
    assert plan.model_budget_lease is not None
    plan.model_budget_lease.settle(unsent=True)
    assert owner['_executionBudget'].consumption_snapshot()==(0,0,True)

@pytest.mark.parametrize('port',[False,1,'invalid'])
def test_invalid_account_rejected(port):
    with pytest.raises(ValueError,match='callable'):adapt([],port)

def test_existing_account_cannot_be_replaced():
    old=lambda **counts:None
    body={'model':'glm-5.3-flash','messages':[],'_model_budget_admit':old}
    with pytest.raises(ValueError,match='replace'):adapt(body,lambda **counts:None)
    assert body['_model_budget_admit'] is old
