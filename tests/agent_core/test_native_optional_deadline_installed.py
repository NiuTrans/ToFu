"""Installed native lifetime: explicit absence and finite expiry preserve ownership."""
import asyncio
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import hashlib
import os
from pathlib import Path
import threading
import time

import pytest
from lib.agent_core.native_process import Budget,NativeProcessClient
from lib.identity import PrincipalContext
from scripts.build_native_runtime import verify
from tests.support.native_process_probe import config

pytestmark=pytest.mark.api

@pytest.mark.parametrize('mode',['unbounded','relative','absolute','absolute-expired','initial-stop'])
def test_installed_optional_deadline_preserves_expiry_and_cleanup(mode,tmp_path):
    archive=os.environ.get('TOFU_NATIVE_OPTIONAL_DEADLINE_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native archive required')
    archive=Path(archive);manifest=verify(archive)
    seen=[];calls=[];text=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(body)
            time.sleep(1)
            delta={'content':'finished'}
            wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':'stop'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(wire)));self.end_headers()
            try:self.wfile.write(wire)
            except (BrokenPipeError,ConnectionResetError):pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    async def run():
        client=NativeProcessClient.from_archive(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'],principal=PrincipalContext.user(subject_id='optional-deadline',owner_user_id=7),
            budget=Budget(1,64<<20,1<<20,1<<20,2))
        settings=config(f'http://127.0.0.1:{server.server_port}/chat','optional-deadline')
        settings['rounds']=None;settings['deadline_ms']=500 if mode=='relative' else None
        if mode=='initial-stop':settings['cancel_requested']=True
        if mode.startswith('absolute'):
            settings['deadline_unix_ms']=int(time.time()*1000)+(500 if mode=='absolute' else -1)
        async def lookup(request):
            calls.append(request.arguments['round'])
            return f'tool{request.arguments["round"]}'
        async def observe(delta):text.append(delta)
        try:
            result=await client.run(settings,callbacks={'lookup':lookup},on_text=observe,working_directory=tmp_path)
            assert client.active_runs==0
            assert result['status']==('done' if mode=='unbounded' else 'aborted')
        finally:await client.close()
    try:asyncio.run(run())
    finally:server.shutdown();thread.join();server.server_close()
    assert calls==[]
    assert len(seen)==(0 if mode in ('absolute-expired','initial-stop') else 1)
    assert ''.join(delta.text for delta in text)==('finished' if mode=='unbounded' else '')
