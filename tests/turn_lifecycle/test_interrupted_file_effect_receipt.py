"""Retirement preserves exact unresolved file calls through ordinary Continue.

Actual file publication plus lost response, selected-store restart and resumed
model/tool history. This does not grant native in-flight checkpoint takeover.
"""
import copy
import json
import pytest

from tests.turn_lifecycle.test_file_effect_inspection import (
    turn_service as turn_service, owner as owner, project as project, _task, _discard, write, service_for,
)
from lib.tools.effect_interruption import record_interrupted_file_effects

pytestmark = pytest.mark.unit


import os
_BACKENDS=['sqlite','tofudb'] + (['postgres'] if os.environ.get('TOFU_STORAGE_TEST_POSTGRES')=='1' else [])


@pytest.fixture(params=_BACKENDS)
def storage(request, tmp_path, monkeypatch):
    from contextlib import ExitStack
    from pathlib import Path
    from lib.storage import StorageSupervisor
    with ExitStack() as resources:
        if request.param=='postgres':
            from tests.support.postgres_isolation import isolated_postgres_dsn
            isolated=resources.enter_context(isolated_postgres_dsn(
                Path(os.environ['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE']),tmp_path))
            monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE',str(isolated))
        if request.param=='tofudb':
            from scripts.check_tofudb_engine_drift import resolve_deployment_binary
            monkeypatch.setenv('TOFUDB_BIN',resolve_deployment_binary(Path(__file__).resolve().parents[2]))
            monkeypatch.setenv('TOFUDB_TRANSPORT','daemon')
        supervisor=StorageSupervisor(project_root=tmp_path,backend=request.param,startup_timeout=60)
        supervisor.start()
        try:yield supervisor
        finally:supervisor.stop()


def _row():
    return {'taskId':'task','attemptId':'attempt','toolCallId':'call','toolName':'write_file',
            'toolArgs':{'path':'a.txt','content':'hello','append':True},'status':'running',
            'executionEffect':{'scope':'attempt','recovery':'reconcile','state':'entered',
                'operationId':'a'*32, 'inspection':{'conversationId':'conv','turnId':'turn',
                    'operationId':'a'*32,'ownerScope':'b'*24},
                'reconciliation':{'kind':'project_file_write','taskId':'task'}}}


def retire(projection):
    return record_interrupted_file_effects(projection, conversation_id='conv',turn_id='turn',
                                           attempt_id='attempt',task_id='task')


def test_interruption_is_a_nonretryable_observation_and_idempotent():
    p={'toolRounds':[_row()]};original=copy.deepcopy(p)
    assert retire(p)==1
    row=p['toolRounds'][0];error=json.loads(row['toolContent'])
    assert error['status']=='error' and error['code']=='tool_effect_unknown' and error['retryable'] is False
    assert 'inspect_tool_effect(' in error['nextAction'] and '"turn_id":"turn"' in error['nextAction']
    assert row['executionEffect']=={**original['toolRounds'][0]['executionEffect'],'state':'unknown'}
    assert row['toolArgs']==original['toolRounds'][0]['toolArgs']
    assert row['status']=='aborted'
    assert retire(p)==0


@pytest.mark.parametrize('fault', ['foreign_task','foreign_attempt','foreign_conv','foreign_turn',
    'foreign_operation','wrong_scope','wrong_recovery','returned','not_started','read_tool',
    'captured_result','invalid_result','model_result','invalid_model_result','duplicate','discarded','unbound'])
def test_unproven_or_already_captured_occurrences_are_unchanged(fault):
    row=_row();p={'toolRounds':[row]};effect=row['executionEffect']
    if fault=='foreign_task':row['taskId']='other'
    elif fault=='foreign_attempt':row['attemptId']='other'
    elif fault=='foreign_conv':effect['inspection']['conversationId']='other'
    elif fault=='foreign_turn':effect['inspection']['turnId']='other'
    elif fault=='foreign_operation':effect['inspection']['operationId']='c'*32
    elif fault=='wrong_scope':effect['scope']='process'
    elif fault=='wrong_recovery':effect['recovery']='read_only'
    elif fault in ('returned','not_started'):effect['state']=fault
    elif fault=='read_tool':row['toolName']='read_file'
    elif fault=='captured_result':row['toolContent']=''
    elif fault=='invalid_result':row['toolContent']={}
    elif fault=='model_result':row['toolModelContent']=[{'type':'text','text':'receipt'}]
    elif fault=='invalid_model_result':row['toolModelContent']={}
    elif fault=='duplicate':p['toolRounds'].append(copy.deepcopy(row))
    elif fault=='discarded':row['_providerAttemptDiscarded']=True
    else:effect.pop('inspection')
    original=copy.deepcopy(p);assert retire(p)==0;assert p==original


class LostExecutor(BaseException):
    pass


@pytest.mark.parametrize('resume_operation', ['continue','checkpoint_resume'])
def test_restart_closes_only_the_interruption_and_preserves_query_and_resume(
        storage, turn_service, owner, project, monkeypatch, resume_operation):
    from lib.project_mod.write_tools import _ops
    from lib.conversation_sync.command_service import _resume_executor_config
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state
    _,task=_task(turn_service,storage,'retired-file-effect')
    def lose_executor(*a,**kw):raise LostExecutor()
    monkeypatch.setattr(_ops,'_record_modification_checked',lose_executor)
    try:
        with pytest.raises(LostExecutor):write(task,project,append=True)
        original=turn_service.get_turn(task['convId'],task['_turnId'],user_id=1)
        row=original['projection']['toolRounds'][0]
        assert row.get('toolContent') is None and row['executionEffect']['state']=='entered'
        operation=row['executionEffect']['operationId'];source={k:task[k] for k in ('id','convId','_turnId','_attemptId')}
    finally:
        _discard(task)
    # The in-process owner has been joined/discarded; reopen the selected authority.
    storage.stop();storage.start()
    assert turn_service.recover_running_attempts()==1
    recovered=turn_service.get_turn(source['convId'],source['_turnId'],user_id=1)
    closed=recovered['projection']['toolRounds'][0]
    expected=copy.deepcopy(original['projection'])
    assert record_interrupted_file_effects(expected,conversation_id=source['convId'],turn_id=source['_turnId'],
        attempt_id=source['_attemptId'],task_id=source['id'])==1
    assert closed==expected['toolRounds'][0] # Same contract on both selected authorities.
    assert recovered['status']=='interrupted' and recovered['settlement']['cause']=='server_restart'
    assert turn_service.recover_running_attempts()==0
    assert service_for(storage).inspect_tool_effect(source['convId'],source['_turnId'],1,
        operation_id=operation)['outcome']=='file_published'
    option=next(o for o in recovered['settlement']['resumeOptions'] if o['operation']==resume_operation)
    accepted=turn_service.create_attempt(source['convId'],source['_turnId'],command_id='resume-'+source['id'],
        operation=resume_operation,expected_projection_revision=recovered['projectionRevision'],
        resume_anchor=option['anchor'],config={'model':'gpt-4o'},user_id=1)
    config=_resume_executor_config(resume_operation,accepted['turn']['projection'])
    prepared=prepare_resume_state(config)
    feedback=[m for m in prepared.checkpoint_messages if m.get('role')=='tool']
    assert len(feedback)==1 and json.loads(feedback[0]['content'])['code']=='tool_effect_unknown'
    assert operation in feedback[0]['content']
    # Original producing reference survives Continue and can still be inspected.
    assert service_for(storage).inspect_tool_effect(source['convId'],source['_turnId'],1,
        operation_id=operation)['outcome']=='file_published'
    assert (project/'new.txt').read_text()=='hello\n'


def test_oversize_escaped_identity_retains_the_gap_instead_of_clipping_a_query():
    row=_row();turn='\x01'*128;row['executionEffect']['inspection']['turnId']=turn
    projection={'toolRounds':[row]};original=copy.deepcopy(projection)
    assert record_interrupted_file_effects(projection,conversation_id='conv',turn_id=turn,
        attempt_id='attempt',task_id='task')==0
    assert projection==original


def test_killed_executor_and_reopened_store_resume_to_fresh_inspection_without_reappend(
        storage, turn_service, owner, project, monkeypatch, tmp_path):
    import os
    from pathlib import Path
    import signal
    import subprocess
    import sys
    import time
    from lib.project_mod import modifications
    from lib.conversation_sync import repository
    from lib.conversation_sync.command_service import _resume_executor_config
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.identity import PrincipalContext
    from tests.turn_lifecycle.test_file_effect_agent_read import read
    client=storage.client;native=hasattr(client,'_secret')
    ready=tmp_path/'writer-ready.json';logfile=tmp_path/'writer.log'
    values={'endpoint':client.endpoint,'token':client._secret.decode() if native else client._token,
            'native':native,'owner_id':getattr(client,'_owner_id',1),'tenant_id':getattr(client,'_tenant_id',None),
            'ready':str(ready),'project':str(project),'journal':modifications.SESSIONS_DIR}
    with logfile.open('w') as log:
        process=subprocess.Popen([sys.executable,'-m','tests.support.file_effect_crash_writer'],
            cwd=Path(__file__).resolve().parents[2],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,
            text=True,start_new_session=True)
        try:
            process.stdin.write(json.dumps(values));process.stdin.close()
            deadline=time.monotonic()+30
            while not ready.exists():
                assert process.poll() is None,logfile.read_text()[-3000:]
                assert time.monotonic()<deadline,'writer did not reach file publication'
                time.sleep(.02)
            source=json.loads(ready.read_text());operation=source['operationId']
            assert (project/'new.txt').read_text()=='hello\n'
            observed=service_for(storage).inspect_tool_effect(source['convId'],source['_turnId'],1,operation_id=operation)
            assert observed['outcome']=='unknown' and observed['reason']=='writer_active'
            os.killpg(process.pid,signal.SIGKILL)
            assert process.wait(timeout=10)==-signal.SIGKILL
        finally:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=10)
    storage.stop();storage.start()
    assert turn_service.recover_running_attempts()==1
    recovered=turn_service.get_turn(source['convId'],source['_turnId'],user_id=1)
    row=recovered['projection']['toolRounds'][0]
    assert row['executionEffect']['state']=='unknown' and json.loads(row['toolContent'])['retryable'] is False
    option=next(o for o in recovered['settlement']['resumeOptions'] if o['operation']=='continue')
    accepted=turn_service.create_attempt(source['convId'],source['_turnId'],command_id='continue-'+source['id'],
        operation='continue',expected_projection_revision=recovered['projectionRevision'],
        resume_anchor=option['anchor'],config={'model':'gpt-4o'},user_id=1)
    target_id='successor-'+source['id'];turn_service.bind_task(accepted['attempt']['attemptId'],target_id,user_id=1)
    config=_resume_executor_config('continue',accepted['turn']['projection'])
    task=chat_task_runtime.create(user_id=1,task_id=target_id)
    task.update(convId=source['convId'],_turnId=source['_turnId'],_attemptId=accepted['attempt']['attemptId'],
        config=config,model='gpt-4o',status='running',content='',thinking='',messages=[],toolRounds=[],
        content_lock=__import__('threading').Lock(),
        _principalContext=PrincipalContext.user(subject_id='test-user:1',owner_user_id=1).to_payload())
    repository_type=repository.SidecarConversationSyncRepository
    monkeypatch.setattr(repository,'SidecarConversationSyncRepository',
        lambda:repository_type(client_factory=lambda write=False:storage.client))
    try:
        messages=[];apply_resume_state(task=task,cfg=config,messages=messages,model='gpt-4o',tid=target_id)
        feedback=[m for m in messages if m.get('role')=='tool']
        assert len(feedback)==1 and operation in feedback[0]['content']
        _,observed=read(task,operation,2)
        assert observed['outcome']=='file_published' and observed['reason']=='prepared_file_applied'
        assert (project/'new.txt').read_text()=='hello\n'
        current=turn_service.get_turn(source['convId'],source['_turnId'],user_id=1)
        originals=[r for r in current['projection']['toolRounds'] if r.get('executionEffect',{}).get('operationId')==operation]
        assert len(originals)==1 and originals[0]['executionEffect']['state']=='unknown'
        assert originals[0]['toolContent']==row['toolContent']
    finally:_discard(task)
