"""Translation refuses goal budget exhaustion without retry or fallback."""
import pytest
from lib.agent_core.execution_budget import model_budget_admission,ModelBudgetAdmissionDenied
pytestmark=pytest.mark.unit

def port():
    task={'id':'translate','config':{'executionLimits':{'max_tokens':0,'max_tool_calls':0,'deadline':None}}}
    return model_budget_admission(task)

def test_free_text_preserves_budget_refusal_without_retry(monkeypatch):
    from lib.translate.engine import _engine as engine
    shared=port();seen=[]
    monkeypatch.setattr(engine,'_try_translation_fast_paths',lambda *a,**k:None)
    def candidate(*a,model_budget_admit,**k):
        seen.append(model_budget_admit);model_budget_admit(input_tokens=1,max_output_tokens=1)
        pytest.fail('no send')
    monkeypatch.setattr(engine,'_dispatch_translation_candidate',candidate)
    with pytest.raises(ModelBudgetAdmissionDenied):
        engine._translate_freetext('测试方向','Translate to English',source='zh',target='en',
            use_cache=False,model_budget_admit=shared)
    assert seen==[shared]

@pytest.mark.parametrize('stream',[False,True])
def test_candidate_forwards_same_budget_to_transport(monkeypatch,stream):
    from contextlib import nullcontext
    from lib.translate.engine import _engine as engine
    from lib import key_stats
    from lib.translate import model_gate
    from lib import llm_dispatch
    shared=port();seen=[]
    monkeypatch.setattr(key_stats,'strict_billing_stop_admission',lambda:nullcontext())
    monkeypatch.setattr(model_gate,'translation_model_slot',lambda **k:nullcontext())
    def dispatch(*a,model_budget_admit,**k):
        seen.append(model_budget_admit);model_budget_admit(input_tokens=1,max_output_tokens=1)
        pytest.fail('no send')
    monkeypatch.setattr(llm_dispatch,'smart_chat',dispatch)
    monkeypatch.setattr(llm_dispatch,'dispatch_stream',dispatch)
    with pytest.raises(ModelBudgetAdmissionDenied):
        engine._dispatch_translation_candidate([],chunk_label='synthetic',max_tokens=32,
            timeout=1,max_retries=0,excluded_models=set(),progress_cb=None,
            deadline_exceeded=lambda:False,remaining_deadline_seconds=lambda:1,
            stream=stream,model_budget_admit=shared)
    assert seen==[shared]

def test_discovery_refusal_does_not_fall_back_to_web(monkeypatch):
    from lib.research import recipe
    shared=port();seen=[]
    monkeypatch.setattr('lib.text_lang.guess_language',lambda text:'zh')
    def translate(*a,model_budget_admit,**k):
        seen.append(model_budget_admit);model_budget_admit(input_tokens=1,max_output_tokens=1)
    monkeypatch.setattr(recipe,'_translate_direction_for_search',translate)
    with pytest.raises(ModelBudgetAdmissionDenied):
        recipe._discover_search_query({'direction':'研究方向','model_budget_admit':shared})
    assert seen==[shared]
