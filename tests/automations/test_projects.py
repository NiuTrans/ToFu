"""Real authority contracts for owned automation project scope and task capture."""
import time

import pytest

from lib.automations.errors import AutomationConflict,AutomationError
from lib.automations.management import AutomationManagement
from lib.automations.projects import project_id,resolve_task_project
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from tests.support.automation_fixtures import clock_definition

pytest_plugins=('tests.support.semantic_authorities',)
pytestmark=pytest.mark.api


def manager(authority,*,tenant='projects',scopes=None):
    factory,owner,_=authority
    principal=PrincipalContext.user(subject_id='project-owner',owner_user_id=owner,tenant_id=tenant,
        scopes={'automations:read','automations:manage','automations:execute'} if scopes is None else scopes)
    return AutomationManagement(principal,client_factory=factory)


def body(root,*,revision=0,request='register',active=True,label='Project'):
    return {'root':str(root),'label':label,'active':active,'expectedRevision':revision,'requestId':request}


def test_registration_replay_cas_retirement_and_restart(semantic_authority,tmp_path):
    service=manager(semantic_authority);root=tmp_path/'项目-東京';root.mkdir()
    request=body(root,label='项目 🔐');first=service.update_project(request)
    assert first['id']==project_id(str(root.resolve())) and first['revision']==1
    assert service.update_project(request)==first
    changed={**request,'label':'conflicting'}
    with pytest.raises(AutomationConflict):service.update_project(changed)
    retired=service.update_project(body(root,revision=1,request='retire',active=False))
    assert retired['createdAt']==first['createdAt'] and retired['revision']==2
    assert not service.projects_repository.owns(service.principal,first['id'])
    assert service.projects()['items']==[retired]
    assert service.projects(limit=1,after=first['id'])['items']==[]
    if semantic_authority[2] is not None:semantic_authority[2].crash_cycle()
    assert service.project(first['id'])==retired
    root.rmdir()
    assert service.update_project(body(root,revision=1,request='retire',active=False))==retired
    with pytest.raises(AutomationError,match='unavailable'):
        service.update_project(body(root,revision=2,request='reactivate'))


def test_retirement_keeps_registered_identity_after_filesystem_relink(semantic_authority,tmp_path):
    service=manager(semantic_authority)
    root=tmp_path/'registered';root.mkdir()
    original=service.update_project(body(root))
    root.rename(tmp_path/'moved')
    replacement=tmp_path/'replacement';replacement.mkdir()
    root.symlink_to(replacement,target_is_directory=True)
    fresh=service.update_project(body(root,request='replacement'))
    assert fresh['id']!=original['id']
    retired=service.update_project(body(original['root'],revision=original['revision'],
        request='retire-original',active=False))
    assert retired['id']==original['id'] and retired['active'] is False
    assert service.project(fresh['id'])['active'] is True


def test_owner_tenant_scopes_and_prelimit_isolation(semantic_authority,tmp_path):
    service=manager(semantic_authority);foreign=manager(semantic_authority,tenant='foreign')
    own=service.update_project(body(tmp_path,request='own'))
    foreign.update_project(body(tmp_path,request='foreign',label='secret'))
    assert service.projects(limit=1)['items']==[own]
    assert foreign.project(own['id'])['label']=='secret'
    with pytest.raises(AutomationError,match='does not exist'):service.project('project_'+'0'*64)
    reader=manager(semantic_authority,scopes={'automations:read'})
    assert reader.projects()['items']==[own]
    with pytest.raises(PermissionError):reader.update_project(body(tmp_path,request='forged'))
    with pytest.raises(PermissionError):manager(semantic_authority,scopes=set()).projects()


def test_unregistered_or_retired_project_cannot_publish_or_admit(semantic_authority,tmp_path):
    from lib.automations.definitions import prepare_definition
    service=manager(semantic_authority)
    rule=clock_definition();rule['trigger']={'kind':'manual'};rule['projectId']=project_id(str(tmp_path))
    with pytest.raises(AutomationError,match='does not exist'):
        service.publish(rule['id'],{'definition':rule,'expectedRevision':0,'requestId':'publish'})
    with pytest.raises(AutomationConflict):
        service.repository.publish(prepare_definition(rule),expected_version=0,request_id='bypass',now=time.time())
    assert service.repository.get('definition',rule['id']) is None
    registered=service.update_project(body(tmp_path))
    definition=service.publish(rule['id'],{'definition':rule,'expectedRevision':0,'requestId':'publish'})
    first=service.start(rule['id'],{'requestId':'start','expectedRevision':definition['revision']})
    service.update_project(body(tmp_path,revision=registered['revision'],request='retire',active=False))
    assert service.start(rule['id'],{'requestId':'start','expectedRevision':definition['revision']})==first
    with pytest.raises(AutomationError):service.start(rule['id'],{'requestId':'new','expectedRevision':definition['revision']})
    event={'id':'manual:raw','kind':'manual','occurredAt':time.time(),'origin':'human',
           'evidence':{},'automationChain':[],'projectId':registered['id']}
    with pytest.raises(AutomationConflict):
        service.repository.admit(rule['id'],event,now=time.time(),expected_definition_version=definition['revision'])
    # Retirement never prevents pausing/retiring the rule or reading accepted work.
    service.inactive(rule['id'],{'expectedRevision':definition['revision'],'requestId':'pause'})
    assert service.run(first['id'])==first


def test_task_scope_is_registered_owned_and_frozen_before_physical_start(semantic_authority,tmp_path):
    factory,_,_=semantic_authority;service=manager(semantic_authority)
    root=tmp_path/'root';root.mkdir();registered=service.update_project(body(root))
    config={'projectPath':str(root),'projectId':'forged','automationProjectId':'forged'}
    capture=resolve_task_project(service.principal,config,client_factory=factory)
    assert capture==registered['id']
    assert resolve_task_project(manager(semantic_authority,tenant='other').principal,config,client_factory=factory) is None
    with pytest.raises(AutomationError):resolve_task_project(service.principal,{},project='forged',client_factory=factory)
    sibling=tmp_path/'sibling';sibling.mkdir()
    with pytest.raises(AutomationError):resolve_task_project(service.principal,{'projectPath':str(sibling)},project=capture,client_factory=factory)
    service.update_project(body(root,revision=1,request='retire',active=False))
    assert capture==registered['id']
    assert resolve_task_project(service.principal,config,client_factory=factory) is None


def test_binding_namespace_denies_generic_forgery_and_deletion(semantic_authority,tmp_path):
    factory,owner,_=semantic_authority;service=manager(semantic_authority)
    first=service.update_project(body(tmp_path));repo=service.projects_repository.repository
    for operation in ('record.put','record.delete'):
        payload={'namespace':'automation.targets.v1','key':repo._key(first['id']),'user_id':owner,'expected_version':1}
        if operation=='record.put':payload['value']={**repo.get('target',first['id']).value,'root':'/forged'}
        with pytest.raises(StorageError):factory(write=True).command(operation,payload,None)
    assert service.project(first['id'])==first


def test_attempt_execution_config_read_is_explicit_and_owner_scoped(semantic_authority,monkeypatch):
    from tests.automations.test_task_started_events import pending
    factory,owner,created,attempt,_=pending(semantic_authority,monkeypatch)
    payload={'user_id':owner,'attempt_id':attempt}
    public=factory().query('turn.attempt.get',payload)
    assert 'config' not in public
    internal=factory().query('turn.attempt.get',{**payload,'include_execution_config':True})
    assert internal['config']=={'model':'synthetic'}
    assert {key:value for key,value in internal.items() if key!='config'}==public
    foreign={**payload,'user_id':owner+1,'include_execution_config':True}
    if semantic_authority[2] is not None:
        with pytest.raises(StorageError):factory().query('turn.attempt.get',foreign)
    else:
        assert factory().query('turn.attempt.get',foreign) is None
    with pytest.raises(StorageError):
        factory().query('turn.attempt.get',{**payload,'include_execution_config':'true'})


@pytest.mark.parametrize('root', [
    'remote:desktop:project',
    'remote:desktop:C:\\work',
    'remote:desktop:share:project',
])
def test_remote_chat_has_no_implicit_local_automation_scope(root):
    principal = PrincipalContext.user(subject_id='remote-owner', owner_user_id=7,
        tenant_id='remote-chat', scopes={'chat'})
    config = {'projectPath': root, 'automationProjectId': 'forged'}

    def forbid_storage(*args, **kwargs):
        pytest.fail('remote chat consulted local automation storage')

    assert resolve_task_project(principal, config, client_factory=forbid_storage) is None
    assert config == {'projectPath': root, 'automationProjectId': 'forged'}


@pytest.mark.parametrize('root', ['relative/project', 'remote:desktop', 'remote::project'])
def test_nonlocal_paths_do_not_weaken_local_automation_root_validation(root):
    principal = PrincipalContext.user(subject_id='remote-owner', owner_user_id=7,
        tenant_id='remote-chat', scopes={'chat'})
    with pytest.raises(AutomationError, match='absolute bounded path'):
        resolve_task_project(principal, {'projectPath': root})


def test_remote_chat_cannot_assert_an_explicit_local_automation_binding(semantic_authority, tmp_path):
    service = manager(semantic_authority)
    registered = service.update_project(body(tmp_path))
    with pytest.raises(AutomationError):
        resolve_task_project(service.principal, {'projectPath': 'remote:desktop:project'},
            project=registered['id'], client_factory=semantic_authority[0])
    assert service.project(registered['id']) == registered
