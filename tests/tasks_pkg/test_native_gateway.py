"""Native callbacks reuse actual parsing/pipeline and exact application receipts."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace

import pytest

from lib.agent_core.native_process import ApprovalRequest, ToolRequest
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_gateway import NativeGateway, compile_native_tool_catalog
from lib.tasks_pkg.tool_dispatch._approval import (
    approval_receipt_authority, consume_approval_receipt, mint_approval_receipt,
)

pytestmark = pytest.mark.unit


def test_response_declaration_defers_rows_skips_native_denials_and_consumes_once():
    from lib.agent_core.native_process import RoundRequest,RoundSettlement
    owner=task()
    definitions=[{'type':'function','function':{'name':'read_files','description':'Read',
        'parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}}]
    with ThreadPoolExecutor(max_workers=1) as executor:
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,configuration={},
            tool_list=definitions,permissions={'read_files':'read'},project_path=None,project_enabled=False,
            model='fixture',round_source=lambda:0,decision_policy=None,pending_calls=8,pending_bytes=8192)
        request=RoundRequest(principal(),'native-gateway','round',0,0)
        def call(name,args,identity):
            return {'id':identity,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}
        calls=[call('read_files',{'path':'fixture.txt'},'valid'),call('read_files',{'path':3},'invalid'),call('foreign',{},'foreign')]
        gateway.prepare_response(RoundSettlement(request,{'kind':'returned','model_calls':calls}))
        assert not owner['toolRounds'] and 0<gateway.response_retained_bytes<=8192
        occurrence=ToolRequest(principal(),owner['id'],'callback','read_files',{'path':'fixture.txt'},model_round=0)
        parsed,assistant=gateway._parse_occurrence(occurrence,0)
        assert parsed[0][2]=='valid' and assistant['tool_calls'][0]['id']=='valid'
        assert len(owner['toolRounds'])==1 and gateway.response_retained_bytes==0
        with pytest.raises(ValueError):gateway._parse_occurrence(occurrence,0)
        gateway.close()
        assert not gateway._response_prepared


def test_response_declaration_stop_and_pressure_never_announce_unexecuted_calls():
    from lib.agent_core.native_process import RoundRequest,RoundSettlement
    owner=task()
    with ThreadPoolExecutor(max_workers=1) as executor:
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,configuration={},
            tool_list=catalog(),permissions={'write_file':'write'},project_path=None,project_enabled=True,
            model='fixture',round_source=lambda:0,decision_policy=None,pending_calls=8,pending_bytes=1)
        request=RoundRequest(principal(),owner['id'],'round',0,0)
        result=RoundSettlement(request,{'kind':'returned','model_calls':[{'id':'write','type':'function',
            'function':{'name':'write_file','arguments':json.dumps({'path':'file.txt','content':'data'})}}]})
        with pytest.raises(ValueError,match='pending-byte'):gateway.prepare_response(result)
        assert not owner['toolRounds'] and not gateway._response_prepared
        owner['abort_event'].set()
        gateway.prepare_response(result)
        assert not owner['toolRounds']
        gateway.close()


def principal():
    return PrincipalContext.user(subject_id='native-gateway-test',owner_user_id=1)


def task():
    return dict(id='native-gateway',convId='',_userId=1,status='running',aborted=False,
        model='fixture',config={'tools':{'resultEnvelope':'legacy'}},events=[],
        events_lock=threading.Lock(),abort_event=threading.Event(),_attended=True,
        _dispatch_heartbeat=0.0,_t_last_event=0.0,toolRounds=[],created_at=time.time())


def catalog():
    return [{'type':'function','function':{'name':'write_file','description':'Write temporary file',
        'parameters':{'type':'object','properties':{'path':{'type':'string'},'content':{'type':'string'}},
                      'required':['path','content']}}}]


def test_gateway_passes_one_borrowed_search_accumulator_to_canonical_pipeline(monkeypatch):
    owner=task();evidence=[]
    definitions=[{'type':'function','function':{'name':'web_search','parameters':{'type':'object'}}}]
    def pipeline(*args,**kwargs):
        messages,search=args[6],args[7]
        assert search is evidence
        from lib.tasks_pkg.tool_dispatch._pipeline import _append_search_result_text
        _append_search_result_text(search,'URL: https://example.test/retrieved')
        messages.append({'role':'tool','tool_call_id':messages[0]['tool_calls'][0]['id'],
                         'content':'URL: https://example.test/retrieved'})
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch.api.execute_tool_pipeline',pipeline)
    with ThreadPoolExecutor(max_workers=1) as executor:
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,permissions={'web_search':'read'},
            configuration=owner['config'],tool_list=definitions,project_path='',project_enabled=False,
            model='fixture',round_source=lambda:0,decision_policy=None,pending_calls=8,pending_bytes=4096,
            search_results=evidence)
        try:
            request=ToolRequest(principal(),owner['id'],'request','web_search',{},model_round=0)
            assert gateway._execute(request,None)=='URL: https://example.test/retrieved'
            assert evidence==['URL: https://example.test/retrieved']
        finally:gateway.close()


def test_receipt_inspection_is_exact_live_and_nonconsuming():
    owner={};args={'path':'file','content':'original'}
    assert mint_approval_receipt(owner,'write_file','call',args,authority='human')
    assert approval_receipt_authority(owner,'write_file','call',args)=='human'
    assert approval_receipt_authority(owner,'write_file','call',dict(args,content='changed')) is None
    assert consume_approval_receipt(owner,'write_file','call',args)
    assert approval_receipt_authority(owner,'write_file','call',args) is None

    assert mint_approval_receipt(owner,'write_file','call',args,authority='human')
    owner['_tool_approval_receipts']['call']['minted_at']-=1000
    assert approval_receipt_authority(owner,'write_file','call',args) is None
    owner['_tool_approval_receipts']['call']['minted_at']=float('nan')
    assert approval_receipt_authority(owner,'write_file','call',args) is None
    owner['_tool_approval_receipts']['call']['minted_at']=time.time()
    owner['_tool_approval_receipts']['call']['authority']='invented'
    assert approval_receipt_authority(owner,'write_file','call',args) is None


def test_compiled_catalog_uses_task_safety_and_detaches_schema():
    owner=task()
    owner['_tool_env']=SimpleNamespace(write_names={'write_file'},idempotent_names=set(),confirmation_names={'write_file'})
    definitions=catalog()
    compiled=compile_native_tool_catalog(owner,definitions)
    assert compiled['write_file']['permission']=='approval'
    from lib.agent_core.native_content_contract import native_model_tools
    assert native_model_tools(compiled)==definitions
    compiled['write_file']['arguments_schema']['required'].append('new')
    assert definitions[0]['function']['parameters']['required']==['path','content']


def test_catalog_refuses_duplicates_unrepresented_controls_and_foreign_permissions():
    import copy
    definitions=catalog()
    strict=copy.deepcopy(definitions);strict[0]['function']['strict']=True
    for candidate,permissions in [(definitions*2,None),(strict,None),(definitions,{'foreign':'read'})]:
        with pytest.raises(ValueError):
            compile_native_tool_catalog(task(),candidate,permissions=permissions)


def test_preapproved_identical_call_does_not_repeat_human_gate(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _approval
    owner=task();args={'path':'file','content':'original'};row={'toolCallId':'call'}
    assert mint_approval_receipt(owner,'write_file','call',args,authority='human')
    def unexpected(*_args,**_kwargs):raise AssertionError('duplicate human prompt')
    monkeypatch.setattr(_approval,'request_write_approval',unexpected)
    assert _approval._handle_approval(owner,'write_file',args,0,row,None,0,'fixture')==(True,None)
    assert consume_approval_receipt(owner,'write_file','call',args)


@pytest.mark.parametrize('auto,approved', [(True,True),(False,True),(False,False)])
def test_default_application_policy_uses_one_prepared_occurrence(monkeypatch,tmp_path,auto,approved):
    from lib.tasks_pkg.tool_dispatch import _approval
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    from contextvars import ContextVar
    marker = ContextVar('gateway-context-fixture', default='unbound')
    owner=task();definitions=catalog();owner['_executable_tool_catalog']=definitions
    target=tmp_path/'default-native-write.txt';calls=[];prompts=[]
    def confirm(approval_id, **kwargs):
        assert marker.get() == 'request'
        prompts.append(approval_id)
        assert kwargs['owner_user_id']==1
        kwargs['on_registered']()
        return approved
    monkeypatch.setattr(_approval,'request_write_approval',confirm)
    def handler(task,tc,name,call_id,args,rn,row,*rest):
        assert marker.get() == 'request'
        calls.append(call_id)
        assert approval_receipt_authority(task,name,call_id,args)==('auto_write_mode' if auto else 'human')
        target.write_text(args['content'])
        consume_approval_receipt(task,name,call_id,args)
        _finalize_tool_round(task,rn,row,[{'title':'write','content':'written','badge':'done'}])
        return call_id,'written',False
    owner['_tool_env']=SimpleNamespace(resolve=lambda name:handler if name=='write_file' else None,
        write_names={'write_file'},idempotent_names=set(),confirmation_names=set())
    async def run(executor):
        marker.set('request')
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={'autoApply':auto},tool_list=definitions,permissions={'write_file':'write'},
            project_path=str(tmp_path),project_enabled=True,model='fixture',round_source=lambda:0,
            decision_policy=None,pending_calls=2,pending_bytes=16384)
        args={'path':str(target),'content':'canonical default'}
        request=ApprovalRequest(principal(),owner['id'],'approval','provider-call','write_file','epoch',
            json.dumps(args).encode(),b'null',asyncio.Event(),2)
        decision=await gateway.decide(request)
        assert decision==('auto_write_mode' if auto else 'human' if approved else 'denied')
        if auto or approved:
            assert await gateway.execute(ToolRequest(principal(),owner['id'],'execute','write_file',args,'provider-call',model_round=0))=='written'
            with pytest.raises(ValueError):
                await gateway.execute(ToolRequest(principal(),owner['id'],'duplicate','write_file',args,'provider-call',model_round=0))
        assert gateway._bytes==0 and not gateway._pending
        gateway.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))
        assert executor.submit(marker.get).result() == 'unbound'
    assert marker.get() == 'unbound'
    assert len(owner['toolRounds'])==1
    assert len(prompts)==(0 if auto else 1)
    assert len(calls)==int(auto or approved)
    assert target.exists()==(auto or approved)


def test_default_approval_budget_fault_settles_occurrence_before_effect(monkeypatch,tmp_path):
    owner=task();owner['_executable_tool_catalog']=catalog()
    async def run(executor):
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={'autoApply':True},tool_list=catalog(),permissions={'write_file':'write'},
            project_path=str(tmp_path),project_enabled=True,model='fixture',round_source=lambda:0,
            decision_policy=None,pending_calls=1,pending_bytes=512)
        request=ApprovalRequest(principal(),owner['id'],'approval','call','write_file','epoch',
            b'{"path":"unwritten","content":"data"}',b'null',asyncio.Event(),1)
        with pytest.raises(ValueError,match='prepared approval budget'):
            await gateway.decide(request)
        assert gateway._bytes==0 and not gateway._pending
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    assert len(owner['toolRounds'])==1 and owner['toolRounds'][0]['status']=='error'
    assert not owner.get('_tool_approval_receipts')


def test_default_manual_cancellation_joins_actual_gate_and_withdraws_receipt(monkeypatch,tmp_path):
    from lib.tasks_pkg.tool_dispatch import _approval
    from lib.tasks_pkg.approval import request_write_approval,resolve_write_approval
    owner=task();owner['_executable_tool_catalog']=catalog();entered=threading.Event();published=[]
    def confirm(approval_id, *, timeout, on_registered, **kwargs):
        published.append(approval_id)
        def registered():
            on_registered();entered.set()
        return request_write_approval(approval_id,timeout=3,on_registered=registered,**kwargs)
    monkeypatch.setattr(_approval,'request_write_approval',confirm)
    async def run(executor):
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={'autoApply':False},tool_list=catalog(),permissions={'write_file':'write'},
            project_path=str(tmp_path),project_enabled=True,model='fixture',round_source=lambda:0,
            decision_policy=None,pending_calls=1,pending_bytes=16384)
        stopped=asyncio.Event()
        request=ApprovalRequest(principal(),owner['id'],'approval','call','write_file','epoch',
            b'{"path":"unwritten","content":"data"}',b'null',stopped,3)
        work=asyncio.create_task(gateway.decide(request))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            with pytest.raises(ValueError,match='have not joined'):
                gateway.finish_approvals()
            stopped.set()
            assert await asyncio.wait_for(work,4)=='denied'
        finally:
            stopped.set()
            if not work.done():
                owner['abort_event'].set()
                await work
        assert gateway._bytes==0 and not gateway._pending
        assert owner['aborted'] and owner['abort_event'].is_set()
        assert not resolve_write_approval(published[0],True,owner_user_id=1)
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    assert owner['toolRounds'][0]['status']=='aborted'
    assert not owner.get('_tool_approval_receipts')


def test_unused_default_grant_is_withdrawn_before_terminal_projection(tmp_path):
    owner=task();owner['_executable_tool_catalog']=catalog()
    async def run(executor):
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={'autoApply':True},tool_list=catalog(),permissions={'write_file':'write'},
            project_path=str(tmp_path),project_enabled=True,model='fixture',round_source=lambda:0,
            decision_policy=None,pending_calls=1,pending_bytes=16384)
        request=ApprovalRequest(principal(),owner['id'],'approval','call','write_file','epoch',
            b'{"path":"unwritten","content":"data"}',b'null',asyncio.Event(),2)
        assert await gateway.decide(request)=='auto_write_mode'
        gateway.finish_approvals()
        count=len(owner['events'])
        gateway.finish_approvals()
        assert len(owner['events'])==count
        assert gateway._bytes==0 and not gateway._pending
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    assert owner['toolRounds'][0]['status']=='aborted'
    assert not owner.get('_tool_approval_receipts')


def test_actual_pipeline_executes_one_admitted_handler_and_keeps_tool_round(monkeypatch,tmp_path):
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    owner=task();definitions=catalog();owner['_executable_tool_catalog']=definitions
    calls=[];target=tmp_path/'native-write.txt'
    def handler(task,tc,name,call_id,args,rn,row,*_rest):
        calls.append((name,call_id))
        assert approval_receipt_authority(task,name,call_id,args)=='human'
        target.write_text(args['content'])
        _finalize_tool_round(task,rn,row,[{'title':'temporary write','content':'written','badge':'done'}])
        return call_id,'written',False
    owner['_tool_env']=SimpleNamespace(resolve=lambda name:handler if name=='write_file' else None)
    class Policy:
        async def decide(self,_request):return 'human'
    async def run(executor):
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={'autoApply':False},tool_list=definitions,permissions={'write_file':'write'},
            project_path=None,project_enabled=False,model='fixture',round_source=lambda:0,
            decision_policy=Policy(),pending_calls=2,pending_bytes=4096)
        from lib.agent_core.native_process import ProgressLimits, ProgressRequest
        from lib.agent_core.progress_evidence import counted_result_evidence, tool_result_evidence
        progress=gateway.enable_progress(ProgressLimits(2,2),4096)
        args={'path':str(target),'content':'actual pipeline mutation'}
        approval=ApprovalRequest(principal(),owner['id'],'approval','provider-call','write_file','epoch',
            json.dumps(args).encode(),b'null',asyncio.Event(),1)
        assert await gateway.decide(approval)=='human'
        request=ToolRequest(principal(),owner['id'],'execute','write_file',args,'provider-call')
        assert await gateway.execute(request)=='written'
        observed=await progress.observe(ProgressRequest(principal(),owner['id'],0,1,threading.Event()))
        expected = counted_result_evidence({tool_result_evidence(
            {'function': {'name': 'write_file', 'arguments': args}}, 'written'): 1})
        assert observed['resultEvidenceComplete'] and observed['evidenceIds']==list(expected)
        assert not observed['nonretryableFailureSignatures'] and not observed['verification']
        with pytest.raises(ValueError):await gateway.execute(request)
        gateway.close()
        assert progress.gateway is None and progress.batch is None
        with pytest.raises(ValueError):await progress.observe(ProgressRequest(principal(),owner['id'],0,1,threading.Event()))
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    assert target.read_text()=='actual pipeline mutation'
    assert len(calls)==1 and len(owner['toolRounds'])==1
    assert owner['toolRounds'][0]['status']=='done'


def test_actual_read_batch_progress_tracks_target_results_not_callback_order():
    from lib.agent_core.native_process import ProgressLimits, ProgressRequest, RoundRequest, RoundSettlement
    from lib.agent_core.progress_ledger import ProgressLedgerV2
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    owner = task()
    definitions = [{'type': 'function', 'function': {'name': 'read_files',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}},
                       'required': ['path']}}}]
    owner['_executable_tool_catalog'] = definitions
    completed = []
    def handler(task, tc, name, call_id, args, rn, row, *_rest):
        round_num = int(call_id.split('-')[0])
        result = 'old' if (args['path'] == 'a') == (round_num < 2) else 'new'
        _finalize_tool_round(task, rn, row, [{'title': 'read', 'content': result, 'badge': 'done'}])
        # This synthetic source changes outside the project and has no reusable
        # file revision. Exercise fresh settled reads through the normal port.
        row['_cacheableResult'] = False
        completed.append(call_id)
        return call_id, result, False
    owner['_tool_env'] = SimpleNamespace(resolve=lambda name: handler if name == 'read_files' else None)
    async def run(executor):
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration=owner['config'], tool_list=definitions, permissions={'read_files': 'read'},
            project_path=None, project_enabled=False, model='fixture', round_source=None,
            decision_policy=None, pending_calls=8, pending_bytes=8192)
        progress = gateway.enable_progress(ProgressLimits(2, 2), 4096)
        ledger = ProgressLedgerV2()
        try:
            for rnd in range(3):
                calls = [{'id': f'{rnd}-{path}', 'type': 'function', 'function': {
                    'name': 'read_files', 'arguments': json.dumps({'path': path})}} for path in ['a', 'b']]
                gateway.prepare_response(RoundSettlement(
                    RoundRequest(principal(), owner['id'], f'round-{rnd}', rnd, 0),
                    {'kind': 'returned', 'model_calls': calls}))
                order = ['a', 'b'] if rnd == 0 else ['b', 'a']
                results = await asyncio.gather(*(gateway.execute(ToolRequest(principal(), owner['id'],
                    f'{rnd}-{path}', 'read_files', {'path': path}, model_round=rnd)) for path in order))
                assert results == ['old' if (path == 'a') == (rnd < 2) else 'new' for path in order]
                observation = await progress.observe(ProgressRequest(
                    principal(), owner['id'], rnd, 2, threading.Event()))
                assert observation['resultEvidenceComplete']
                decision = ledger.observe(calls, evidence_ids=observation['evidenceIds'],
                    evidence_complete=observation['resultEvidenceComplete'])
                assert decision['noProgressStreak'] == (1 if rnd == 1 else 0)
        finally:
            gateway.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        asyncio.run(run(executor))
    assert len(completed) == len(set(completed)) == len(owner['toolRounds']) == 6
    assert all(row['status'] == 'done' for row in owner['toolRounds'])


def test_registered_project_writer_runs_through_the_same_pipeline(tmp_path):
    from lib.tasks_pkg.handlers import project  # noqa: F401 — register retained built-in handler
    owner=task();definitions=catalog();owner['_executable_tool_catalog']=definitions
    target=tmp_path/'registered-write.txt'
    class Policy:
        async def decide(self,_request):return 'human'
    async def run(executor):
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={'autoApply':False},tool_list=definitions,permissions={'write_file':'write'},
            project_path=str(tmp_path),project_enabled=True,model='fixture',round_source=lambda:0,
            decision_policy=Policy(),pending_calls=2,pending_bytes=4096)
        args={'path':str(target),'content':'registered handler mutation'}
        approval=ApprovalRequest(principal(),owner['id'],'approval','provider-call','write_file','epoch',
            json.dumps(args).encode(),b'null',asyncio.Event(),1)
        assert await gateway.decide(approval)=='human'
        request=ToolRequest(principal(),owner['id'],'execute','write_file',args,'provider-call')
        result=await gateway.execute(request)
        assert 'File created:' in result, result
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    assert target.read_text()=='registered handler mutation'
    assert len(owner['toolRounds'])==1 and owner['toolRounds'][0]['status']=='done'


def test_pre_execution_hook_still_blocks_an_approved_native_call(monkeypatch,tmp_path):
    from lib.tasks_pkg.tool_dispatch import _pipeline
    owner=task();definitions=catalog();owner['_executable_tool_catalog']=definitions
    target=tmp_path/'must-not-exist.txt'
    monkeypatch.setattr(_pipeline,'run_pre_hooks',lambda *_args:SimpleNamespace(
        action='block',message='current owner policy refused',additional_context=''))
    owner['_tool_env']=SimpleNamespace(resolve=lambda _name:lambda *_args:target.write_text('bad'))
    class Policy:
        async def decide(self,_request):return 'human'
    async def run(executor):
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={'autoApply':False},tool_list=definitions,permissions={'write_file':'write'},
            project_path=None,project_enabled=False,model='fixture',round_source=lambda:0,
            decision_policy=Policy(),pending_calls=2,pending_bytes=4096)
        args={'path':str(target),'content':'bad'}
        approved=ApprovalRequest(principal(),owner['id'],'approval','call','write_file','epoch',
            json.dumps(args).encode(),b'null',asyncio.Event(),1)
        await gateway.decide(approved)
        result=await gateway.execute(ToolRequest(principal(),owner['id'],'execute','write_file',args,'call'))
        assert 'blocked by pre-execution hook' in result
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    assert not target.exists()


def test_parent_stop_reaches_existing_tool_context_and_waits_for_handler(monkeypatch,tmp_path):
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    owner=task();definitions=catalog();owner['_executable_tool_catalog']=definitions
    entered,release=threading.Event(),threading.Event()
    def handler(task,tc,name,call_id,args,rn,row,*_rest):
        entered.set();release.wait(5)
        assert task['aborted'] and task['abort_event'].is_set()
        _finalize_tool_round(task,rn,row,[{'title':'stopped','content':'cancelled'}],status='aborted')
        return call_id,'cancelled',False
    owner['_tool_env']=SimpleNamespace(resolve=lambda _name:handler)
    class Policy:
        async def decide(self,_request):return 'human'
    async def run(executor):
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={'autoApply':False},tool_list=definitions,permissions={'write_file':'write'},
            project_path=None,project_enabled=False,model='fixture',round_source=lambda:0,
            decision_policy=Policy(),pending_calls=2,pending_bytes=4096)
        args={'path':str(tmp_path/'cancelled'),'content':'not written'}
        approval=ApprovalRequest(principal(),owner['id'],'approval','call','write_file','epoch',
            json.dumps(args).encode(),b'null',asyncio.Event(),1)
        await gateway.decide(approval);stop=asyncio.Event()
        request=ToolRequest(principal(),owner['id'],'execute','write_file',args,'call',None,stop)
        work=asyncio.create_task(gateway.execute(request))
        while not entered.is_set():await asyncio.sleep(0.001)
        stop.set();await asyncio.sleep(0.01)
        assert not work.done() and owner['aborted']
        release.set();await work
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    assert owner['toolRounds'][0]['status']=='aborted'


def test_pending_policy_reserves_budget_and_close_rejects_late_grant():
    owner=task()
    async def run(executor):
        entered,release=asyncio.Event(),asyncio.Event()
        class Policy:
            async def decide(self,_request):
                entered.set()
                await release.wait()
                return 'human'
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={},tool_list=catalog(),permissions={'write_file':'write'},
            project_path=None,project_enabled=False,model='fixture',round_source=lambda:0,
            decision_policy=Policy(),pending_calls=1,pending_bytes=4096)
        def request(identity):
            return ApprovalRequest(principal(),owner['id'],identity,identity,'write_file','epoch',
                b'{"path":"file","content":"data"}',b'null',asyncio.Event(),1)
        decision=asyncio.create_task(gateway.decide(request('first')))
        await entered.wait()
        with pytest.raises(ValueError,match='budget exhausted'):
            await gateway.decide(request('second'))
        assert len(gateway._pending)==1 and gateway._bytes>0
        gateway.close()
        release.set()
        with pytest.raises(ValueError,match='ownership changed'):
            await decision
        assert gateway._pending=={} and gateway._bytes==0
        with pytest.raises(ValueError,match='ownership changed'):
            await gateway.decide(request('third'))
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


def test_native_round_overrides_legacy_source_and_invalid_round_has_no_effect(tmp_path):
    from lib.tasks_pkg.handlers import project  # noqa: F401
    owner=task();definitions=catalog();owner['_executable_tool_catalog']=definitions
    target=tmp_path/'native-round.txt'
    class Policy:
        async def decide(self,_request):return 'human'
    async def run(executor):
        gateway=NativeGateway(principal=principal(),task=owner,executor=executor,
            configuration={'autoApply':False},tool_list=definitions,permissions={'write_file':'write'},
            project_path=str(tmp_path),project_enabled=True,model='fixture',round_source=lambda:99,
            decision_policy=Policy(),pending_calls=2,pending_bytes=4096)
        for identity,round_number in [('invalid',True),('actual',3)]:
            args={'path':str(target),'content':'native round'}
            approval=ApprovalRequest(principal(),owner['id'],identity,identity,'write_file','epoch',
                json.dumps(args).encode(),b'null',asyncio.Event(),1)
            assert await gateway.decide(approval)=='human'
            request=ToolRequest(principal(),owner['id'],identity,'write_file',args,identity,
                                model_round=round_number)
            if identity=='invalid':
                with pytest.raises(ValueError,match='factual model round'):
                    await gateway.execute(request)
                assert not target.exists() and owner['toolRounds']==[]
            else:
                assert 'File created:' in await gateway.execute(request)
                assert owner['toolRounds'][0]['llmRound']==3
        gateway.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


def test_declared_model_content_uses_packaged_schema_and_rejects_extra_fields(monkeypatch):
    from pathlib import Path
    from lib.agent_core.native_process import validate_model_content, _model_content_validators
    _model_content_validators.cache_clear()
    read_text=Path.read_text
    def read(path,*args,**kwargs):
        if str(path).endswith('contracts/native_agent_stdio_v1.schema.json'):
            raise AssertionError('runtime repository schema read')
        return read_text(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    value=[{'type':'text','text':'before'},{'type':'image_url','image_url':{'url':'data:image/png;base64,iVBORw==','detail':'high'}},{'type':'text','text':'after'}]
    assert validate_model_content(value) is value
    for content in [[],[None],[{'type':'text','text':7}],[{'type':'image_url','image_url':{'url':''}}],
                    [{'type':'text','text':'ok','extra':True}],
                    [{'type':'image_url','image_url':{'url':'uri','detail':'original'}}]]:
        with pytest.raises(TypeError):validate_model_content(content)


def test_native_catalog_projects_inputs_and_retains_host_result_metadata():
    import copy
    definitions=catalog()
    definitions[0]['decoded_result_schema']={'type':'object','properties':{'written':{'type':'boolean'}}}
    before=copy.deepcopy(definitions)
    compiled=compile_native_tool_catalog(task(),definitions)
    assert compiled['write_file']['arguments_schema']==definitions[0]['function']['parameters']
    assert 'decoded_result_schema' not in compiled['write_file']
    from lib.tools.gateway import sanitize_wire_tools
    assert compiled['write_file']['description']==sanitize_wire_tools(definitions)[0]['function']['description']
    assert 'coverage/freshness unknown' in compiled['write_file']['description']
    assert definitions==before
    for value in (None,[],{'type':float('nan')}):
        invalid=copy.deepcopy(definitions)
        invalid[0]['decoded_result_schema']=value
        with pytest.raises(ValueError):compile_native_tool_catalog(task(),invalid)
