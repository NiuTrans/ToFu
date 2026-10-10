"""Durable waiting joins completed owned work without repeating an executor."""
import pytest
from tests.evolution.test_evolution_runs import runtime as runtime,finish,review,OWNER
from tests.evolution.test_evolution_background_execution import command as command,complete,attach_background_launch
from lib.tasks_pkg import background_commands as commands

pytestmark=pytest.mark.unit


def wait(runtime,command):
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Reproduce the frozen comparison','require_command_execution':True,'max_attempts':1},user_id=OWNER)
    source,handle=command
    attach_background_launch(command,tasks[0])
    pending=finish(runtime,tasks[0],'The experiment was submitted; its outcome is not yet known.')
    assert pending['status']=='executing' and pending['background_wait']
    assert len(tasks)==1 and len(pending['receipts'])==1
    return pending,handle


def test_reaper_waits_then_admits_one_review_and_keeps_original_immutable(runtime,command,monkeypatch):
    from lib.evolution import stage_outcomes as outcomes
    runs,_,tasks=runtime
    run,handle=wait(runtime,command)
    parent=outcomes.records.get_record(outcomes.NAMESPACE,outcomes._key(run['task_id'],OWNER))
    for _ in range(3):
        waiting=runs.advance_run(run['run_id'],user_id=OWNER)
        assert waiting['background_wait'] and len(waiting['receipts'])==1 and len(tasks)==1
    loader=runs.default_task_loader
    monkeypatch.setattr(runs,'default_task_loader',lambda tid,user_id:None if tid==run['task_id'] else loader(tid,user_id=user_id))
    complete(handle)
    resumed=runs.advance_run(run['run_id'],user_id=OWNER)
    assert resumed['status']=='reviewing' and len(tasks)==2
    assert resumed['actuals']['tokens']==15
    assert resumed['background_evidence_source']['source_kind']=='background_evidence'
    assert 'additional_original_sources' in tasks[1]['messages'][0]['content']
    assert outcomes.records.get_record(outcomes.NAMESPACE,outcomes._key(run['task_id'],OWNER))==parent
    with commands._lock:commands._registry.pop(handle,None)
    page=outcomes.page(run['run_id'],run['task_id'],user_id=OWNER,source_kind='background_evidence')
    assert 'measured result' in page['content'] and page['background_evidence_available']
    assert runs.advance_run(run['run_id'],user_id=OWNER)['status']=='reviewing' and len(tasks)==2


@pytest.mark.parametrize('exit_code',[1,-1])
def test_failed_completed_background_never_mints_success(runtime,command,exit_code):
    runs,_,tasks=runtime;run,handle=wait(runtime,command)
    complete(handle,exit_code)
    result=runs.advance_run(run['run_id'],user_id=OWNER)
    assert result['status']=='failed' and result['error']=='background_command_execution_failed'
    assert len(tasks)==1 and len(result['receipts'])==1
    assert result['background_evidence_source']['source_kind']=='background_evidence'


def test_cancel_signals_only_owned_pending_background_and_cannot_resume(runtime,command):
    runs,_,tasks=runtime;run,handle=wait(runtime,command)
    cancelled=runs.cancel_run(run['run_id'],user_id=OWNER)
    assert cancelled['status']=='cancelled'
    with commands._lock:assert commands._registry[handle]['task']['aborted']
    complete(handle)
    assert runs.advance_run(run['run_id'],user_id=OWNER)['status']=='cancelled' and len(tasks)==1


def test_missing_handle_requires_attention_without_executor_replay(runtime,command):
    runs,_,tasks=runtime;run,handle=wait(runtime,command)
    with commands._lock:commands._registry.pop(handle,None)
    result=runs.advance_run(run['run_id'],user_id=OWNER)
    assert result['status']=='failed' and result['error']=='background_command_result_unavailable'
    assert len(tasks)==1


def test_committed_wait_survives_lost_terminal_callback_and_resumes_without_executor(runtime,command,monkeypatch):
    from tests.evolution.test_evolution_stage_outcomes import terminal
    from lib.tasks_pkg.manager import _persist,_terminal
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.storage import get_storage_client
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Await an admitted durable background experiment','require_command_execution':True,'max_attempts':1},user_id=OWNER)
    source,handle=command;executor=terminal(tasks[0],'Experiment submitted; results remain pending.')
    attach_background_launch(command,executor)
    executor['_executionSession'].settle(ExecutionPhase.COMPLETED,cause='controlled_test')
    monkeypatch.setattr(_terminal,'notify_terminal_listeners',lambda _:(_ for _ in ()).throw(RuntimeError('lost callback')))
    assert _persist.persist_task_result(executor,_defer_heavy_release=True) is False
    row=get_storage_client().query('task_results.replay_get',{'key':executor['id'],'user_id':OWNER,'include_metadata':True})
    assert row['metadata']['terminalContext']['outcome']['pending_background_commands'][0]['command_id']==handle
    loader=runs.default_task_loader
    monkeypatch.setattr(runs,'default_task_loader',lambda tid,user_id:None if tid==executor['id'] else loader(tid,user_id=user_id))
    waiting=runs.advance_run(run['run_id'],user_id=OWNER)
    assert waiting['background_wait'] and len(tasks)==1
    complete(handle)
    resumed=runs.advance_run(run['run_id'],user_id=OWNER)
    assert resumed['status']=='reviewing' and len(tasks)==2 and resumed['actuals']['tokens']==15
    assert len(resumed['receipts'])==1 and resumed['attempt']==1


def test_unread_background_original_refuses_positive_review(runtime,command):
    runs,_,tasks=runtime;run,handle=wait(runtime,command)
    complete(handle)
    reviewing=runs.advance_run(run['run_id'],user_id=OWNER)
    assert reviewing['status']=='reviewing'
    final=finish(runtime,tasks[-1],review())
    assert final['status']=='needs_attention'
    assert final['review']['outcome']!='success'
    assert 'background' in next(row for row in final['review']['criterion_results'] if row['criterion_id']=='objective')['evidence_ref']


def test_wait_respects_original_deadline_and_signals_pending_command(runtime,command,monkeypatch):
    runs,_,tasks=runtime
    source,handle=command
    run=runs.submit_run({'objective':'Wait within the original ceiling','require_command_execution':True,'max_attempts':1,
        'budget':{'max_tokens':1000,'max_tool_calls':100,'max_wall_clock_seconds':10}},user_id=OWNER)
    attach_background_launch(command,tasks[0])
    waiting=finish(runtime,tasks[0],'Background experiment pending')
    budget=dict(waiting['budget'])
    monkeypatch.setattr(runs.time,'time',lambda:waiting['created_at']+11)
    stopped=runs.advance_run(run['run_id'],user_id=OWNER)
    assert stopped['budget']==budget and stopped['status']=='failed'
    assert stopped['error']=='background_execution_budget_exhausted' and len(tasks)==1
    with commands._lock:assert commands._registry[handle]['task']['aborted']


def test_review_receives_complete_background_original_before_success(runtime,command):
    import json
    from lib.evolution.source_tools import execute,fully_read
    from tests.evolution.test_evolution_source_tools import delivered
    runs,_,tasks=runtime;run,handle=wait(runtime,command);complete(handle)
    reviewing=runs.advance_run(run['run_id'],user_id=OWNER);reviewer=tasks[-1]
    reference=reviewing['background_evidence_source']
    result=execute(reviewer,{'action':'read','run_id':run['run_id'],'task_id':reference['task_id'],
        'source_kind':'background_evidence','source_sha256':reference['source_sha256'],'offset':0})
    assert not fully_read(reviewer,reference)
    delivered(reviewer,{'action':'read','run_id':run['run_id'],'task_id':reference['task_id'],
        'source_kind':'background_evidence','source_sha256':reference['source_sha256'],'offset':0})
    assert fully_read(reviewer,reference)
    final=finish(runtime,reviewer,review())
    assert final['status']=='completed' and len(tasks)==2


def test_wait_observes_original_budget_recovery_and_cas_not_duplicate_workers(runtime,command,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    runs,_,tasks=runtime;run,handle=wait(runtime,command);complete(handle)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:runs.advance_run(run['run_id'],user_id=OWNER),range(2)))
    current=runs.get_run(run['run_id'],user_id=OWNER)
    assert current['status']=='reviewing' and len(tasks)==2
    assert len(current['receipts'])==1 and current['actuals']['tokens']==15


def test_missing_handle_during_registry_recovery_remains_waiting(runtime,command,monkeypatch):
    runs,_,tasks=runtime;run,handle=wait(runtime,command)
    with commands._lock:commands._registry.pop(handle,None)
    monkeypatch.setattr(commands,'recovery_needs_retry',lambda:True)
    assert runs.advance_run(run['run_id'],user_id=OWNER)['background_wait'] and len(tasks)==1


def test_wait_poll_preserves_header_version_and_readonly_snapshot(runtime,command):
    runs,store,tasks=runtime;run,handle=wait(runtime,command)
    writes=store.writes
    for _ in range(5):
        assert runs.advance_run(run['run_id'],user_id=OWNER)['background_wait']
        assert runs.get_run_snapshot(run['run_id'],user_id=OWNER)['status']=='executing'
    assert store.writes==writes and len(tasks)==1


def test_late_source_page_does_not_rescan_lifetime_body(runtime,command,monkeypatch):
    from lib.evolution import stage_outcomes as outcomes
    runs,_,tasks=runtime;run,handle=wait(runtime,command)
    raw='$ experiment\n'+'measured data '*15000+'\n[exit code: 0]'
    commands.finish_handed_off_command(command_id=handle,result=raw,error=None)
    runs.advance_run(run['run_id'],user_id=OWNER)
    get=outcomes.records.get_record;parts=[]
    def read(namespace,*args,**kwargs):
        if namespace==outcomes.PART_NAMESPACE:parts.append(args[0])
        return get(namespace,*args,**kwargs)
    monkeypatch.setattr(outcomes.records,'get_record',read)
    page=outcomes.page(run['run_id'],run['task_id'],user_id=OWNER,source_kind='background_evidence')
    assert page['next_offset']==6000 and len(parts)==1



def test_handle_missing_before_terminal_capture_never_admits_review(runtime,command):
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Preserve every submitted experiment.',
        'require_command_execution':True,'max_attempts':1},user_id=OWNER)
    source,handle=command;attach_background_launch(command,tasks[0])
    with commands._lock:commands._registry.pop(handle,None)
    waiting=finish(runtime,tasks[0],'Submitted experiment; no producer result was received.')
    assert waiting.get('background_wait') and len(tasks)==1
    failed=runs.advance_run(run['run_id'],user_id=OWNER)
    assert failed['error']=='background_command_result_unavailable' and failed['status']=='failed'
    assert len(tasks)==1 and failed['review'] is None


def test_cancel_foreign_launch_does_not_signal_another_same_user_task(runtime,command):
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Do not adopt another experiment.',
        'require_command_execution':True,'max_attempts':1},user_id=OWNER)
    source,handle=command
    tasks[0]['toolRounds']=source['toolRounds']
    waiting=finish(runtime,tasks[0],'A copied handle is not producer ownership.')
    assert waiting['background_wait']
    cancelled=runs.cancel_run(run['run_id'],user_id=OWNER)
    assert cancelled['status']=='cancelled'
    with commands._lock:assert not commands._registry[handle]['task'].get('aborted')
    assert not source.get('aborted') and len(tasks)==1
