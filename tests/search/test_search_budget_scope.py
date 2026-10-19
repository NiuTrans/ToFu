"""Search filter fallback cannot hide a task's explicit budget refusal."""
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import pytest
from tofu_search.config import get_config,bind_call_config
from lib.search.search_budget import content_filter_budget_scope
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
pytestmark=pytest.mark.unit

def test_swallowed_denial_reappears_and_restores_config(monkeypatch):
    from lib.search import search_bridge
    owner={'id':'search','config':{'executionLimits':{'max_tokens':0,'max_tool_calls':0,'deadline':None}}}
    port=model_budget_admission(owner)
    def adapter(messages,*,owner_user_id,model_budget_admit,**kwargs):
        assert owner_user_id==2 and model_budget_admit is port
        model_budget_admit(input_tokens=1,max_output_tokens=1)
    monkeypatch.setattr(search_bridge,'_chatui_llm',adapter)
    config=replace(get_config(),llm_function=adapter)
    with bind_call_config(config):
        with pytest.raises(ModelBudgetAdmissionDenied):
            with content_filter_budget_scope(owner_user_id=2,model_budget_admit=port):
                with pytest.raises(ModelBudgetAdmissionDenied):
                    get_config().llm_function([])
        assert get_config() is config

def test_concurrent_contexts_and_worker_inheritance(monkeypatch):
    from lib.search import search_bridge
    def adapter(messages,*,owner_user_id,model_budget_admit,**kwargs):
        return (owner_user_id,model_budget_admit)
    monkeypatch.setattr(search_bridge,'_chatui_llm',adapter)
    config=replace(get_config(),llm_function=adapter);ports=[lambda **k:None,lambda **k:None]
    def run(i):
        with bind_call_config(config):
            with content_filter_budget_scope(owner_user_id=i+2,model_budget_admit=ports[i]):
                context=copy_context()
                with ThreadPoolExecutor(max_workers=1) as child:
                    return child.submit(context.run,lambda:get_config().llm_function([])).result()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
    assert results==[(2,ports[0]),(3,ports[1])]

def test_normal_filter_failure_does_not_become_budget_refusal(monkeypatch):
    from lib.search import search_bridge
    def adapter(*a,**k):raise RuntimeError('synthetic failure')
    monkeypatch.setattr(search_bridge,'_chatui_llm',adapter)
    config=replace(get_config(),llm_function=adapter)
    with bind_call_config(config):
        with content_filter_budget_scope(owner_user_id=2,model_budget_admit=lambda **k:None):
            try:get_config().llm_function([])
            except RuntimeError:pass
        assert get_config() is config

def test_real_filter_and_host_adapter_cannot_hide_refusal(monkeypatch):
    from lib.search import search_bridge
    from lib.llm_dispatch import api
    from tofu_search.fetch import content_filter
    owner={'id':'real-filter','config':{'executionLimits':{
        'max_tokens':0,'max_tool_calls':0,'deadline':None}}}
    port=model_budget_admission(owner);seen=[]
    def dispatch(messages,*,owner_user_id,model_budget_admit,**kwargs):
        seen.append((owner_user_id,model_budget_admit))
        model_budget_admit(input_tokens=1,max_output_tokens=1)
        pytest.fail('no provider send')
    monkeypatch.setattr(api,'dispatch_chat',dispatch)
    monkeypatch.setattr(content_filter,'_get_filter_cache',lambda config:None)
    config=replace(get_config(),llm_function=search_bridge._chatui_llm,filter_enabled=True)
    raw='Synthetic source passage. '*100
    with bind_call_config(config):
        with pytest.raises(ModelBudgetAdmissionDenied):
            with content_filter_budget_scope(owner_user_id=2,model_budget_admit=port):
                result=content_filter.filter_web_content(raw,url='https://example.invalid/source',
                    user_question='synthetic source',min_chars=1)
                assert result==raw  # The package catches the exception; scope still refuses.
        assert get_config() is config
    assert seen==[(2,port)]

def test_late_callback_after_scope_cannot_dispatch(monkeypatch):
    from lib.search import search_bridge
    from lib.llm.llm_errors import AbortedError
    def adapter(*a,**k):pytest.fail('closed scope cannot call model')
    monkeypatch.setattr(search_bridge,'_chatui_llm',adapter)
    config=replace(get_config(),llm_function=adapter)
    with bind_call_config(config):
        with content_filter_budget_scope(owner_user_id=2,model_budget_admit=lambda **k:None):
            callback=get_config().llm_function
        with pytest.raises(AbortedError,match='closed'):callback([])
        assert get_config() is config
