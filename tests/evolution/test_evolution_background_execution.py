"""Owned current-task background completion is captured without waiting/replay."""
import threading
import uuid
import pytest
from lib.evolution.tool_evidence import observe_command_execution
from lib.tasks_pkg import background_commands as commands
from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER

pytestmark=pytest.mark.unit


@pytest.fixture
def command():
    handle='bg_'+uuid.uuid4().hex[:12]
    task={'id':'task','_userId':OWNER,'toolRounds':[{'toolName':'run_command',
        'toolCallId':'launch','status':'done','results':[{'exitCode':'background','backgroundCommandId':handle}]}],
        'messages':[]}
    assert commands.register_handed_off_command(command_id=handle,task=task,command='experiment',done=threading.Event())
    yield task,handle
    with commands._lock:commands._registry.pop(handle,None)


def attach_background_launch(fixture,executor):
    """Register the synthetic producer against the actual execution carrier."""
    from copy import deepcopy
    source,handle=fixture
    with commands._lock:commands._registry.pop(handle,None)
    assert commands.register_handed_off_command(command_id=handle,task=executor,
        command='experiment',done=threading.Event())
    executor['toolRounds']=deepcopy(source['toolRounds'])


def complete(handle,exit_code=0,error=None):
    commands.finish_handed_off_command(command_id=handle,
        result=f'$ experiment\nmeasured result\n[exit code: {exit_code}]',error=error)


def test_real_owned_completion_is_observed_after_context_compaction(command):
    task,handle=command
    assert not observe_command_execution(task,user_id=OWNER)
    complete(handle)
    assert observe_command_execution(task,user_id=OWNER)
    assert task['messages']==[]
    assert not observe_command_execution(dict(task,_userId=OWNER+1),user_id=OWNER+1)


@pytest.mark.parametrize('change',['unfinished','failed','interrupted','foreign_task_handle',
    'wrong_owner','metadata_mismatch','missing_current_launch','display_claim_only','missing_handle','registry_error'])
def test_unproven_background_completion_cannot_pass(command,change):
    task,handle=command
    if change!='unfinished':complete(handle,1 if change=='failed' else 0,'worker failed' if change=='registry_error' else None)
    if change=='interrupted':commands.finish_handed_off_command(command_id=handle,result='$ experiment\n[Command aborted by user]\n[exit code: -1]',error=None)
    elif change=='foreign_task_handle':task['toolRounds'][0]['results'][0]['backgroundCommandId']='bg_foreign'
    elif change=='wrong_owner':task['_userId']=OWNER+1
    elif change=='metadata_mismatch':task['toolRounds'][0]['backgroundCommandId']='bg_other'
    elif change=='missing_current_launch':task['toolRounds']=[]
    elif change=='display_claim_only':
        task['toolRounds']=[]
        task['_bgCommandInjects']=[{'previews':[{'backgroundCommand':{'commandId':handle,'status':'completed','exitCode':0}}]}]
    elif change=='missing_handle':
        with commands._lock:commands._registry.pop(handle,None)
    assert not observe_command_execution(task,user_id=task['_userId'])


def test_completed_background_is_frozen_in_terminal_outcome_without_dispatch_replay(runtime,command):
    from lib.evolution.stage_outcomes import read_metadata
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Reproduce the frozen comparison','require_command_execution':True,
        'max_attempts':1},user_id=OWNER)
    task,handle=command
    executor=tasks[0]
    attach_background_launch(command,executor)
    complete(handle)
    result=finish(runtime,executor,'The background command completed; substantive correctness still needs review.')
    assert result['status']=='reviewing' and len(tasks)==2
    metadata=read_metadata(run['run_id'],executor['id'],user_id=OWNER)
    assert metadata['command_execution_observed'] is True
    with commands._lock:commands._registry.pop(handle,None)
    final=finish(runtime,tasks[1],review())
    assert final['status']=='completed' and len(tasks)==2
    assert read_metadata(run['run_id'],executor['id'],user_id=OWNER)['command_execution_observed'] is True


def test_snapshot_reads_are_owner_scoped_nonblocking_and_deduplicated(command,monkeypatch):
    task,handle=command
    task['toolRounds'].append(dict(task['toolRounds'][0]))
    complete(handle)
    calls=[]
    reader=commands.check_background_command
    def snapshot(command_id,**kwargs):
        calls.append((command_id,kwargs))
        return reader(command_id,**kwargs)
    monkeypatch.setattr(commands,'check_background_command',snapshot)
    assert observe_command_execution(task,user_id=OWNER)
    assert calls==[(handle,{'user_id':OWNER,'wait_s':0,'task_id':task['id']})]


def test_foreground_completion_never_probes_background_registry(monkeypatch):
    monkeypatch.setattr(commands,'check_background_command',lambda *a,**k:pytest.fail('No background lookup required'))
    task={'_userId':OWNER,'toolRounds':[{'toolName':'run_command','toolCallId':'call',
        'status':'done','results':[{'exitCode':'0'}]}]}
    assert observe_command_execution(task,user_id=OWNER)


def test_failed_background_outputs_are_archived_without_satisfying_success(command):
    from lib.evolution.tool_evidence import capture_background_results,background_execution_observed
    task,handle=command
    complete(handle,exit_code=1,error='measured worker failure')
    observations=capture_background_results(task,user_id=OWNER)
    assert len(observations)==1
    assert observations[0]['content'].endswith('[exit code: 1]')
    assert observations[0]['error']=='measured worker failure'
    assert not background_execution_observed(observations)


@pytest.mark.parametrize('reply',[None,{}, {'command_id':'bg_other','status':'completed','result':'claimed'},
    {'status':'completed','result':None},{'status':'completed','result':42},{'status':'unknown'}])
def test_declared_launch_retains_unresolved_producer_reply(command,monkeypatch,reply):
    from lib.evolution.tool_evidence import capture_background_state
    task,handle=command
    response=dict(reply,command_id=handle) if isinstance(reply,dict) and 'command_id' not in reply else reply
    monkeypatch.setattr(commands,'check_background_command',lambda *args,**kwargs:response)
    observed,pending=capture_background_state(task,user_id=OWNER)
    assert not observed and pending==[{'command_id':handle,'source_tool_call_id':'launch'}]


def test_same_user_other_task_cannot_supply_completion_or_be_cancelled(command):
    from lib.evolution.tool_evidence import capture_background_state
    source,handle=command;complete(handle)
    other={**source,'id':'different-experiment'}
    assert commands.check_background_command(handle,user_id=OWNER) is not None
    assert commands.check_background_command(handle,user_id=OWNER,task_id=other['id']) is None
    assert not observe_command_execution(other,user_id=OWNER)
    observed,pending=capture_background_state(other,user_id=OWNER)
    assert not observed and pending[0]['command_id']==handle
    assert commands.check_background_command(handle,user_id=OWNER,task_id=source['id'])['status']=='completed'


def test_task_restricted_cancel_never_signals_another_experiment(command):
    source,handle=command
    assert not commands.kill_background_command(handle,user_id=OWNER,task_id='different-experiment')
    assert not source.get('aborted')
    assert commands.kill_background_command(handle,user_id=OWNER,task_id=source['id'])
    assert source['aborted']


def test_standalone_background_identity_preserves_owner_checked_completion(command):
    task, handle = command
    task['toolRounds'][0]['toolName'] = 'code_exec'
    assert not observe_command_execution(task, user_id=OWNER)
    complete(handle)
    assert observe_command_execution(task, user_id=OWNER)
    assert not observe_command_execution(task, user_id=OWNER + 1)
