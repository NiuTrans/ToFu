"""Durable v4 ROOT requests use native model and repository execution together."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid

import pytest
from test_native_extraction_stream import stream as stream, assert_joined
from test_native_extraction_owned import blocked_sources, observe_first_ast
from tests.storage.test_storage_application_contract import application_storage as application_storage, application_storage_owner_id

pytestmark = pytest.mark.api


@pytest.mark.parametrize('operation', ['complete', 'abort', 'plain', 'byte_paths'])
def test_durable_http_task_owns_native_repository_scan(application_storage, stream, monkeypatch, operation):
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive:
        pytest.skip('explicit verified native application archive required')
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_process import Budget
    from lib.app.app_assembly import create_application
    from lib.model_routing.repository import OwnerBoundary
    from lib.server.server_loop_runtime import ServingLoopRuntime
    from lib.tasks_pkg.native_application import NativeRootConfiguration
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    from lib.turn_lifecycle import get_attempt, get_turn
    from tofu_agent import NativeRepositoryConfiguration
    from scripts.build_native_runtime import verify
    from tests.model_routing.test_native_route import Repository
    from tests.tasks_pkg.test_native_application import profile
    import tofu_repo_kernel

    scanner, root = stream
    if operation in {'plain', 'byte_paths'}:
        root = root.parent/'plain-http-project'
        root.mkdir()
    (root/'module.js').write_text('function durableNativeRepositorySymbol() { return used; }')
    if operation == 'byte_paths':
        Path(os.fsdecode(os.fsencode(root)+b'/invalid-\xff.js')).write_text('function durableOriginalByte() { return used; }')
        (root/'invalid-\ufffd.js').write_text('function durableReplacementByte() { return used; }')
        monkeypatch.setenv('PATH', str(root/'missing-binaries'))
    entered = None
    if operation == 'abort':
        blocked_sources(root)
        entered = observe_first_ast(monkeypatch)
    seen, images, children, tasks = [], [], [], []
    original_open = tofu_repo_kernel.open_stream_process
    def open_stream():
        image = original_open(); images.append(image)
        def observe(original):
            def start(*args, **kwargs):
                process = original(*args, **kwargs); children.append(process); return process
            return start
        image.start = observe(image.start)
        image.start_rg = observe(image.start_rg)
        return image
    monkeypatch.setattr(tofu_repo_kernel, 'open_stream_process', open_stream)
    def forbidden(*args, **kwargs):
        pytest.fail('explicit native application entered a reference execution path')
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._run.run_root_agent_loop', forbidden)
    monkeypatch.setattr('lib.repo_map.outline.scan', forbidden)

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(body)
            if any(row.get('role') == 'tool' for row in body['messages']):
                delta, reason = {'content':'durable native repository complete'}, 'stop'
            else:
                delta = {'tool_calls':[{'index':0,'id':'durable-repository-call','type':'function',
                    'function':{'name':'repo_map','arguments':'{}'}}]}
                reason = 'tool_calls'
            wire = ('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':reason}],
                    'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200); self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(wire))); self.end_headers(); self.wfile.write(wire)

    source = Path(archive); manifest = verify(source)
    native_image = NativeArchiveImage(archive=source, sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'], budget=Budget(1,32<<20,1<<20,1<<20,5))
    server = ThreadingHTTPServer(('127.0.0.1',0),Provider)
    thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01}); thread.start()
    repository = Repository(); repository.boundary = OwnerBoundary.create(1)
    repository.document['connections'][1]['enabled'] = False
    repository.document['connections'][0]['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
    monkeypatch.setattr('lib.model_routing.ModelRoutingRepository',lambda:repository)
    configuration = profile()
    configuration['model_counter'].update(input_bytes=1<<20,text_bytes=1<<20,tool_json_bytes=1<<20)
    def pricing(principal, task):
        tasks.append(task)
        return NativeRoutePricing(principal=principal,margin=0,
            lookup=lambda owner,model,provider,**kwargs:{'input':2.5,'output':10},rates=lambda principal:7.24)
    options = NativeRootConfiguration(configuration=configuration,working_directory=root,
        repository_factory=lambda principal:repository,pricing_factory=pricing,
        native_repository=NativeRepositoryConfiguration(str(scanner.grammar_directory),scanner.limits.arguments()))

    async def exercise():
        serving = None
        async def start_runtime():
            nonlocal serving
            serving = ServingLoopRuntime(asyncio.get_running_loop(),threading.Event(),environ={
                'TOFU_TASK_CLEANUP_INTERVAL':'0','TOFU_EXECUTOR_IDLE_SECONDS':'0',
                'TOFU_SYNC_WORKERS':'2','TOFU_AGENT_WORKERS':'2'}).start()
            app.extensions['tofu_serving_loop_lifecycle'] = {'runtime':serving}
        async def stop_runtime():
            if serving is not None: await serving.stop()
        app = create_application(__name__,static_dir=str(root),logger=logging.getLogger(__name__),
            secret_key='fixture',config={'TESTING':True},native_image=native_image,native_root=options,
            startup_handlers=[('fixture.serving',start_runtime)],shutdown_handlers=[('fixture.serving',stop_runtime)])
        binding = app.extensions['tofu_native_application']
        assert binding.repository_runtime.retained_image_bytes() == 0
        async with app.test_app():
            conversation = uuid.uuid4().hex
            await asyncio.to_thread(application_storage.command, 'conversation.create',
                dict(conv_id=conversation,user_id=1,title='Native repository HTTP',
                     created_at=1,updated_at=1,settings={}), uuid.uuid4().hex)
            body = {'commandId':uuid.uuid4().hex,'inputTurn':{'content':'Use repo_map to inspect this project'},
                    'config':{'maxTokens':64,'thinkingEnabled':False,'modelRef':{'creator_id':'creator','model_id':'alpha'},
                              'projectEnabled':True,'projectPath':str(root),'memoryEnabled':False,
                              'searchMode':'off','tools':{'programmaticCalling':'off'}}}
            client = app.test_client()
            response = await client.post(f'/api/v4/conversations/{conversation}/turns',json=body)
            payload = await response.get_json()
            assert response.status_code == 200 and payload['ok'] is True, payload
            attempt_id = payload['attempt']['attemptId']; turn_id = payload['turn']['turnId']
            if entered is not None:
                assert await asyncio.to_thread(entered.wait,5), 'native repository callback did not enter'
                stopped = await client.post(f'/api/v4/attempts/{attempt_id}/abort',json={})
                assert stopped.status_code == 200, await stopped.get_json()
            expires = time.monotonic()+15
            while True:
                attempt = await asyncio.to_thread(get_attempt,attempt_id,user_id=1)
                if attempt['status'] in {'completed','failed','interrupted'}: break
                assert time.monotonic() < expires, attempt
                await asyncio.sleep(.02)
            turn = await asyncio.to_thread(get_turn,conversation,turn_id,user_id=1)
            assert attempt['status'] == turn['status'] == ('interrupted' if entered is not None else 'completed'), turn
            while app.extensions['tofu_native_host']._clients or tasks[0].get('_nativePreparedInvocation') is not None:
                assert time.monotonic() < expires, 'native task did not join'
                await asyncio.sleep(.01)
            assert len(tasks) == 1 and tasks[0]['_repository_runtime'] is binding.repository_runtime
            assert tasks[0]['_repository_scanner_closed'] and '_repository_scanner_binding' not in tasks[0]
            assert len(images) == 1 and len(children) == (2 if operation in {'plain', 'byte_paths'} else 1)
            assert not images[0]._image.closed
            if entered is None:
                assert turn['projection']['content'] == 'durable native repository complete'
                assert len(seen) == 2
                tool_results = [row for row in seen[-1]['messages'] if row.get('role') == 'tool']
                assert len(tool_results) == 1 and 'durableNativeRepositorySymbol' in str(tool_results[0]['content'])
                if operation == 'byte_paths':
                    assert 'durableOriginalByte' in str(tool_results[0]['content'])
                    assert 'durableReplacementByte' in str(tool_results[0]['content'])
                assert turn['projection']['toolRounds']
            else:
                assert len(seen) == 1
        assert binding.closed and binding.repository_runtime.retained_image_bytes() == 0
        assert images[0]._image.closed and native_image._closed
    try:
        asyncio.run(exercise())
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
    assert not thread.is_alive()
    assert_joined(children)
