"""Explicit transient native terminal delivery has no Turn/store authority."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest
from lib.agent_core.native_process import Budget, NativeProcessClient, NativeProcessError
from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.terminal_delivery import bind_terminal_delivery
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy, project_native_terminal
from tests.agent_core.test_native_task_terminal import principal, request


@pytest.mark.unit
@pytest.mark.parametrize('marker,declared,extra', [
    (True, False, {}), (False, True, {}), ('yes', False, {}), ('yes', True, {}),
    (True, True, {'_attemptId':'attempt'}), (True, True, {'_turnId':'turn'}),
    (True, True, {'_billingUserId':'wallet'}),
    (True, True, {'config':{'_resumeSource':{'taskId':'source'}}}),
])
def test_lifetime_mismatch_refuses_before_projection_or_delivery(marker, declared, extra):
    with ThreadPoolExecutor(max_workers=1) as executor:
        task=dict(id='task',_userId=7,status='running',_transientRuntime=marker,**extra)
        with pytest.raises(ValueError,match='declared lifetime'):
            NativeTaskTerminalPolicy(principal=principal(),task=task,operation_id='commit',
                executor=executor,project=lambda *_:pytest.fail('unexpected projection'),
                candidate_bytes=4096,transient=declared)


@pytest.mark.unit
def test_transient_delivery_retries_same_result_and_lifetime_change_is_fenced(monkeypatch):
    async def run():
        from lib.agent_core.terminal_delivery import TerminalDelivery
        monkeypatch.setattr('lib.storage.get_storage_client',lambda *_a,**_k:pytest.fail('transient accessed storage'))
        task=dict(id='task',_userId=7,status='running',_transientRuntime=True)
        seen=[];projections=[]
        def receive(value):
            seen.append((value['id'],value['content']))
            if len(seen)==1:raise OSError('lost ACK after receiver effect')
        bind_terminal_delivery(task,receive)
        def project(value,candidate):
            projections.append(candidate);value.update(status='done',content='answer',thinking='',finishReason='stop')
        with ThreadPoolExecutor(max_workers=1) as executor:
            policy=NativeTaskTerminalPolicy(principal=principal(),task=task,operation_id='commit',
                executor=executor,project=project,candidate_bytes=4096,transient=True)
            assert await policy.persist(request())=='retry'
            assert await policy.persist(request(2))=='accepted'
            assert await policy.persist(request(3))=='accepted'
            assert len(projections)==1 and seen==[('task','answer')]*2
            assert isinstance(task['_terminalDelivery'],TerminalDelivery)
            task['_transientRuntime']=False
            with pytest.raises(ValueError,match='ownership changed'):
                await policy.persist(request(4))
    asyncio.run(run())


@pytest.mark.api
@pytest.mark.parametrize('outcome,active_abort',[('done',False),('error',False),('aborted',False),('aborted',True)])
def test_installed_native_transient_ack_joins_original_delivery_without_storage(tmp_path, monkeypatch, outcome, active_abort):
    archive=os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    from lib.tasks_pkg.manager import create_task
    manifest=verify(Path(archive))
    budget=Budget(1,16<<20,65536,65536,5)
    image=NativeArchiveImage(archive=Path(archive),sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'],budget=budget)
    monkeypatch.setattr('lib.storage.get_storage_client',lambda *_a,**_k:pytest.fail('transient accessed storage'))
    task=create_task('transient-native', [{'role':'user','content':'hello'}], {},principal=principal(),supersede=False,transient=True)
    task['status']='running'
    seen=[];deliveries=[]
    entered,release=threading.Event(),threading.Event()
    def receive(value):
        deliveries.append((value['id'],value['status'],value['content']))
        if outcome=='done' and len(deliveries)==1:raise OSError('lost terminal ACK')
    bind_terminal_delivery(task,receive)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_args):pass
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if outcome=='error':
                self.send_response(400);self.send_header('Content-Length','0');self.end_headers();return
            if active_abort:
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                self.wfile.flush();entered.set();release.wait(5);return
            wire=('data: '+json.dumps({'choices':[{'delta':{'content':'answer'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':3,'completion_tokens':2}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    config=dict(version=manifest['build']['stdio_contract'],type='configure',owner_user_id=7,task_id=task['id'],epoch='epoch',
        endpoint=f'http://127.0.0.1:{server.server_port}/chat',headers={},template={'model':'fixture'},
        messages=[{'role':'user','content':'hello'}],tools={},event_bytes=65536,output_bytes=65536,tool_calls=8,
        request_bytes=65536,rounds=3,retained_bytes=65536,feedback_bytes=4096,tool_frame_bytes=65536,deadline_ms=3000,
        policy=dict(connections=1,idle_connections=1,keepalive_ms=1000,connect_ms=1000,request_headers_ms=2000,
            pool_wait_ms=1000,stream_idle_ms=1000,drain_timeout_ms=100,drain_bytes=65536,drain_chunks=16),
        terminal_persistence=dict(operation_id='commit',frame_bytes=65536,attempts=3,ack_ms=2000))
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            policy=NativeTaskTerminalPolicy(principal=principal(),task=task,operation_id='commit',
                executor=executor,project=project_native_terminal,candidate_bytes=65536,transient=True)
            client=NativeProcessClient.from_shared_archive(image=image,principal=principal(),budget=budget)
            if outcome=='aborted' and not active_abort:task['abort_event'].set()
            try:
                try:
                    running=asyncio.create_task(client.run(config,callbacks={},working_directory=tmp_path,
                        terminal_policy=policy,stop_signal=task['abort_event']))
                    if active_abort:
                        assert await asyncio.to_thread(entered.wait,5),'model request never entered'
                        task['abort_event'].set()
                    await running
                except (NativeProcessError, asyncio.CancelledError):
                    assert outcome in {'error','aborted'}
                assert policy.accepted and task['status']==outcome
            finally:
                await client.close();await image.close()
    try:
        asyncio.run(run())
        assert len(seen)==(0 if outcome=='aborted' and not active_abort else 1)
        assert len(deliveries)==(2 if outcome=='done' else 1)
        assert all(row[1]==outcome for row in deliveries)
        if outcome=='done':assert task['content']=='answer' and task['messages'][-1]['content']=='answer'
        assert image._closed and image._image.done() and image._image.result().closed
    finally:
        release.set();server.shutdown();server.server_close();thread.join(2);assert not thread.is_alive()
