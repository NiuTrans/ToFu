"""Route-owned production resolution; no model HTTP, wallet or secret reads."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from unittest.mock import patch

import pytest
from lib.agent_core.execution_budget import ExecutionBudget
from lib.agent_core.native_model_control import ModelRequest,ModelSettlement,NativePricingError
from lib.tasks_pkg.native_pricing import NativeRoutePricing
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.agent_core.test_native_task_terminal import principal
pytestmark=pytest.mark.unit

def request(owner=7):return ModelRequest(principal(owner),'task',1,10,10,1,threading.Event())
def settlement(usage=None):
    return ModelSettlement(request(),json.dumps(usage or {'prompt_tokens':100,'completion_tokens':2}).encode(),False)
def metadata():return dict(owner_user_id=7,tenant_id='',logical_model='logical-model',wire_model='wire-alias',provider_id='owned-provider')

def test_resolver_uses_owned_logical_model_provider_actual_total_and_explicit_clock():
    seen=[]
    def lookup(who,model,provider,**kwargs):
        seen.append((who.require_owner(),model,provider,kwargs))
        return dict(input=6.,output=24.,currency='CNY',cacheReadMul=0.,cacheWriteMul=1.)
    resolver=NativeRoutePricing(principal=principal(),margin=.125,lookup=lookup,rates=lambda _:7.24,clock=lambda:100.5)
    result=settlement({'prompt_tokens':1200,'input_tokens':100,'cache_read_input_tokens':1100,'output_tokens':2})
    price=resolver(result,metadata()).values()
    assert seen==[(7,'logical-model','owned-provider',dict(at=100.5,prompt_tokens=1200))]
    assert price==dict(prompt_tokens=1200,input=6.,output=24.,cache_write_multiplier=1.,cache_read_multiplier=0.,usd_to_cny=7.24,currency='CNY',margin=.125)


def test_default_adapter_reuses_current_tier_peak_and_exchange_owners():
    from lib.pricing._provider import PROVIDER_PRICING
    row={'input':2.5,'output':10.,'contextTiers':[{'id':'small','maxPromptTokens':100,'input':1.,'output':2.},{'id':'large','maxPromptTokens':200,'input':3.,'output':4.}],
         'peak':{'mul':2.,'windows':[(0,24)],'effective_from':1,'tz_offset':0}}
    with patch.dict(PROVIDER_PRICING,{'owned-provider':{'logical-model':row}}),patch('lib.pricing.get_pricing_data',return_value={'usdToCny':8.}):
        resolver=NativeRoutePricing(principal=principal(),margin=0.,clock=lambda:100.)
        for count,expected in [(100,2.),(101,6.),(201,6.)]:
            price=resolver(settlement({'prompt_tokens':count,'completion_tokens':1}),metadata()).values()
            assert price['input']==expected and price['usd_to_cny']==8.
            assert price['prompt_tokens']==count


def test_foreign_owner_or_missing_price_refuses_without_default_estimates():
    calls=[]
    resolver=NativeRoutePricing(principal=principal(),margin=0.,lookup=lambda *args,**kwargs:calls.append(args),rates=lambda _:7.24)
    with pytest.raises(NativePricingError):resolver(ModelSettlement(request(8),b'{}',False),metadata())
    assert not calls
    with pytest.raises(NativePricingError,match='unavailable'):resolver(settlement(),metadata())
    assert len(calls)==1
    assert resolver(settlement({'completion_tokens':2}),metadata()) is None
    assert len(calls)==1


def test_binding_freezes_route_disposes_callback_and_rejects_foreign_resolver_before_use():
    task=dict(id='task',_userId=7,config={},abort_event=threading.Event())
    account=ExecutionBudget(task);task['_executionBudget']=account
    seen=[]
    resolver=NativeRoutePricing(principal=principal(),margin=0.,lookup=lambda p,m,v,**kw:seen.append((m,v)) or {'input':1.,'output':2.},rates=lambda _:7.24)
    with ThreadPoolExecutor(max_workers=1) as executor:
        policy=NativeModelAccountPolicy(principal=principal(),task=task,executor=executor,pricing_resolver=resolver)
        route=metadata()
        with policy.bind_pricing(principal=principal(),task_id='task',metadata=route):
            route['logical_model']='mutated'
            assert policy.prepare_pricing(settlement()).values()['input']==1.
        assert seen==[('logical-model','owned-provider')]
        assert policy.prepare_pricing is None and policy.pricing_result is None
        policy.pricing_resolver=NativeRoutePricing(principal=principal(8),margin=0.)
        with pytest.raises(NativePricingError):
            with policy.bind_pricing(principal=principal(),task_id='task',metadata=metadata()):pass


def test_foreign_tenant_and_unusable_exchange_rate_never_produce_a_snapshot():
    from lib.identity import PrincipalContext
    who=PrincipalContext.user(subject_id='tenant-priced',owner_user_id=7,tenant_id='tenant-a')
    resolver=NativeRoutePricing(principal=who,margin=0.,lookup=lambda *args,**kw:{'input':1.,'output':2.},rates=lambda _:0.)
    with pytest.raises(NativePricingError):resolver.validate_binding(principal(),metadata())
    bound=dict(metadata(),tenant_id='tenant-a')
    result=ModelSettlement(ModelRequest(who,'task',1,10,10,1,threading.Event()),b'{"prompt_tokens":10,"completion_tokens":2}',False)
    with pytest.raises(ValueError):resolver(result,bound)


def test_route_failure_disposes_price_binding_and_preserves_original_error():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tests.model_routing.test_native_route import Repository,selection
    from lib.model_routing.native_route import compile_native_route,run_native_route
    async def run(executor):
        repo=Repository();route=compile_native_route(repo,repo.boundary,selection())
        task=dict(id='task',_userId=7,config={},abort_event=threading.Event())
        account=ExecutionBudget(task);task['_executionBudget']=account
        model=NativeModelAccountPolicy(principal=principal(),task=task,executor=executor,
            pricing_resolver=NativeRoutePricing(principal=principal(),margin=0.))
        failure=RuntimeError('owned execution failed')
        async def execute(*args,**kwargs):
            assert model.prepare_pricing is not None
            raise failure
        client=SimpleNamespace(principal=principal(),run=execute)
        with pytest.raises(RuntimeError) as caught:
            await run_native_route(client,route,{'owner_user_id':7,'task_id':'task','model_control':{'pricing':True}},prepare_template=lambda meta:dict(model=meta['wire_model'],max_tokens=10),callbacks={},working_directory=None,model_policy=model)
        assert caught.value is failure
        assert model.prepare_pricing is None and model.pricing_result is None
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))


@pytest.mark.parametrize('row',[{}, {'name':'unpriced'}, {'input':None,'output':1.}, {'input':1.,'output':1.,'_pricingSource':'qwen_default_estimate'}])
def test_partial_or_estimated_rows_are_not_native_invoice_prices(row):
    resolver=NativeRoutePricing(principal=principal(),margin=0.,lookup=lambda *args,**kwargs:row,rates=lambda _:7.24)
    with pytest.raises(NativePricingError,match='unavailable'):resolver(settlement(),metadata())


def test_invalid_explicit_ports_cannot_select_default_global_prices():
    for key in ('lookup','rates','clock'):
        with pytest.raises(TypeError):NativeRoutePricing(principal=principal(),margin=0.,**{key:False})


def test_boolean_prices_or_exchange_rates_are_not_numeric_rate_evidence():
    for row,rate in [({'input':True,'output':1.},7.24),({'input':1.,'output':1.},True)]:
        resolver=NativeRoutePricing(principal=principal(),margin=0.,lookup=lambda *args,**kwargs:row,rates=lambda _:rate)
        with pytest.raises(NativePricingError):resolver(settlement(),metadata())
