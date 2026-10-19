"""Installed error/Stop history reaches the existing terminal projection once."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch

import pytest
from lib.agent_core.native_process import Budget,NativeProcessClient,NativeProcessError
from lib.agent_core.abort_signal import AbortSignal
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy,project_native_terminal
from tests.agent_core.test_native_process import principal
from tests.support.native_process_probe import config
from scripts.build_native_runtime import verify

pytestmark=pytest.mark.api


@pytest.mark.parametrize('mode',['error','cancel'])
def test_installed_retained_tool_prefix_survives_error_and_stop(mode,tmp_path):
    source=os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not source:pytest.skip('explicit verified native archive required')
    archive=Path(source);manifest=verify(archive)
    entered,release=threading.Event(),threading.Event();seen=[];effects=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if len(seen)==2:
                entered.set()
                if mode=='error':
                    self.send_response(500);self.send_header('Content-Length','0');self.end_headers();return
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                release.wait(5);return
            wire=b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call","type":"function","function":{"name":"lookup","arguments":"{}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler)
    async def run():
        bounds=Budget(1,32<<20,65536,131072,5)
        client=NativeProcessClient.from_archive(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'],principal=principal(),budget=bounds)
        task={'id':'retained-'+mode,'_userId':7,'status':'running','messages':[],
              'usage':{'total_tokens':31},'abort_event':AbortSignal()}
        settings=config(f'http://127.0.0.1:{server.server_port}/chat',task['id'])
        settings.update(retain_execution_history=True,deadline_ms=None,
            terminal_persistence={'operation_id':'commit','attempts':2,'ack_ms':2000,'frame_bytes':131072})
        async def lookup(request):effects.append(request.name);return 'observed tool result'
        with ThreadPoolExecutor(max_workers=1) as executor:
            policy=NativeTaskTerminalPolicy(principal=principal(),task=task,operation_id='commit',executor=executor,
                project=project_native_terminal,candidate_bytes=131072)
            try:
                with patch('lib.tasks_pkg.native_terminal._persist',return_value=True) as persist:
                    work=asyncio.create_task(client.run(settings,callbacks={'lookup':lookup},terminal_policy=policy,working_directory=tmp_path))
                    if mode=='cancel':
                        assert await asyncio.to_thread(entered.wait,3)
                        work.cancel()
                        with pytest.raises(asyncio.CancelledError):await work
                    else:assert (await work)['status']=='error'
                    assert persist.call_count==1
                assert task['messages'][-1]['role']=='tool'
                assert task['messages'][-1]['content']=='observed tool result'
                assert task['_messages_include_current_run']
                assert task['usage']=={'total_tokens':31}
                assert task['status']==('error' if mode=='error' else 'aborted')
                assert client.active_runs==0
            finally:await client.close()
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    try:asyncio.run(run())
    finally:release.set();server.shutdown();thread.join();server.server_close()
    assert effects==['lookup'] and len(seen)==2


def test_old_archive_refuses_retention_configuration_before_provider(tmp_path):
    source=os.environ.get('TOFU_NATIVE_RETAINED_HISTORY_OLD_ARCHIVE')
    if not source:pytest.skip('explicit old archive required')
    archive=Path(source);manifest=verify(archive);seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):seen.append(True);self.send_response(500);self.end_headers()
    server=HTTPServer(('127.0.0.1',0),Handler)
    async def run():
        client=NativeProcessClient.from_archive(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'],principal=principal(),budget=Budget(1,32<<20,65536,131072,5))
        settings=config(f'http://127.0.0.1:{server.server_port}/chat','old-retention');settings.update(retain_execution_history=True,tools={})
        try:
            with pytest.raises(NativeProcessError):await client.run(settings,callbacks={},working_directory=tmp_path)
            assert client.active_runs==0
        finally:await client.close()
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    try:asyncio.run(run())
    finally:server.shutdown();thread.join();server.server_close()
    assert not seen
