"""Lost ACK, owner isolation and draft/publication races on actual authorities."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.automations.errors import AutomationConflict
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from tests.automations.test_atomic_publication import setup,publication_payload,get_job,subscriptions

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('committed',[False,True])
def test_publication_lost_ack_reads_authority_once_without_blind_retry(semantic_authority,committed):
    factory,repo,rule = setup(semantic_authority,kind='schedule')
    calls=[]
    class Client:
        def command(self,name,payload,receipt):
            calls.append(name)
            if committed:
                factory(write=True).command(name,payload,receipt)
            raise StorageError('database_timeout','Synthetic lost publication acknowledgement')
        def query(self,name,payload):
            calls.append(name)
            return factory().query(name,payload)
    unreliable = AutomationExecutionRepository(repo.principal,client_factory=lambda **_:Client())
    if committed:
        saved = unreliable.publish(prepare_definition(rule),expected_version=0,request_id='lost-ack',now=100)
        assert get_job(factory,repo,saved.value['triggerState']['clockJobId']) is not None
        assert saved.value['publicationCommand']['requestedAt'] == 100
    else:
        with pytest.raises(StorageError):
            unreliable.publish(prepare_definition(rule),expected_version=0,request_id='lost-ack',now=100)
        assert repo.get('definition',rule['id']) is None
    assert calls == ['automation.definition.update','record.get']


def test_independent_tenants_have_distinct_clock_jobs_and_reject_cross_scope_key(semantic_authority):
    factory,repo,rule = setup(semantic_authority,kind='schedule')
    other = AutomationExecutionRepository(PrincipalContext.user(subject_id=repo.principal.subject_id,
        owner_user_id=repo.owner_id,tenant_id='other-tenant',scopes=repo.principal.scopes),client_factory=factory)
    first = repo.publish(prepare_definition(rule),expected_version=0,now=100)
    second = other.publish(prepare_definition(rule),expected_version=0,now=100)
    assert first.value['triggerState']['clockJobId'] != second.value['triggerState']['clockJobId']
    payload = publication_payload(other,prepare_definition(rule),expected=1)
    payload['definition_key'] = repo._key(rule['id'])
    with pytest.raises(StorageError): factory(write=True).command('automation.definition.update',payload,None)
    assert repo.get('definition',rule['id']).value == first.value
    assert other.get('definition',rule['id']).value == second.value


def test_unversioned_draft_writer_cannot_overwrite_promoted_grant(semantic_authority):
    factory,repo,rule = setup(semantic_authority)
    prepared = prepare_definition(rule)
    draft = repo.save_definition(prepared,expected_version=0,now=99)
    gate = Barrier(2)
    def promote():
        gate.wait()
        try:
            return repo.publish(prepared,expected_version=draft.version,now=100)
        except AutomationConflict:
            return None
    def draft_write():
        gate.wait()
        try:
            return factory(write=True).command('record.put',{'namespace':'automation.definitions.v1',
                'key':repo._key(rule['id']),'user_id':repo.owner_id,'value':draft.value},'draft-race')
        except StorageError:
            return None
    with ThreadPoolExecutor(max_workers=2) as workers:
        promoted=workers.submit(promote);written=workers.submit(draft_write)
        published=promoted.result();written.result()
    current=repo.get('definition',rule['id'])
    if published is not None:
        assert current.value == published.value and len(subscriptions(factory,repo)) == 1
    else:
        assert current.value.get('grant') is None and subscriptions(factory,repo)==[]
        published=repo.publish(prepared,expected_version=current.version,now=101)
        assert repo.get('definition',rule['id']).value == published.value
    # Regardless of which transaction won the race, the now-published record is fenced.
    with pytest.raises(StorageError):
        factory(write=True).command('record.put',{'namespace':'automation.definitions.v1',
            'key':repo._key(rule['id']),'user_id':repo.owner_id,'value':draft.value},'late-draft')


@pytest.mark.parametrize('kind',['event','schedule'])
def test_native_publication_survives_ungraceful_authority_reopen(semantic_authority,kind):
    engine=semantic_authority[2]
    if engine is None:
        pytest.skip('Native authority crash/reopen contract')
    factory,repo,rule=setup(semantic_authority,kind=kind)
    prepared=prepare_definition(rule)
    original=repo.publish(prepared,expected_version=0,request_id='before-crash',now=100)
    old_subscriptions=subscriptions(factory,repo)
    clock_id=original.value['triggerState']['clockJobId']
    clock=get_job(factory,repo,clock_id) if clock_id else None
    engine.crash_cycle()
    assert repo.get('definition',rule['id']).value == original.value
    assert subscriptions(factory,repo) == old_subscriptions
    if clock_id:
        assert get_job(factory,repo,clock_id) == clock
    assert repo.publish(prepared,expected_version=0,request_id='before-crash',now=200).value == original.value
