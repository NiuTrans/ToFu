"""A new file write needs observed facts and, if unknown, exact human takeover."""
import copy
import json
from types import SimpleNamespace

import pytest

from tests.tasks_pkg.test_native_gateway import task as make_task
from lib.conversation_sync.turn_images import turn_image_owner_scope
from lib.tasks_pkg.executor._file_recovery import check_file_recovery, recovery_receipt
from lib.tasks_pkg.executor._execute import _execute_tool_one
from lib.tasks_pkg.tool_dispatch._approval import _handle_approval, mint_approval_receipt
from lib.tasks_pkg.tool_dispatch._pipeline import _approval_stops_tool_call

pytestmark = pytest.mark.unit


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    task=make_task();task.update(convId='conv',_attemptId='attempt',_turnId='turn')
    # This fixture is an ordinary project conversation. Supply its independent
    # role lookup so recovery assertions reach file admission without a live DB.
    def unbound_project(repository, project_path):
        assert repository.user_id == task['_userId']
        assert project_path == str(tmp_path)
        return {'projectPath': project_path, 'conversationId': '',
                'revision': 0, 'availability': 'unbound'}
    monkeypatch.setattr(
        'lib.conversations.project_coordination.ProjectCoordinationRepository.read',
        unbound_project)
    reference=dict(conversationId='conv',turnId='turn',operationId='a'*32,
                   ownerScope=turn_image_owner_scope(1,'conv'))
    source=dict(toolName='write_file',taskId='producer',attemptId='old',status='aborted',
        executionEffect=dict(scope='attempt',recovery='reconcile',state='unknown',operationId='a'*32,
            inspection=reference,reconciliation=dict(kind='project_file_write',taskId='producer',
                basePath=str(tmp_path),path='file.txt')))
    task['_checkpointToolRounds']=[source]
    observed=dict(ok=True,contract='tofu.conversation-sync.tool-effect-inspection/v1',
        conversationId='conv',turnId='turn',operationId='a'*32,
        outcome='file_published',reason='undo_committed')
    calls=[]
    def inspect(owner,args):
        calls.append((owner['id'],copy.deepcopy(args)))
        return dict(observed)
    monkeypatch.setattr('lib.tasks_pkg.handlers.tool_effect_inspection.inspect_for_task',inspect)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',lambda *a,**kw:True)
    monkeypatch.setattr('lib.tasks_pkg.executor._finalize.append_event',lambda *a,**kw:True)
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._approval.append_event',lambda *a,**kw:True)
    return SimpleNamespace(task=task,source=source,observed=observed,calls=calls,root=tmp_path)


def inspect_receipt(s, **changes):
    row=dict(toolName='inspect_tool_effect',status='done',llmRound=1,taskId=s.task['id'],
        attemptId=s.task['_attemptId'],toolContent=json.dumps(s.observed),
        executionEffect=dict(scope='attempt',recovery='read_only',state='returned'))
    row.update(changes);s.task['toolRounds'].append(row);return row


def write_row():
    return dict(query='write file.txt',toolName='write_file',toolCallId='new',llmRound=2,roundNum=3,status='running')


def args(**changes):return dict(path='file.txt',content='again',append=True,**changes)


def check(s,row=None,arguments=None):
    return check_file_recovery(s.task,'write_file',arguments or args(),row or write_row(),str(s.root))


@pytest.mark.parametrize('fault',['absent','same_round','future_round','foreign_task','foreign_attempt',
    'unfinished','failed','discarded','clipped','forged_success'])
def test_only_earlier_complete_current_executor_observations_satisfy_admission(scenario,fault):
    s=scenario
    if fault!='absent':
        row=inspect_receipt(s)
        if fault=='same_round':row['llmRound']=2
        elif fault=='future_round':row['llmRound']=3
        elif fault=='foreign_task':row['taskId']='another'
        elif fault=='foreign_attempt':row['attemptId']='another'
        elif fault=='unfinished':row['executionEffect']['state']='entered'
        elif fault=='failed':row['status']='error'
        elif fault=='discarded':row['_providerAttemptDiscarded']=True
        elif fault=='clipped':row['toolContent']='{"outcome":"file_published"}'
        elif fault=='forged_success':row['toolContent']='{"ok":true}'
    decision=check(s)
    assert json.loads(decision.refusal)['code']=='tool_effect_inspection_required'
    assert not s.calls # No speculative query grants the still-uninformed call.


@pytest.mark.parametrize('outcome,reason',[
    ('file_published','undo_committed'),('file_published','prepared_file_applied'),
    ('file_not_published','prepared_file_unpublished')])
def test_known_facts_permit_a_new_call_only_after_an_observed_round(scenario,outcome,reason):
    s=scenario;s.observed.update(outcome=outcome,reason=reason);inspect_receipt(s)
    decision=check(s);assert decision.refusal is None and not decision.unknown
    assert s.calls[0][1]=={'operation_id':'a'*32,'turn_id':'turn'}
    assert s.source['executionEffect']['state']=='unknown'


def test_changed_or_unavailable_evidence_never_uses_old_observation(scenario,monkeypatch):
    s=scenario;inspect_receipt(s);s.observed.update(outcome='unknown',reason='evidence_conflict')
    assert json.loads(check(s).refusal)['code']=='tool_effect_inspection_changed'
    def fail(*a):raise OSError('storage unavailable')
    monkeypatch.setattr('lib.tasks_pkg.handlers.tool_effect_inspection.inspect_for_task',fail)
    assert json.loads(check(s).refusal)['code']=='tool_effect_inspection_unavailable'


@pytest.mark.parametrize('reason',['writer_active','evidence_unavailable'])
def test_busy_or_unavailable_file_authority_never_offers_human_override(scenario,reason):
    s=scenario;s.observed.update(outcome='unknown',reason=reason);inspect_receipt(s)
    result=check(s);assert result.refusal and not result.unknown


def test_other_file_and_reads_remain_independent_and_symlink_alias_is_covered(scenario):
    s=scenario
    assert check(s,arguments={'path':'other.txt','content':'ok'}).refusal is None
    assert check_file_recovery(s.task,'read_files',{},write_row(),str(s.root)).refusal is None
    (s.root/'alias').symlink_to(s.root/'file.txt')
    assert check(s,arguments={'path':'alias','content':'again'}).refusal
    assert not s.calls


def test_common_executor_refuses_before_handler_entry_and_effect_creation(scenario):
    s=scenario;entered=[]
    s.task['_tool_env']=SimpleNamespace(resolve=lambda name:lambda *a:(entered.append(True) or ('new','ok',False)))
    row=write_row()
    result=_execute_tool_one(s.task,{},'write_file','new',args(),3,row,{},str(s.root),True)
    assert json.loads(result[1])['code']=='tool_effect_inspection_required'
    assert not entered and 'executionEffect' not in row and row['status']=='rejected'


def gate(s, *, attended=True):
    row=write_row();results={}
    stopped=_approval_stops_tool_call(task=s.task,fn_name='write_file',fn_args=args(),tc_id='new',rn=3,
        round_entry=row,confirmation_tools=frozenset(),write_tools={'write_file'},attended=attended,
        auto_apply=True,cfg={},project_path=str(s.root),round_num=2,model='fixture',
        tool_results=results,idempotent_tools=frozenset(),cache={},tid=s.task['id'],receipt_required=True)
    return stopped,row,results


@pytest.mark.parametrize('authority',['auto_write_mode','agent_adjudicator','human'])
def test_unknown_facts_need_a_new_disclosed_human_decision_not_an_old_grant(scenario,monkeypatch,authority):
    s=scenario;s.observed.update(outcome='unknown',reason='missing_evidence');inspect_receipt(s)
    assert mint_approval_receipt(s.task,'write_file','new',args(),authority=authority)
    shown=[]
    def decide(approval_id,**kw):
        kw['on_registered']();shown.append(approval_id);return True
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._approval.request_write_approval',decide)
    monkeypatch.setattr('lib.tasks_pkg.approval_delegate.adjudicate_tool_call',lambda *a,**kw:pytest.fail('delegate must not decide takeover'))
    stopped,row,_=gate(s)
    assert not stopped and len(shown)==1
    text=json.dumps(row['approvalMeta'])
    assert 'may duplicate' in text and 'again' in text and 'a'*32 in text
    decision=check(s)
    assert recovery_receipt(s.task,'write_file','new',args(),decision,consume=True)
    assert not recovery_receipt(s.task,'write_file','new',args(),decision,consume=True)
    assert not recovery_receipt(s.task,'write_file','different',args(),decision)


def test_unknown_unattended_or_denied_decisions_execute_nothing(scenario,monkeypatch):
    s=scenario;s.observed.update(outcome='unknown',reason='evidence_conflict');inspect_receipt(s)
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._pipeline._settle_tool_result',lambda *a,**kw:None)
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._approval.request_write_approval',lambda *a,**kw:pytest.fail('unattended prompt'))
    stopped,row,results=gate(s,attended=False)
    assert stopped and 'tool_effect_takeover_required' in results['new'][0]
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._approval.request_write_approval',lambda *a,**kw:False)
    stopped,row,results=gate(s)
    assert stopped and not recovery_receipt(s.task,'write_file','new',args(),check(s))


def test_evidence_change_after_human_approval_refuses_at_common_entry(scenario,monkeypatch):
    s=scenario;s.observed.update(outcome='unknown',reason='missing_evidence');inspect_receipt(s)
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._approval.request_write_approval',lambda *a,**kw:True)
    assert not gate(s)[0]
    s.observed.update(reason='evidence_conflict')
    s.task['_tool_env']=SimpleNamespace(resolve=lambda name:pytest.fail('must not resolve handler'))
    row=write_row()
    result=_execute_tool_one(s.task,{},'write_file','new',args(),3,row,{},str(s.root),True)
    assert json.loads(result[1])['code']=='tool_effect_inspection_changed'
    assert 'executionEffect' not in row


def test_native_default_auto_requires_one_human_takeover_and_consumes_exactly_once(scenario,monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from tests.tasks_pkg.test_native_gateway import principal,catalog
    from lib.tasks_pkg.native_gateway import NativeGateway
    from lib.agent_core.native_process import ApprovalRequest,ToolRequest
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    s=scenario;s.observed.update(outcome='unknown',reason='missing_evidence');inspect_receipt(s)
    definitions=catalog();s.task['_executable_tool_catalog']=definitions;prompts=[];writes=[]
    def confirm(approval_id,**kw):prompts.append(approval_id);kw['on_registered']();return True
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._approval.request_write_approval',confirm)
    def handler(task,tc,name,call_id,arguments,rn,row,*rest):
        writes.append(call_id);(s.root/'file.txt').write_text(arguments['content'])
        _finalize_tool_round(task,rn,row,[{'title':'write','content':'written','badge':'done'}])
        return call_id,'written',False
    s.task['_tool_env']=SimpleNamespace(resolve=lambda name:handler,write_names={'write_file'},
        idempotent_names=set(),confirmation_names=set())
    async def run(executor):
        gateway=NativeGateway(principal=principal(),task=s.task,executor=executor,configuration={'autoApply':True},
            tool_list=definitions,permissions={'write_file':'write'},project_path=str(s.root),project_enabled=True,
            model='fixture',round_source=lambda:2,decision_policy=None,pending_calls=2,pending_bytes=16384)
        try:
            arguments={'path':'file.txt','content':'new decision'}
            approval=ApprovalRequest(principal(),s.task['id'],'approval','new','write_file','epoch',
                json.dumps(arguments).encode(),b'null',asyncio.Event(),2)
            assert await gateway.decide(approval)=='human'
            request=ToolRequest(principal(),s.task['id'],'execute','write_file',arguments,'new',model_round=2)
            assert await gateway.execute(request)=='written'
            with pytest.raises(ValueError):await gateway.execute(request)
        finally:gateway.close()
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    assert len(prompts)==1 and len(writes)==1 and (s.root/'file.txt').read_text()=='new decision'


@pytest.mark.parametrize('status',['error','aborted','rejected'])
def test_captured_unsuccessful_return_still_requires_file_publication_evidence(scenario,status):
    s=scenario;s.source['status']=status;s.source['executionEffect']['state']='returned'
    result=check(s)
    assert result.refusal and 'tool_effect_inspection_required' in result.refusal
    assert not s.calls


def test_foreign_reference_and_expired_takeover_never_grant_a_write(scenario,monkeypatch):
    s=scenario;s.source['executionEffect']['inspection']['ownerScope']='f'*24
    assert 'tool_effect_evidence_invalid' in check(s).refusal and not s.calls
    s.source['executionEffect']['inspection']['ownerScope']=turn_image_owner_scope(1,'conv')
    s.observed.update(outcome='unknown',reason='missing_evidence');inspect_receipt(s)
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._approval.request_write_approval',lambda *a,**kw:True)
    assert not gate(s)[0]
    decision=check(s)
    assert not recovery_receipt(s.task,'write_file','new',{'path':'file.txt','content':'changed'},decision)
    s.task['_tool_approval_receipts']['new']['minted_at']-=1000
    assert not recovery_receipt(s.task,'write_file','new',args(),decision)
