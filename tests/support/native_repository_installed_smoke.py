"""Run an installed native SDK/provider/tool cycle outside the checkout."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import tofu_agent
from tofu_agent import AgentRuntime, AgentTimeoutError, NativeAgentConfiguration, NativeRepositoryConfiguration, ModelRoutingConfig, PrincipalContext
from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.native_process import Budget
from lib.tasks_pkg.native_host import NativeHost
import tofu_repo_kernel

installed=Path(sys.argv[1]).resolve()
assert Path(tofu_agent.__file__).is_relative_to(installed)
fixture=json.loads(Path(sys.argv[2]).read_text())
repository_kind = sys.argv[3] if len(sys.argv) > 3 else 'git'
assert repository_kind in {'git', 'plain', 'byte_paths', 'held_history'}
grammar_selection = sys.argv[4] if len(sys.argv) > 4 else 'external'
assert grammar_selection in {'installed', 'external'}
seen=[];images=[];children=[]
history_jobs=[];history_finished=[];close_withheld_while_history=False
history_entered=threading.Event();history_release=threading.Event();history_drained=threading.Event()
if repository_kind == 'held_history':
    from lib.tasks_pkg.commit_round import _commit as history_commit
    original_history=history_commit._run_commit_round_async
    def held_history(*args,**kwargs):
        history_jobs.append(threading.current_thread().name)
        if len(history_jobs)==2:
            history_entered.set()
            assert history_release.wait(15),'history fault injection was not released'
        try:return original_history(*args,**kwargs)
        finally:
            history_finished.append(True)
            if len(history_finished)==2:history_drained.set()
    history_commit._run_commit_round_async=held_history
original=tofu_repo_kernel.open_stream_process

def observe_open():
    image=original();images.append(image)
    def observe(start):
        def observe_start(*args,**kwargs):
            child=start(*args,**kwargs);children.append(child);return child
        return observe_start
    image.start=observe(image.start)
    image.start_rg=observe(image.start_rg)
    return image

tofu_repo_kernel.open_stream_process=observe_open
class Provider(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));seen.append(body)
        if any(row.get('role')=='tool' for row in body['messages']):
            delta,reason={'content':'installed native model tool cycle'},'stop'
        else:
            delta={'tool_calls':[{'index':0,'id':'installed-native-call','type':'function',
                   'function':{'name':'repo_map','arguments':'{}'}}]};reason='tool_calls'
        wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':reason}],
              'usage':{'prompt_tokens':3,'completion_tokens':2}})+'\n\ndata: [DONE]\n\n').encode()
        self.send_response(200);self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)

archive=Path(os.environ['TOFU_NATIVE_TERMINAL_TEST_ARCHIVE'])
native_image=NativeArchiveImage(archive=archive,sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                              application_version=tofu_agent.__version__,budget=Budget(1,32<<20,1<<20,1<<20,5))
host=NativeHost(native_image)
server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
try:
    for connection in fixture['routing']['model_routing']['connections']:
        connection['base_url']=f'http://127.0.0.1:{server.server_port}/v1'
    access=ModelRoutingConfig.from_mapping(fixture['routing'])
    results=[]
    with tempfile.TemporaryDirectory(prefix='installed-native-model-loop-') as directory:
        root=Path(directory)
        (root/'module.js').write_text('function installedModelLoopSymbol() { return used; }')
        if repository_kind == 'git':
            subprocess.run(['git','init','-q',directory],check=True)
        if repository_kind == 'byte_paths':
            Path(os.fsdecode(os.fsencode(root)+b'/invalid-\xff.js')).write_text('function installedOriginalByte() { return used; }')
            (root/'invalid-\ufffd.js').write_text('function installedReplacementByte() { return used; }')
            os.environ['PATH'] = str(root/'missing-binaries')
        async def exercise():
            global close_withheld_while_history
            with ThreadPoolExecutor(max_workers=2) as callbacks:
                native=NativeAgentConfiguration(host=host,loop=asyncio.get_running_loop(),executor=callbacks,
                    configuration=fixture['profile'],working_directory=root,resource_policy={'frame_maximum':1<<20},
                    pending_calls=8,pending_bytes=1<<20)
                repository=(NativeRepositoryConfiguration.from_launch_policy(launch_policy={'frame_maximum':128<<20})
                    if grammar_selection=='installed' else NativeRepositoryConfiguration(
                        os.environ['TOFU_NATIVE_EXTRACTION_GRAMMARS'],(8<<20,8<<20,32<<20,16<<20)))
                runtime=AgentRuntime(principal=PrincipalContext.user(subject_id='installed-model-loop',owner_user_id=73),
                    model_routing=access,max_inflight=1,native_execution=native,native_repository=repository)
                import lib.tasks_pkg.orchestrator._run as orchestrator
                import lib.repo_map.outline as outline
                def forbidden(*args,**kwargs):raise AssertionError('native installed path entered a reference implementation')
                orchestrator.run_root_agent_loop=forbidden;outline.scan=forbidden
                try:
                    for iteration in range(2):
                        execution=runtime.start([{'role':'user','content':'Use repo_map'}],
                            config={'tools':['repo_map'],'project':directory},timeout_s=15)
                        if repository_kind == 'held_history' and iteration == 1:
                            try:
                                assert await asyncio.to_thread(history_entered.wait,5),'real history work did not start'
                                try:await runtime.close_async(abort=False,timeout_s=.03)
                                except AgentTimeoutError:close_withheld_while_history=True
                                else:raise AssertionError('runtime close returned while owned file history was active')
                                assert not execution._terminal.is_set() and runtime.in_flight==1
                            finally:
                                history_release.set()
                                assert await asyncio.to_thread(history_drained.wait,5),'owned history writer did not drain'
                        result=await execution.result_async()
                        assert result.status=='done' and result.content=='installed native model tool cycle',result.error
                        assert execution._terminal.is_set() and runtime.in_flight==0 and not host._clients
                        assert execution._task['_repository_scanner_closed']
                        results.append({'status':result.status,'usage':result.usage})
                    assert len(seen)==4 and len(images)==1 and len(children)==(2 if repository_kind=='git' else 4)
                    for body in seen[1::2]:
                        rows=[row for row in body['messages'] if row.get('role')=='tool']
                        assert len(rows)==1 and 'installedModelLoopSymbol' in str(rows[0]['content'])
                        if repository_kind == 'byte_paths':
                            assert 'installedOriginalByte' in str(rows[0]['content'])
                            assert 'installedReplacementByte' in str(rows[0]['content'])
                finally:
                    try:await runtime.close_async()
                    finally:await host.close()
                assert images[0]._image.closed and runtime._repository_runtime.retained_image_bytes()==0
                assert native_image._closed and native_image._image.result().closed
                assert [child.poll() for child in children] == ([0,127,0,127] if repository_kind == 'byte_paths' else [0]*len(children))
                assert all(pipe.closed for child in children for pipe in (child.stdin,child.stdout,child.stderr))
        asyncio.run(exercise())
finally:
    server.shutdown();server.server_close();thread.join(2)
    tofu_repo_kernel.open_stream_process=original
assert not thread.is_alive()
for name,module in sys.modules.items():
    if name=='lib' or name.startswith('lib.') or name=='tofu_agent' or name.startswith('tofu_agent.') or name=='tofu_repo_kernel' or name.startswith('tofu_repo_kernel.'):
        path=getattr(module,'__file__',None)
        assert path is None or Path(path).is_relative_to(installed),(name,path)
assert 'lib.storage' not in sys.modules
assert not any(name=='tests' or name.startswith(('tests.','benchmarks.')) for name in sys.modules)
print(json.dumps({'installed_agent':str(installed),'native_archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
    'repository_kind':repository_kind,'grammar_selection':grammar_selection,'model_requests':len(seen),'sdk_runs':len(results),'scan_images':len(images),'joined_scan_children':len(children),
    'results':results,'native_host_closed':native_image._closed,'retained_code_after_close':0,
    'history_jobs_joined':len(history_finished),'close_withheld_while_history':close_withheld_while_history,
    'source_or_benchmark_imports':False},indent=2))
