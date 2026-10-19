"""Installed native round policy: absent cap and explicit cap preserve effects."""
import asyncio
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import hashlib
import os
from pathlib import Path
import threading

import pytest
from lib.agent_core.native_process import Budget,NativeProcessClient
from lib.identity import PrincipalContext
from scripts.build_native_runtime import verify
from tests.support.native_process_probe import config

pytestmark=pytest.mark.api

@pytest.mark.parametrize('round_limit',[None,3])
def test_installed_optional_round_limit_preserves_dispatch_and_text(round_limit,tmp_path):
    archive=os.environ.get('TOFU_NATIVE_OPTIONAL_ROUNDS_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native archive required')
    archive=Path(archive);manifest=verify(archive)
    seen=[];calls=[];text=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            number=len(seen);seen.append(body)
            if number:assert body['messages'][-1]['content']==f'tool{number-1}'
            delta={'content':'finished'} if number==7 else {'reasoning':'r','tool_calls':[{
                'index':0,'id':f'call-{number}','type':'function','function':{
                    'name':'lookup','arguments':json.dumps({'round':number})}}]}
            wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':'stop' if number==7 else 'tool_calls'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    async def run():
        client=NativeProcessClient.from_archive(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'],principal=PrincipalContext.user(subject_id='optional-rounds',owner_user_id=7),
            budget=Budget(1,64<<20,1<<20,1<<20,2))
        settings=config(f'http://127.0.0.1:{server.server_port}/chat','optional-rounds')
        settings['rounds']=round_limit;settings['deadline_ms']=5000
        async def lookup(request):
            calls.append(request.arguments['round'])
            return f'tool{request.arguments["round"]}'
        async def observe(delta):text.append(delta)
        try:
            result=await client.run(settings,callbacks={'lookup':lookup},on_text=observe,working_directory=tmp_path)
            assert client.active_runs==0
            assert result['status']==('done' if round_limit is None else 'error')
            if round_limit is not None:assert result['error']['kind']=='agent_round_budget'
        finally:await client.close()
    try:asyncio.run(run())
    finally:server.shutdown();thread.join();server.server_close()
    assert calls==list(range(7 if round_limit is None else 2))
    assert len(seen)==(8 if round_limit is None else 3)
    assert [delta.sequence for delta in text]==list(range(1,len(text)+1))
    assert ''.join(delta.text for delta in text)==('r'*7+'finished' if round_limit is None else 'r'*3)
