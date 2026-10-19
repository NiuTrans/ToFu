"""Non-stream dispatch preserves the budget and terminal failure."""
import pytest
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
from tests.llm_dispatch.test_async_dispatch_stream import _make_slot,_FakeDispatcher
pytestmark=pytest.mark.unit

def test_smart_exhaustion_preserves_same_port(monkeypatch):
    from lib.llm_dispatch import _api_multi as multi
    port=lambda **counts:None;seen=[]
    def fail(*a,model_budget_admit,**k):seen.append(model_budget_admit);raise RuntimeError('synthetic unavailable')
    def direct(**kwargs):seen.append(kwargs['model_budget_admit']);return 'ok',{}
    monkeypatch.setattr(multi,'dispatch_chat',fail)
    monkeypatch.setattr('lib.llm.chat',direct)
    with pytest.raises(RuntimeError, match='synthetic unavailable'):
        multi.smart_chat([],model_budget_admit=port)
    assert seen==[port]

@pytest.mark.parametrize('smart',[False,True])
def test_budget_denial_releases_slot_without_health_damage(monkeypatch,smart):
    from lib.llm_dispatch import api
    owner={'id':'dispatch-budget','config':{'executionLimits':{
        'max_tokens':0,'max_tool_calls':0,'deadline':None}}}
    port=model_budget_admission(owner);slot=_make_slot();dispatcher=_FakeDispatcher([slot]);dispatcher.slots=[slot];seen=[]
    monkeypatch.setattr(api,'get_dispatcher',lambda:dispatcher)
    def chat(**kwargs):
        seen.append(kwargs['model_budget_admit'])
        kwargs['model_budget_admit'](input_tokens=1,max_output_tokens=1)
        pytest.fail('no send')
    monkeypatch.setattr('lib.llm.chat',chat)
    monkeypatch.setattr(slot,'record_error',lambda *a,**k:pytest.fail('no health penalty'))
    released=[];monkeypatch.setattr(slot,'release',lambda:released.append(True))
    call=api.smart_chat if smart else api.dispatch_chat
    with pytest.raises(ModelBudgetAdmissionDenied):call([],model_budget_admit=port,max_retries=3)
    assert seen==[port] and released==[True] and dispatcher.picks==1

@pytest.mark.parametrize('invalid',[False,1,'invalid'])
def test_invalid_port_no_dispatch(monkeypatch,invalid):
    from lib.llm_dispatch import api
    monkeypatch.setattr(api,'get_dispatcher',lambda:pytest.fail('no selection'))
    for call in (api.dispatch_chat,api.smart_chat):
        with pytest.raises(ValueError,match='callable'):call([],model_budget_admit=invalid)
