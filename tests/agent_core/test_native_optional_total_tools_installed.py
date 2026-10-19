"""Installed native total-tool policy stays separate from response admission."""
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

@pytest.mark.parametrize('total_limit',['legacy',None,0,3])
def test_installed_total_tool_limit_preserves_response_bound_and_effects(total_limit,tmp_path):
    archive=os.environ.get('TOFU_NATIVE_TOTAL_TOOLS_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native archive required')
    archive=Path(archive);manifest=verify(archive)
    seen=[];calls=[];text=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            number=len(seen);seen.append(body)
            if number:assert body['messages'][-1]['content']==f'tool{number-1}'
            delta={'content':'finished'} if number==12 else {'reasoning':'r','tool_calls':[{
                'index':0,'id':f'call-{number}','type':'function','function':{
                    'name':'lookup','arguments':json.dumps({'round':number})}}]}
            wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':'stop' if number==12 else 'tool_calls'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    async def run():
        client=NativeProcessClient.from_archive(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'],principal=PrincipalContext.user(subject_id='optional-total-tools',owner_user_id=7),
            budget=Budget(1,64<<20,1<<20,1<<20,2))
        settings=config(f'http://127.0.0.1:{server.server_port}/chat','optional-total-tools')
        settings['rounds']=None;settings['deadline_ms']=5000
        if total_limit!='legacy':settings['total_tool_calls']=total_limit
        async def lookup(request):
            calls.append(request.arguments['round'])
            return f'tool{request.arguments["round"]}'
        async def observe(delta):text.append(delta)
        try:
            result=await client.run(settings,callbacks={'lookup':lookup},on_text=observe,working_directory=tmp_path)
            assert client.active_runs==0
            assert result['status']==('done' if total_limit is None else 'error')
            if total_limit is not None:assert result['error']['kind']=='agent_tool_budget'
        finally:await client.close()
    try:asyncio.run(run())
    finally:server.shutdown();thread.join();server.server_close()
    limit=12 if total_limit is None else 8 if total_limit=='legacy' else total_limit
    assert calls==list(range(limit))
    assert len(seen)==(13 if total_limit is None else limit+1)
    assert [delta.sequence for delta in text]==list(range(1,len(text)+1))
    assert ''.join(delta.text for delta in text)==('r'*12+'finished' if total_limit is None else 'r'*(limit+1))
