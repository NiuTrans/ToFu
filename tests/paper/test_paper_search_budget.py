"""Research fetch batches retain one auxiliary filter budget across threads."""
import json
from contextvars import ContextVar
from dataclasses import replace
from types import SimpleNamespace
import pytest
from tofu_search.config import get_config,bind_call_config
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
pytestmark=pytest.mark.unit

def test_real_filter_denial_survives_tool_batch_fallback(monkeypatch):
    from lib.search import search_bridge
    from lib.llm_dispatch import api
    from lib.paper.tools import execute_paper_tool
    from tofu_search.fetch import content_filter
    owner={'id':'paper-search','config':{'executionLimits':{
        'max_tokens':0,'max_tool_calls':0,'deadline':None}}}
    port=model_budget_admission(owner);seen=[]
    def dispatch(messages,*,owner_user_id,model_budget_admit,**kwargs):
        seen.append((owner_user_id,model_budget_admit))
        model_budget_admit(input_tokens=1,max_output_tokens=1)
        pytest.fail('no send')
    monkeypatch.setattr(api,'dispatch_chat',dispatch)
    monkeypatch.setattr(content_filter,'_get_filter_cache',lambda cfg:None)
    def fetch(url,*a,**k):
        raw='Synthetic research content. '*100
        content=content_filter.filter_web_content(raw,url=url,user_question='research',min_chars=1)
        return {'url':url,'page_content':content,'filtered_chars':len(content),'error_msg':None}
    backend=SimpleNamespace(fetch_url_one=fetch,format_fetch_display=lambda row,short:row)
    config=replace(get_config(),llm_function=search_bridge._chatui_llm,filter_enabled=True)
    with bind_call_config(config):
        with pytest.raises(ModelBudgetAdmissionDenied):
            execute_paper_tool('fetch_url',json.dumps({'urls':['https://example.invalid/a','https://example.invalid/b']}),
                search_backend=backend,owner_user_id=2,model_budget_admit=port)
        assert get_config() is config
    assert 1<=len(seen)<=2 and all(uid==2 and p is port for uid,p in seen)
    assert owner['_executionBudget'].consumption_snapshot()==(0,0,True)

def test_batch_context_inheritance_is_explicit():
    from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
    value=ContextVar('synthetic-batch-context',default='unbound')
    token=value.set('owned')
    try:
        assert run_batch_concurrent([1,2],lambda _:value.get(),max_workers=2)==['unbound']*2
        assert run_batch_concurrent([1,2],lambda _:value.get(),max_workers=2,inherit_context=True)==['owned']*2
    finally:value.reset(token)

def test_normal_tool_failure_retains_existing_error_result():
    from lib.paper.tools import execute_paper_tool
    def fetch(*a,**k):raise RuntimeError('synthetic fetch failure')
    backend=SimpleNamespace(fetch_url_one=fetch,format_fetch_display=lambda row,short:row)
    result=execute_paper_tool('fetch_url','{"url":"https://example.invalid/a"}',search_backend=backend)
    assert 'Failed to fetch' in result[0]
    assert len(result[1])==1 and result[1][0]['page_content'] is None
