"""Real native retry reference after committed writer confirmation is withheld.

The canonical writer and authority perform actual commits. Only its first successful
return is hidden from the terminal port to model lost writer confirmation; native
transport retries still require an explicit host retry ACK, never guessed replay.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import threading
import time
import uuid

import pytest
from lib.agent_core.native_process import Budget, NativeProcessClient, _TerminalControl
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy, project_native_terminal
from tests.storage import test_storage_application_contract as application_contract

pytestmark=[pytest.mark.api,pytest.mark.skipif(not os.environ.get("TOFU_NATIVE_USAGE_TEST_BINARY"),
    reason="explicit locally built native binary required")]
application_storage=application_contract.application_storage
application_storage_owner_id=application_contract.application_storage_owner_id


@pytest.mark.parametrize('references',[False,True])
def test_committed_terminal_lost_writer_confirmation_retries_frozen_candidate(
    application_storage,monkeypatch,tmp_path,references,
):
    configured=os.environ.get('TOFU_NATIVE_USAGE_TEST_BINARY')
    if not configured:pytest.skip('explicit source-matched native binary required')
    binary=Path(configured).resolve(strict=True)
    from lib.turn_lifecycle import create_turn_pair,bind_task,record_task_event,get_turn,get_attempt
    from lib.tasks_pkg import native_terminal
    conv_id,task_id=uuid.uuid4().hex,uuid.uuid4().hex
    pair=create_turn_pair(conv_id,command_id='create-'+conv_id,input_projection={'content':'question'},
        config={'model':'fixture-model'},user_id=1,
        conversation_defaults={'allowCreate':True,'title':'Retry contract','settings':{}})
    attempt_id=pair['attempt']['attemptId'];turn_id=pair['turn']['turnId']
    bind_task(attempt_id,task_id,user_id=1)
    task=dict(id=task_id,convId=conv_id,_attemptId=attempt_id,_turnId=turn_id,_userId=1,
        status='running',content='',thinking='',model='fixture-model',config={},
        toolRounds=[],segments=[],events=[],events_lock=threading.Lock(),
        created_at=time.time(),abort_event=threading.Event(),content_lock=threading.Lock())
    assert record_task_event(task,{'type':'phase','phase':'text'})
    projections=[];commit_returns=[];committed_snapshots=[];requests=[];frames=[];model_calls=[];tool_calls=[]
    def snapshot():
        turn=get_turn(conv_id,turn_id,user_id=1)
        attempt=get_attempt(attempt_id,user_id=1)
        result=application_storage.query('task_results.replay_get',{'key':task_id,'user_id':1})
        assert turn['status']==attempt['status']=='completed'
        assert turn['projection']['content']=='durable answer' and result['status']=='done'
        return result
    def project(current,request):
        projections.append(request)
        project_native_terminal(current,request)
        event={'type':'done','finishReason':'stop'}
        assert record_task_event(current,event,task_event={'task_id':task_id,'sequence':1,'event':event})=='carried'
    canonical=native_terminal._persist
    def hide_first_success(current):
        accepted=canonical(current)
        assert accepted is True
        commit_returns.append(accepted);committed_snapshots.append(snapshot())
        return False if len(commit_returns)==1 else True
    monkeypatch.setattr(native_terminal,'_persist',hide_first_success)
    original_handle=_TerminalControl.handle
    async def observe_handle(control,frame,process):
        frames.append(frame['type'])
        return await original_handle(control,frame,process)
    monkeypatch.setattr(_TerminalControl,'handle',observe_handle)
    original_policy=NativeTaskTerminalPolicy.persist
    async def observe_policy(policy,request):
        requests.append(request)
        return await original_policy(policy,request)
    monkeypatch.setattr(NativeTaskTerminalPolicy,'persist',observe_policy)
    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            model_calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            first=len(model_calls)==1
            delta={'tool_calls':[{'index':0,'id':'lookup-call','type':'function',
                'function':{'name':'lookup','arguments':'{}'}}]} if first else {'content':'durable answer'}
            value={'choices':[{'delta':delta,'finish_reason':'tool_calls' if first else 'stop'}],
                'usage':{'prompt_tokens':3,'completion_tokens':2}}
            raw=('data: '+json.dumps(value)+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def log_message(self,*_args):pass
    server=HTTPServer(('127.0.0.1',0),Provider);worker=threading.Thread(target=server.serve_forever);worker.start()
    async def run():
        owner=PrincipalContext.user(subject_id='durable-retry-contract',owner_user_id=1)
        client=NativeProcessClient(binary=binary,sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            protocol_version='tofu.native-agent-stdio/v1',principal=owner,
            budget=Budget(1,binary.stat().st_size,65536,65536,5))
        profile=dict(operation_id='commit-'+task_id,ack_ms=5000,attempts=2,frame_bytes=65536)
        if references:profile.update(retry_reference=True,completion_reference=True)
        config=dict(version=client.protocol_version,type='configure',owner_user_id=1,task_id=task_id,
            epoch='epoch',endpoint=f'http://127.0.0.1:{server.server_port}/chat',headers={},template={'model':'fixture'},
            messages=[{'role':'user','content':'question'}],tools={'lookup':{'name':'lookup','permission':'read',
                'contractVersion':'tofu.tool-contract/v2','arguments_schema':{'type':'object'}}},event_bytes=65536,output_bytes=65536,
            tool_calls=8,request_bytes=65536,rounds=3,retained_bytes=65536,feedback_bytes=4096,
            tool_frame_bytes=8192,deadline_ms=10000,terminal_persistence=profile,
            policy=dict(connections=1,idle_connections=1,keepalive_ms=1000,connect_ms=1000,
                request_headers_ms=5000,pool_wait_ms=1000,stream_idle_ms=1000,
                drain_timeout_ms=100,drain_bytes=65536,drain_chunks=16))
        async def lookup(request):
            tool_calls.append(request.name)
            return 'tool evidence'
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                policy=NativeTaskTerminalPolicy(principal=owner,task=task,operation_id=profile['operation_id'],
                    executor=executor,project=project,candidate_bytes=65536)
                result=await client.run(config,callbacks={"lookup":lookup},working_directory=tmp_path,terminal_policy=policy)
                assert result['status']=='done' and policy.accepted and client.active_runs==0
        finally:await client.close()
    try:asyncio.run(run())
    finally:server.shutdown();server.server_close();worker.join()
    assert len(model_calls)==2 and len(projections)==1
    assert tool_calls==["lookup"]
    assert len(commit_returns)==len(committed_snapshots)==len(requests)==2
    assert frames==['terminal_request','terminal_retry' if references else 'terminal_request']
    assert [r.attempt for r in requests]==[1,2]
    assert requests[0].event_json==requests[1].event_json
    assert requests[0].receipt_json==requests[1].receipt_json
    if references:
        assert requests[0].event_json is requests[1].event_json
        assert requests[0].receipt_json is requests[1].receipt_json
    assert not task.get('_terminalPersistencePending')
    assert task['usage']=={'prompt_tokens':6,'completion_tokens':4}
    final=snapshot()
    assert final['status']==committed_snapshots[0]['status']=='done'
    # The canonical writer may refresh checkpoint version/timestamps on retry.
    # This test protects business payload and event/effect uniqueness, not a
    # transport-independent promise that maintenance metadata never advances.
    bookkeeping={'version','updated_at_ms','completed_at'}
    stable=lambda value:{key:item for key,item in value.items() if key not in bookkeeping}
    assert stable(committed_snapshots[0])==stable(committed_snapshots[1])==stable(final)
    assert committed_snapshots[1]==final
    assert final['version']>=committed_snapshots[0]['version']
    events=application_storage.query('event.list',{'task_id':task_id,'user_id':1})
    assert len([row for row in events if row.get('event',{}).get('type')=='done'])==1
