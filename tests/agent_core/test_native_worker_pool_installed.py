"""Actual sealed worker reuse and callback failure retire the process before reuse."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import pytest
from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.native_process import Budget,NativeToolError
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_host import NativeHost
from tests.support.native_process_probe import config
from scripts.build_native_runtime import verify

pytestmark=pytest.mark.api


def test_installed_worker_reuses_success_and_retires_failed_callback(tmp_path):
    source=os.environ.get('TOFU_NATIVE_WORKER_TEST_ARCHIVE')
    if not source:pytest.skip('explicit verified native worker archive required')
    archive=Path(source);manifest=verify(archive);seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));seen.append(body)
            tool=body['messages'][0]['content']=='tool'
            delta={'tool_calls':[{'index':0,'id':'call','type':'function','function':{'name':'lookup','arguments':'{}'}}]} if tool else {'content':'done'}
            wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':'tool_calls' if tool else 'stop'}]})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    async def run():
        budget=Budget(1,32<<20,1<<20,1<<20,2)
        image=NativeArchiveImage(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),application_version=manifest['build']['application_version'],budget=budget)
        host=NativeHost(image);host.enable_worker_reuse({'root_maximum':2})
        identity=PrincipalContext.user(subject_id='worker-test',owner_user_id=7)
        settings=config(f'http://127.0.0.1:{server.server_port}/chat','same-task')
        async def lookup(_request):raise RuntimeError('injected callback failure')
        pids=[]
        try:
            for _ in range(2):
                async with host.client(identity,budget=budget) as client:
                    assert (await client.run(settings,callbacks={'lookup':lookup},working_directory=tmp_path))['status']=='done'
                pids.append(next(iter(host._worker_pool.idle)).process.pid)
            assert pids[0]==pids[1]
            settings['messages'][0]['content']='tool'
            async with host.client(identity,budget=budget) as client:
                with pytest.raises(NativeToolError):await client.run(settings,callbacks={'lookup':lookup},working_directory=tmp_path)
            assert not host._worker_pool.idle and not host._worker_pool.workers
            settings['messages'][0]['content']='hello'
            async with host.client(identity,budget=budget) as client:
                assert (await client.run(settings,callbacks={'lookup':lookup},working_directory=tmp_path))['status']=='done'
            assert next(iter(host._worker_pool.idle)).process.pid!=pids[0]
        finally:await host.close()
        assert not host._worker_pool.workers and image._closed and not host._clients
    try:asyncio.run(run())
    finally:server.shutdown();server.server_close();thread.join()
    assert len(seen)==4
