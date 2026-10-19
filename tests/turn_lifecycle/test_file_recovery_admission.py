"""Real publication and common model receipts gate subsequent writes on selected stores."""
import json
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_interrupted_file_effect_receipt import storage as storage
from tests.turn_lifecycle.test_file_effect_inspection import turn_service as turn_service,owner as owner,project as project,_task,_discard,write
from lib.tasks_pkg.handlers.project import _handle_project_tool
from lib.tasks_pkg.handlers.tool_effect_inspection import handle_tool_effect_inspection
from lib.tools.tool_effect_inspection import TOOL_EFFECT_INSPECTION

pytestmark=pytest.mark.unit


def prepare(task,project):
    definitions=[{'type':'function','function':{'name':'write_file','parameters':{
        'type':'object','properties':{'path':{'type':'string'},'content':{'type':'string'},'append':{'type':'boolean'}},
        'required':['path','content']}}},TOOL_EFFECT_INSPECTION.provider_schema()]
    task['_executable_tool_catalog']=definitions
    from lib.tools.contracts import compile_execution_contract_documents
    task['_toolContractDocumentsByName']=compile_execution_contract_documents(definitions)
    task['_toolContractDocumentsByName']['inspect_tool_effect']=TOOL_EFFECT_INSPECTION.search_document()
    task['_tool_env']=SimpleNamespace(resolve=lambda name:handle_tool_effect_inspection if name=='inspect_tool_effect' else _handle_project_tool,
        write_names={'write_file'},idempotent_names=set(),confirmation_names=set())
    task.update(_attended=True,messages=[],model='fixture')
    task['config'].update(projectPath=str(project),tools={'resultEnvelope':'v2'})
    return definitions


def invoke(task,project,definitions,name,arguments,number):
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    from lib.tasks_pkg.tool_dispatch._pipeline import execute_tool_pipeline
    message={'role':'assistant','content':'','tool_calls':[{'id':f'call-{number}','type':'function',
        'function':{'name':name,'arguments':json.dumps(arguments)}}]}
    task['messages'].append(message)
    parsed,_=parse_tool_calls(message,task,number,max((r.get('roundNum',0) for r in task['toolRounds']),default=0),True)
    execute_tool_pipeline(task,parsed,{'autoApply':True},str(project),True,definitions,task['messages'],[],number,'fixture')
    return parsed[0][5],task['messages'][-1]['content']


def test_real_append_waits_for_observed_publication_then_replans_and_rechecks(
        storage,turn_service,owner,project,monkeypatch):
    from lib.project_mod.write_tools import _ops
    from lib.conversation_sync import repository
    repository_type=repository.SidecarConversationSyncRepository
    monkeypatch.setattr(repository,'SidecarConversationSyncRepository',
        lambda:repository_type(client_factory=lambda write=False:storage.client))
    _,task=_task(turn_service,storage,'recovery-write-admission')
    original=_ops._record_modification_checked
    monkeypatch.setattr(_ops,'_record_modification_checked',lambda *a,**kw:(_ for _ in ()).throw(OSError('lost ACK')))
    try:
        source,result=write(task,project,append=True)
        assert source['status']=='error' and (project/'new.txt').read_text()=='hello\n', result
        operation=source['executionEffect']['operationId'];definitions=prepare(task,project)
        arguments={'path':'new.txt','content':'again\n','append':True}
        row,result=invoke(task,project,definitions,'write_file',arguments,1)
        assert row['status']=='rejected' and 'tool_effect_inspection_required' in result
        assert (project/'new.txt').read_text()=='hello\n'
        row,result=invoke(task,project,definitions,'inspect_tool_effect',{'operation_id':operation},2)
        assert json.loads(result)['outcome']=='file_published'
        assert row['executionEffect']['state']=='returned' and row['toolContent']==result
        # A new caller intent after the observed round, still through normal file authority.
        monkeypatch.setattr(_ops,'_record_modification_checked',original)
        row,result=invoke(task,project,definitions,'write_file',arguments,3)
        assert row['status']=='done',result
        assert (project/'new.txt').read_text()=='hello\nagain\n'
        # Prepared-only historical proof is no longer current. A retained read cannot authorize another append.
        row,result=invoke(task,project,definitions,'write_file',arguments,4)
        assert row['status']=='rejected' and 'tool_effect_inspection_changed' in result
        assert (project/'new.txt').read_text()=='hello\nagain\n'
        assert source['status']=='error'
    finally:_discard(task)


def test_continue_rehydrates_the_file_dependency_before_any_new_write(
        storage,turn_service,owner,project,monkeypatch):
    import threading
    from lib.project_mod.write_tools import _ops
    from lib.conversation_sync import repository
    from lib.conversation_sync.command_service import _resume_executor_config
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.identity import PrincipalContext
    from tests.turn_lifecycle.test_interrupted_file_effect_receipt import LostExecutor
    repository_type=repository.SidecarConversationSyncRepository
    monkeypatch.setattr(repository,'SidecarConversationSyncRepository',
        lambda:repository_type(client_factory=lambda write=False:storage.client))
    _,old=_task(turn_service,storage,'continued-file-admission')
    original=_ops._record_modification_checked
    def interrupted(*a,**kw):raise LostExecutor()
    monkeypatch.setattr(_ops,'_record_modification_checked',interrupted)
    try:
        with pytest.raises(LostExecutor):write(old,project,append=True)
        source={k:old[k] for k in ('id','convId','_turnId','_attemptId')}
        operation=old['toolRounds'][0]['executionEffect']['operationId']
    finally:_discard(old)
    monkeypatch.setattr(_ops,'_record_modification_checked',original)
    storage.stop();storage.start();assert turn_service.recover_running_attempts()==1
    recovered=turn_service.get_turn(source['convId'],source['_turnId'],user_id=1)
    option=next(o for o in recovered['settlement']['resumeOptions'] if o['operation']=='continue')
    accepted=turn_service.create_attempt(source['convId'],source['_turnId'],command_id='resume-'+source['id'],
        operation='continue',expected_projection_revision=recovered['projectionRevision'],
        resume_anchor=option['anchor'],config={'model':'gpt-4o'},user_id=1)
    target='resumed-'+source['id'];turn_service.bind_task(accepted['attempt']['attemptId'],target,user_id=1)
    config=_resume_executor_config('continue',accepted['turn']['projection'])
    task=chat_task_runtime.create(user_id=1,task_id=target)
    task.update(convId=source['convId'],_turnId=source['_turnId'],_attemptId=accepted['attempt']['attemptId'],
        config=config,status='running',content='',thinking='',toolRounds=[],content_lock=threading.Lock(),
        _principalContext=PrincipalContext.user(subject_id='test-user:1',owner_user_id=1).to_payload())
    try:
        definitions=prepare(task,project)
        apply_resume_state(task=task,cfg=config,messages=task['messages'],model='fixture',tid=target)
        assert task['_checkpointToolRounds'][0]['executionEffect']['operationId']==operation
        arguments={'path':'new.txt','content':'resumed work\n','append':True}
        row,result=invoke(task,project,definitions,'write_file',arguments,1)
        assert row['status']=='rejected' and 'tool_effect_inspection_required' in result
        assert (project/'new.txt').read_text()=='hello\n'
        _,result=invoke(task,project,definitions,'inspect_tool_effect',{'operation_id':operation},2)
        assert json.loads(result)['outcome']=='file_published'
        row,result=invoke(task,project,definitions,'write_file',arguments,3)
        assert row['status']=='done',result
        assert (project/'new.txt').read_text()=='hello\nresumed work\n'
    finally:_discard(task)
