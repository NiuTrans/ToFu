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
from lib.tasks_pkg.native_gateway import NativeGateway
from lib.tasks_pkg.tool_dispatch._approval import (
    approval_receipt_authority, consume_approval_receipt, mint_approval_receipt,
)

pytestmark = pytest.mark.unit


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


def test_preapproved_identical_call_does_not_repeat_human_gate(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _approval
    owner=task();args={'path':'file','content':'original'};row={'toolCallId':'call'}
    assert mint_approval_receipt(owner,'write_file','call',args,authority='human')
    def unexpected(*_args,**_kwargs):raise AssertionError('duplicate human prompt')
    monkeypatch.setattr(_approval,'request_write_approval',unexpected)
    assert _approval._handle_approval(owner,'write_file',args,0,row,None,0,'fixture')==(True,None)
    assert consume_approval_receipt(owner,'write_file','call',args)


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
        from lib.agent_core.progress_evidence import visible_result_evidence
        progress=gateway.enable_progress(ProgressLimits(2,2),4096)
        args={'path':str(target),'content':'actual pipeline mutation'}
        approval=ApprovalRequest(principal(),owner['id'],'approval','provider-call','write_file','epoch',
            json.dumps(args).encode(),b'null',asyncio.Event(),1)
        assert await gateway.decide(approval)=='human'
        request=ToolRequest(principal(),owner['id'],'execute','write_file',args,'provider-call')
        assert await gateway.execute(request)=='written'
        observed=await progress.observe(ProgressRequest(principal(),owner['id'],0,1,threading.Event()))
        assert observed['resultEvidenceComplete'] and observed['evidenceIds']==[visible_result_evidence('written')]
        assert not observed['nonretryableFailureSignatures'] and not observed['verification']
        with pytest.raises(ValueError):await gateway.execute(request)
        gateway.close()
        assert progress.gateway is None and progress.batch is None
        with pytest.raises(ValueError):await progress.observe(ProgressRequest(principal(),owner['id'],0,1,threading.Event()))
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    assert target.read_text()=='actual pipeline mutation'
    assert len(calls)==1 and len(owner['toolRounds'])==1
    assert owner['toolRounds'][0]['status']=='done'


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
