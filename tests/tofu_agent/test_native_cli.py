"""CLI Rust composition owns archive/loop/callback lifetime and preserves results."""
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest
from tofu_agent.cli import main
from tests.support.standalone_model_routing import standalone_model_routing_envelope
from tests.tasks_pkg.test_native_application import profile


@pytest.mark.unit
@pytest.mark.parametrize('arguments',[
    ['--engine','rust'], ['--engine','rust','--native-archive','/missing'],
])
def test_native_cli_requires_explicit_local_archive_and_profile(arguments,capsys):
    with pytest.raises(SystemExit) as caught:main(['run','--task','hello',*arguments])
    assert caught.value.code==2
    assert 'requires --native-archive and --native-profile' in capsys.readouterr().err


@pytest.mark.api
@pytest.mark.parametrize('command',['run','doctor','serve','provider_error'])
def test_native_cli_uses_real_archive_without_python_loop_or_storage(tmp_path,monkeypatch,capsys,command):
    archive=os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native CLI archive required')
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_args):pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if command=='provider_error':
                self.send_response(400);self.send_header('Content-Length','0');self.end_headers();return
            wire=('data: '+json.dumps({'choices':[{'delta':{'content':'cli-native-answer'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':3,'completion_tokens':2}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    document=standalone_model_routing_envelope(base_url=f'http://127.0.0.1:{server.server_port}/v1')
    document['model_routing']['models'][0]['context_window']=131072
    document['model_routing']['deployments'][0]['max_output_tokens']=1024
    access=tmp_path/'access.json';access.write_text(json.dumps(document))
    native_profile=profile();native_profile['model_control']['pricing']=False
    native_profile['model_counter'].update(input_bytes=1<<20,text_bytes=1<<20,tool_json_bytes=1<<20)
    resource_file=tmp_path/'native-profile.json';resource_file.write_text(json.dumps(native_profile))
    monkeypatch.setattr('lib.storage.get_storage_client',lambda *_a,**_k:pytest.fail('CLI accessed storage'))
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._run.run_root_agent_loop',lambda *_a,**_k:pytest.fail('CLI entered Python model loop'))
    selected_command='run' if command=='provider_error' else command
    argv=[selected_command,'--engine','rust','--native-archive',archive,'--native-profile',str(resource_file),
        '--model-routing-file',str(access),'--max-inflight','1']
    if selected_command=='run':argv.extend(['--task','hello','--cwd',str(tmp_path),'--timeout-s','10'])
    served=[]
    if command=='serve':
        argv.extend(['--token','native-cli-test-token'])
        auth={'Authorization':'Bearer native-cli-test-token'}
        async def serve(app,config):
            client=app.test_client()
            response=await client.get('/capabilities',headers=auth)
            if response.status_code==404:response=await client.get('/api/v1/capabilities',headers=auth)
            assert response.status_code==200
            assert (await response.get_json())['execution']['backend']=='rust'
            response=await client.post('/api/v1/agent/run',headers=auth,json={'messages':[{'role':'user','content':'hello'}]})
            assert response.status_code==200,await response.get_json()
            served.append(await response.get_json())
        monkeypatch.setattr('hypercorn.asyncio.serve',serve)
    try:
        assert main(argv)==(4 if command=='provider_error' else 0)
        captured=capsys.readouterr()
        result=served[0] if served else json.loads(captured.out)
        if command=='provider_error':
            assert result['ok'] is False and result['status']=='error' and len(requests)==1
        elif command in {'run','serve'}:
            assert result['ok']
            assert result['content']=='cli-native-answer' and result['usage']['prompt_tokens']==3
            assert len(requests)==1
        else:
            assert result['execution']['backend']=='rust' and result['ready']
            assert not requests
        assert not any(t.is_alive() and t.name.startswith('tofu-native-sdk-callback') for t in threading.enumerate())
    finally:server.shutdown();server.server_close();thread.join(2);assert not thread.is_alive()


@pytest.mark.api
def test_real_cli_sigint_joins_native_process_tree(tmp_path,monkeypatch):
    import signal
    import subprocess
    import sys
    import psutil
    archive=os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native CLI archive required')
    entered,release=threading.Event(),threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']));entered.set();release.wait(5)
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    document=standalone_model_routing_envelope(base_url=f'http://127.0.0.1:{server.server_port}/v1')
    document['model_routing']['deployments'][0]['max_output_tokens']=1024
    access=tmp_path/'access-signal.json';access.write_text(json.dumps(document))
    value=profile();value['model_control']['pricing']=False
    value['model_counter'].update(input_bytes=1<<20,text_bytes=1<<20,tool_json_bytes=1<<20)
    resource=tmp_path/'profile-signal.json';resource.write_text(json.dumps(value))
    root=Path(__file__).resolve().parents[2]
    launcher=json.loads(os.environ.get('TOFU_NATIVE_TEST_PYTHON_COMMAND','null')) or [sys.executable]
    command=launcher+['-m','tofu_agent','run','--task','hello','--engine','rust','--native-archive',archive,
        '--native-profile',str(resource),'--model-routing-file',str(access),'--cwd',str(tmp_path),
        '--timeout-s','30','--trajectory','']
    process=subprocess.Popen(command,cwd=root,env=dict(os.environ),stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,text=True)
    children=[]
    try:
        assert entered.wait(10),'native CLI provider request did not enter'
        children=psutil.Process(process.pid).children(recursive=True)
        assert children,'CLI did not own a native process'
        process.send_signal(signal.SIGINT)
        _stdout,stderr=process.communicate(timeout=10)
        assert process.returncode in (130,-signal.SIGINT),stderr[-2000:]
        assert all(not child.is_running() or child.status()==psutil.STATUS_ZOMBIE for child in children)
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL);process.communicate(timeout=5)
        release.set();server.shutdown();server.server_close();thread.join(2);assert not thread.is_alive()
