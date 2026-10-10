"""Actual selected authority source settings: owned CAS, staged-ref replay, retirement."""
import time

import pytest

from lib.skills.automation.source_records import (
    SOURCE_NAMESPACE,credential_scope,source_key,source_digest,
)
from lib.storage.errors import StorageError

pytest_plugins=('tests.support.semantic_authorities',)
pytestmark=pytest.mark.api


def request(authority,*,tenant='source-owner',version=0,nonce='configure',reference='a'*32,
            seal='b'*64,active=True):
    _,owner,_=authority
    return {'user_id':owner,'tenant_id':tenant,'source_key':source_key(owner,tenant),
        'expected_version':version,'request_id':nonce,'actor_subject_id':'source-owner',
        'environment':'prod','active':active,'credential_ref':None if reference is None else credential_scope(owner,tenant)+'_'+reference,
        'credential_seal':seal,'now_ms':int(time.time()*1000)}


def test_identical_staged_credentials_replay_first_reference(semantic_authority):
    factory,owner,_=semantic_authority
    payload=request(semantic_authority)
    first=factory(write=True).command('automation.skills.source.update',payload,None)
    assert first['version']==1
    assert set(first)=={'version','value','updated_at_ms'}
    assert first['value']['command']['digest']==source_digest(payload)
    assert first['value']['credentialRef']==payload['credential_ref']
    replay=dict(payload,credential_ref=credential_scope(owner,payload['tenant_id'])+'_'+'c'*32,now_ms=payload['now_ms']+1)
    assert factory(write=True).command('automation.skills.source.update',replay,None)==first
    with pytest.raises(StorageError) as changed:
        factory(write=True).command('automation.skills.source.update',dict(replay,credential_seal='d'*64),None)
    assert changed.value.code=='database_conflict'


def test_retirement_and_credential_removal_are_cas_and_durable(semantic_authority):
    factory,owner,_=semantic_authority
    first=factory(write=True).command('automation.skills.source.update',request(semantic_authority),None)
    retired=request(semantic_authority,version=1,nonce='retire',reference=None,seal=None,active=False)
    value=factory(write=True).command('automation.skills.source.update',retired,None)
    assert value['version']==2 and value['value']['active'] is False
    assert value['value']['credentialRef'] is None
    assert value['value']['createdAt']==first['value']['createdAt']
    with pytest.raises(StorageError) as stale:
        factory(write=True).command('automation.skills.source.update',request(semantic_authority,nonce='stale'),None)
    assert stale.value.code=='database_conflict'
    restart=getattr(factory,'restart',None)
    if callable(restart):restart()
    restored=factory().query('record.get',{'namespace':SOURCE_NAMESPACE,'key':retired['source_key'],'user_id':owner})
    # The source operation has an explicit backend-neutral projection.
    restored.pop('key',None)
    assert restored==value


def test_tenant_credential_references_and_namespace_forgery_are_denied(semantic_authority):
    factory,owner,_=semantic_authority
    payload=request(semantic_authority,tenant='owned')
    first=factory(write=True).command('automation.skills.source.update',payload,None)
    foreign=request(semantic_authority,tenant='foreign')
    bad=dict(foreign,credential_ref=payload['credential_ref'])
    with pytest.raises(StorageError):factory(write=True).command('automation.skills.source.update',bad,None)
    assert factory().query('record.get',{'namespace':SOURCE_NAMESPACE,'key':foreign['source_key'],'user_id':owner}) is None
    for operation in ['record.put','record.delete']:
        raw={'namespace':SOURCE_NAMESPACE,'key':payload['source_key'],'user_id':owner,
             'expected_version':first['version']}
        if operation=='record.put':raw['value']=first['value']
        with pytest.raises(StorageError):factory(write=True).command(operation,raw,None)


@pytest.mark.parametrize('change',[
    {'active':True,'credential_ref':None,'credential_seal':None},
    {'credential_seal':None}, {'environment':'unknown'}, {'extra':'denied'},
    {'expected_version':True}, {'now_ms':0}, {'actor_subject_id':''},
])
def test_source_wire_rejects_invalid_settings_before_mutation(semantic_authority,change):
    factory,owner,_=semantic_authority
    payload=dict(request(semantic_authority),**change)
    with pytest.raises(StorageError):factory(write=True).command('automation.skills.source.update',payload,None)
    assert factory().query('record.get',{'namespace':SOURCE_NAMESPACE,
        'key':payload['source_key'],'user_id':owner}) is None


def test_nullable_credential_fields_are_required_on_wire(semantic_authority):
    factory,owner,_=semantic_authority
    payload=request(semantic_authority,reference=None,seal=None,active=False)
    del payload['credential_ref']
    with pytest.raises(StorageError):factory(write=True).command('automation.skills.source.update',payload,None)
    assert factory().query('record.get',{'namespace':SOURCE_NAMESPACE,
        'key':payload['source_key'],'user_id':owner}) is None
