"""Owned project/tenant capture through the actual selected application adapter."""
import time
import uuid

import pytest

from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.storage.test_storage_application_contract import application_storage_owner_id as application_storage_owner_id

pytestmark=pytest.mark.api


@pytest.fixture(autouse=True)
def isolated_cancellation_store(monkeypatch):
    from lib.conversation_sync import pending_abort
    from lib.runtime.runtime_state_store import InProcRuntimeStateStore
    store = InProcRuntimeStateStore()
    monkeypatch.setattr(pending_abort, 'get_store', lambda: store)


def test_scoped_actual_start_uses_accepted_principal_and_project(application_storage,tmp_path,monkeypatch):
    from lib.storage import get_storage_client
    from lib.identity import PrincipalContext
    from lib.automations.management import AutomationManagement
    from lib.automations.events import task_start_context,task_started_job
    from lib.automations.projects import resolve_task_project
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.turn_lifecycle import get_attempt
    from tests.support.automation_fixtures import clock_definition
    principal=PrincipalContext.user(subject_id='accepted-user',owner_user_id=1,tenant_id='accepted-app',
        scopes={'chat','automations:read','automations:manage','automations:execute'})
    service=AutomationManagement(principal)
    project=service.update_project({'root':str(tmp_path),'label':'Owned','active':True,'expectedRevision':0,'requestId':'register'})
    rule=clock_definition();rule['id']+='-'+uuid.uuid4().hex;rule['projectId']=project['id']
    service.publish(rule['id'],{'definition':rule,'expectedRevision':0,'requestId':'publish'})
    captured=[];task='synthetic-'+uuid.uuid4().hex
    def register(conv,config,request_data,abort_after,bind,**kwargs):
        captured.append(dict(config));bind(task);return task,None
    monkeypatch.setattr(conversation_turn_commands,'_start_task',register)
    conversation='project-app-'+uuid.uuid4().hex
    application_storage.command('conversation.create',{'conv_id':conversation,'user_id':1,
        'title':'Owned','settings':{}},'create-'+conversation)
    outcome=conversation_turn_commands.create_turn(conversation,1,
        {'commandId':'accept','message':{'role':'user','content':'Owned scoped work'},
         'config':{'model':'synthetic','projectPath':str(tmp_path),'_turnPrincipalTenantId':'forged'},
         'conversationDefaults':{'allowCreate':True,'title':'Owned','settings':{}}},
        request_started_at=time.time(),principal=principal)
    # CommandOutcome's public result intentionally has no execution config.
    value=outcome.value
    attempt=value['attempt']['attemptId']
    accepted=get_attempt(attempt,user_id=1,include_execution_config=True)['config']
    assert accepted['_turnPrincipalTenantId']=='accepted-app'
    assert captured[0]['_turnPrincipalTenantId']=='accepted-app'
    project_id=resolve_task_project(principal,captured[0],client_factory=get_storage_client)
    context=task_start_context(principal,{'role':'user'},project_id=project_id)
    started=application_storage.command('turn.attempt.start',{'user_id':1,'attempt_id':attempt,
        'task_id':task,'automation_context':context},None)
    job_id,_=task_started_job(1,context,attempt_id=attempt,task_id=task,
        conversation_id=value['turn']['conversationId'],turn_id=value['turn']['turnId'],started_at_ms=started['startedAt'])
    job=application_storage.query('worker_job.get',{'user_id':1,'task_id':job_id})
    assert job['payload']['event']['projectId']==project['id']
    assert job['payload']['principal']['tenant_id']=='accepted-app'


def test_execution_config_read_failure_settles_attempt_before_worker_start(application_storage,monkeypatch):
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.conversation_sync.command_service import AttemptStartFailure
    import lib.conversation_sync.command_service as commands
    from lib.storage.errors import StorageError
    conversation='config-failure-'+uuid.uuid4().hex
    application_storage.command('conversation.create',{'conv_id':conversation,'user_id':1,
        'title':'Read failure','settings':{}},'create-'+conversation)
    calls=[]
    monkeypatch.setattr(conversation_turn_commands,'_start_task',lambda *args,**kwargs:calls.append(args))
    def unavailable(*args,**kwargs):
        assert kwargs['include_execution_config'] is True
        raise StorageError('database_unavailable','Injected private config read failure')
    monkeypatch.setattr(commands,'get_attempt',unavailable)
    with pytest.raises(AttemptStartFailure):
        conversation_turn_commands.create_turn(conversation,1,{'commandId':'accept',
            'message':{'role':'user','content':'Read failure'},'config':{'model':'synthetic'}},
            request_started_at=time.time())
    page=application_storage.query('turn.list',{'conversation_id':conversation,'user_id':1,'limit':8})
    turn=next(row for row in page if row['actor']=='assistant')
    attempt=application_storage.query('turn.attempt.get',{'user_id':1,'attempt_id':turn['currentAttemptId']})
    assert attempt['status']=='failed' and attempt['startedAt'] is None
    # The terminal record retains the internal dispatch claim as its audit
    # identity; it must never acquire a physical executor task identity.
    assert attempt['taskId'].startswith('@dispatching:')
    assert calls==[]
