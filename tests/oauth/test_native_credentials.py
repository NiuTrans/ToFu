"""Production native credentials recheck the live grant/source around refresh."""
import json
import threading
from dataclasses import replace

import pytest
from lib.model_routing.native_route import NativeCredentialRequest,NativeRouteError
from lib.model_routing.domain import ModelRoutingError
from lib.oauth.native_credentials import NativeCodexCredentialResolver
from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
from tests.model_routing.test_native_route import Repository,principal

pytestmark=pytest.mark.unit


def fixture(monkeypatch):
    repo=Repository()
    repo.document['connections'][0].update(protocol='openai_responses',responses_profile='codex',base_url='https://chatgpt.com/backend-api/codex')
    repo.document['credentials'][0]['kind']='oauth'
    repo.resolve_secret=lambda *_:'oauth:codex'
    state={'account_id':'account-a','owner_user_id':7,'tenant_id':'','access_token':'fixture'}
    monkeypatch.setattr('lib.oauth.token_store.load_stored_token',lambda provider:dict(state))
    calls=[]
    def resolve(kind,body,headers,user_id='',**options):
        calls.append((kind,user_id,options))
        return 'owned-token',{'session-id':options['cache_affinity_key']},body
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',resolve)
    request=NativeCredentialRequest(principal=principal(),task_id='task',attempt_ordinal=1,credential_id='credential-a',provider_id='provider-a',offering_id='alpha-a',connection_id='connection-a',deployment_id='alpha-a-deployment',secret_reference='secret-a',subscription='codex',endpoint='https://chatgpt.com/backend-api/codex/responses',wire_model='alpha-wire-a',cache_affinity_key='bound-key',extra_headers_json=json.dumps({'X-Region':'fixture'}).encode(),cancelled=threading.Event())
    from lib.model_routing.native_route import compile_native_route,route_binding_sha256
    from tests.model_routing.test_native_route import selection
    route=compile_native_route(repo,repo.boundary,selection(),credential_resolver=lambda request:{})
    metadata,_=route.consume(principal())
    request=replace(request,route_binding_sha256=route_binding_sha256(metadata))
    resolver=NativeCodexCredentialResolver(repository=repo,principal=principal())
    return repo,state,calls,request,resolver,resolve


def test_production_resolver_uses_explicit_owner_account_and_affinity(monkeypatch):
    _,_,calls,request,resolver,_=fixture(monkeypatch)
    assert resolver(request)=={'Authorization':'Bearer owned-token','session-id':'bound-key'}
    assert calls==[('codex','7',{'expected_account_id':'account-a','cache_affinity_key':'bound-key'})]


@pytest.mark.parametrize('change',['owner','endpoint','credential','disabled','account_owner','account_missing','context'])
def test_invalid_binding_refuses_before_refresh(monkeypatch,change):
    repo,state,calls,request,resolver,_=fixture(monkeypatch)
    if change=='owner':request=replace(request,principal=principal(8))
    elif change=='endpoint':request=replace(request,endpoint='https://other.example/responses')
    elif change=='credential':repo.document['credentials'][0]['secret_reference']='replaced'
    elif change=='disabled':repo.document['credentials'][0]['enabled']=False
    elif change=='account_owner':state['owner_user_id']=8
    elif change=='context':repo.document['models'][0]['context_window']=128000
    else:state['account_id']=''
    with pytest.raises((NativeRouteError,SubscriptionCredentialUnavailableError,ModelRoutingError)):resolver(request)
    assert calls==[]


@pytest.mark.parametrize('change',['revocation','account','source'])
def test_binding_changes_during_refresh_never_return_credentials(monkeypatch,change):
    repo,state,_,request,resolver,resolve=fixture(monkeypatch)
    def refresh(*args,**kwargs):
        value=resolve(*args,**kwargs)
        if change=='revocation':repo.document['credentials'][0]['enabled']=False
        elif change=='account':state['account_id']='account-b'
        else:state['owner_user_id']=8
        return value
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',refresh)
    with pytest.raises((NativeRouteError,SubscriptionCredentialUnavailableError,ModelRoutingError)):resolver(request)


def test_personal_legacy_source_requires_existing_local_owner_boundary(monkeypatch):
    _,state,calls,request,resolver,_=fixture(monkeypatch)
    state.pop('owner_user_id')
    monkeypatch.setattr('lib.oauth.codex_shared_login.local_owner',lambda owner:None)
    with pytest.raises(SubscriptionCredentialUnavailableError):resolver(request)
    assert calls==[]
    monkeypatch.setattr('lib.oauth.codex_shared_login.local_owner',lambda owner:owner)
    assert resolver(request)['Authorization']=='Bearer owned-token'


def test_outbound_account_change_is_refused_without_credentials(monkeypatch):
    from lib.oauth.outbound import resolve_oauth_request
    monkeypatch.setattr('lib.oauth.codex.codex_get_valid_token',lambda **kwargs:'private-token')
    monkeypatch.setattr('lib.oauth.token_store.load_token',lambda provider:{'account_id':'account-b'})
    with pytest.raises(SubscriptionCredentialUnavailableError) as error:
        resolve_oauth_request('codex',{}, {},user_id='7',expected_account_id='account-a')
    assert error.value.reason=='account_changed'
    assert 'private-token' not in str(error.value)


def test_shared_source_reference_is_frozen_across_in_place_changes(monkeypatch):
    _,state,_,request,resolver,resolve=fixture(monkeypatch)
    reference={'owner_user_id':7,'account_id':'account-a','kind':'local','source':'a'}
    state['codex_shared_reference']=reference
    monkeypatch.setattr('lib.oauth.codex_shared_login.local_owner',lambda owner:owner)
    def refresh(*args,**kwargs):
        result=resolve(*args,**kwargs)
        reference['source']='b'
        return result
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',refresh)
    with pytest.raises(SubscriptionCredentialUnavailableError) as error:resolver(request)
    assert error.value.reason=='credential_source_changed'


def test_application_route_factory_installs_production_resolver_without_token_reads(monkeypatch):
    repo,_,calls,_,_,_=fixture(monkeypatch)
    from lib.tasks_pkg.native_execution import compile_native_application_route
    from tests.model_routing.test_native_route import selection
    monkeypatch.setattr('lib.oauth.token_store.load_stored_token',lambda provider: (_ for _ in ()).throw(AssertionError('startup token lookup')))
    route=compile_native_application_route(repo,principal(),selection())
    assert isinstance(route._credential_resolver,NativeCodexCredentialResolver)
    route.close()
    assert route._credential_resolver is None and calls==[]


def test_account_replacement_between_rounds_is_not_silently_rebound(monkeypatch):
    _,state,calls,request,resolver,_=fixture(monkeypatch)
    resolver(request)
    state['account_id']='account-b'
    with pytest.raises(SubscriptionCredentialUnavailableError) as error:
        resolver(replace(request,attempt_ordinal=2))
    assert error.value.reason=='credential_source_changed'
    assert len(calls)==1


def test_unrelated_aggregate_revision_does_not_revoke_same_route(monkeypatch):
    repo,_,calls,request,resolver,_=fixture(monkeypatch)
    original=repo.get
    def get(boundary):
        from dataclasses import replace
        authority=original(boundary)
        return replace(authority,revision=99)
    repo.get=get
    assert resolver(request)['Authorization']=='Bearer owned-token'
    assert len(calls)==1


def test_production_resolver_uses_real_outbound_identity_headers(monkeypatch):
    from lib.oauth.outbound import resolve_oauth_request
    _,state,_,request,resolver,_=fixture(monkeypatch)
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',resolve_oauth_request)
    monkeypatch.setattr('lib.oauth.codex.codex_get_valid_token',lambda **kwargs:'live-fixture')
    monkeypatch.setattr('lib.oauth.token_store.load_token',lambda provider:dict(state))
    headers=resolver(request)
    assert headers['Authorization']=='Bearer live-fixture'
    assert headers['chatgpt-account-id']=='account-a'
    assert headers['session-id']==headers['thread-id']==headers['x-client-request-id']=='bound-key'
    assert headers['originator'] and headers['User-Agent']


def test_changed_headers_revoke_the_bound_route_before_refresh(monkeypatch):
    repo,_,calls,request,resolver,_=fixture(monkeypatch)
    repo.document['connections'][0]['extra_headers']['X-Region']='changed'
    with pytest.raises(NativeRouteError):resolver(request)
    assert calls==[]


def test_source_change_during_login_failure_is_not_misreported_as_expiry(monkeypatch):
    _,state,_,request,resolver,_=fixture(monkeypatch)
    from lib.llm.llm_errors import SubscriptionLoginRequiredError
    def refresh(*args,**kwargs):
        state['account_id']='account-b'
        raise SubscriptionLoginRequiredError('fixture expired')
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',refresh)
    with pytest.raises(SubscriptionCredentialUnavailableError) as error:resolver(request)
    assert error.value.reason=='credential_source_changed'
