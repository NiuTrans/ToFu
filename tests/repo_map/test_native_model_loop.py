"""Real Rust SDK model/tool rounds own the installed repository scanner.

Only the upstream provider is a loopback fixture. Routing, preparation, native
model loop, registered gateway, scan, publication and terminal cleanup stay real.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

import pytest
from test_native_extraction_stream import stream as stream, assert_joined
from test_native_extraction_owned import blocked_sources, observe_first_ast
from tests.support.standalone_model_routing import standalone_model_routing_envelope
from tests.tasks_pkg.test_native_application import profile

pytestmark = pytest.mark.api


@pytest.mark.parametrize(('mode', 'repository_kind'), [(mode, 'git') for mode in
    ['success', 'abort', 'close', 'artifact_refusal', 'child']] + [(mode, kind) for mode in ['success', 'child'] for kind in ['plain', 'byte_paths']])
def test_native_model_rounds_use_task_owned_repository_stream(stream, monkeypatch, mode, repository_kind):
    from tofu_agent import AgentRuntime, NativeAgentConfiguration, NativeRepositoryConfiguration, ModelRoutingConfig, PrincipalContext
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_process import Budget
    from lib.tasks_pkg.native_host import NativeHost
    from scripts.build_native_runtime import verify
    import tofu_repo_kernel

    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive:
        pytest.skip('explicit verified native SDK archive required')
    scanner, root = stream
    if repository_kind != 'git':
        root = root.parent/'plain-model-project'
        root.mkdir()
    (root/'module.js').write_text('function nativeLoopSymbol() { return referenced; }')
    if repository_kind == 'byte_paths':
        Path(os.fsdecode(os.fsencode(root)+b'/invalid-\xff.js')).write_text('function originalByteSymbol() { return referenced; }')
        (root/'invalid-\ufffd.js').write_text('function replacementByteSymbol() { return referenced; }')
        monkeypatch.setenv('PATH', str(root/'missing-binaries'))
    entered = None
    if mode in {'abort', 'close'}:
        blocked_sources(root)
        entered = observe_first_ast(monkeypatch)
    images, children, storage_calls, seen = [], [], [], []
    original_open = tofu_repo_kernel.open_stream_process
    def open_stream():
        if mode == 'artifact_refusal':
            raise RuntimeError('native model-loop artifact refused')
        image = original_open()
        def observe(original):
            def start(*args, **kwargs):
                child = original(*args, **kwargs)
                children.append(child)
                return child
            return start
        image.start = observe(image.start)
        image.start_rg = observe(image.start_rg)
        images.append(image)
        return image
    monkeypatch.setattr(tofu_repo_kernel, 'open_stream_process', open_stream)
    def deny_storage(*args, **kwargs):
        storage_calls.append((args, kwargs))
        raise AssertionError('standalone SDK accessed application storage')
    def deny_python_loop(*args, **kwargs):
        pytest.fail('native SDK entered the Python model loop')
    def deny_reference_scan(*args, **kwargs):
        pytest.fail('selected native repository scan fell back to Python discovery')
    monkeypatch.setattr('lib.storage.get_storage_client', deny_storage)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._run.run_root_agent_loop', deny_python_loop)
    monkeypatch.setattr('lib.repo_map.outline.scan', deny_reference_scan)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(body)
            if any(message.get('role') == 'tool' for message in body['messages']):
                delta = {'content': 'native repository round complete'}
                reason = 'stop'
            else:
                delta = {'tool_calls': [{'index': 0, 'id': 'native-repository-call', 'type': 'function',
                    'function': {'name': ('custom__delegate' if mode == 'child' and any(
                        tool.get('function', {}).get('name') == 'custom__delegate' for tool in body.get('tools', []))
                        else 'repo_map'), 'arguments': '{}'}}]}
                reason = 'tool_calls'
            payload = {'choices': [{'delta': delta, 'finish_reason': reason}],
                       'usage': {'prompt_tokens': 3, 'completion_tokens': 2}}
            wire = ('data: '+json.dumps(payload)+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    server_thread.start()
    manifest = verify(Path(archive))
    image = NativeArchiveImage(archive=Path(archive), sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'], budget=Budget(1,32<<20,1<<20,1<<20,5))
    host = NativeHost(image)
    envelope = standalone_model_routing_envelope(base_url=f'http://127.0.0.1:{server.server_port}/v1')
    envelope['model_routing']['models'][0]['context_window'] = 131072
    envelope['model_routing']['deployments'][0]['max_output_tokens'] = 1024
    access = ModelRoutingConfig.from_mapping(envelope)

    async def exercise():
        with ThreadPoolExecutor(max_workers=2) as callbacks:
            configuration = profile()
            configuration['model_control']['pricing'] = False
            configuration['model_counter'].update(input_bytes=1<<20, text_bytes=1<<20, tool_json_bytes=1<<20)
            native = NativeAgentConfiguration(host=host, loop=asyncio.get_running_loop(), executor=callbacks,
                configuration=configuration, working_directory=root, resource_policy={'frame_maximum':1<<20},
                pending_calls=8, pending_bytes=1<<20)
            repository = NativeRepositoryConfiguration(str(scanner.grammar_directory), scanner.limits.arguments())
            runtime = AgentRuntime(principal=PrincipalContext.user(subject_id='native-repository-loop', owner_user_id=73),
                model_routing=access, max_inflight=1, native_execution=native, native_repository=repository)
            try:
                assert runtime._repository_runtime.retained_image_bytes() == 0
                for _ in range(2 if mode == 'success' else 1):
                    custom = {}
                    if mode == 'child':
                        custom = {'custom_tools':[{'type':'function', 'function':{'name':'custom__delegate',
                            'description':'Delegate the repository inspection',
                            'parameters':{'type':'object', 'properties':{}, 'additionalProperties':False}},
                            'execution':{'mode':'client'}}]}
                    execution = runtime.start([{'role':'user', 'content':'Use repo_map for this project'}],
                        config={'tools':['repo_map'], 'project':str(root)}, timeout_s=15, **custom)
                    child = None
                    if mode == 'child':
                        from lib.tasks_pkg.child_invocation import create_child_invocation, run_child_invocation
                        async for event in execution.events_async(timeout_s=15):
                            if event.get('type') == 'custom_tool_call':
                                assert child is None
                                child_tools = [tool for tool in seen[0]['tools']
                                               if tool.get('function', {}).get('name') == 'repo_map']
                                assert len(child_tools) == 1
                                child = create_child_invocation(execution._task, model='model-a', tools=child_tools, project_path=str(root),
                                    objective='Use repo_map to inspect the project', event_sink=lambda event:None,
                                    abort_check=lambda:execution._task['abort_event'].is_set())
                                assert child['_repository_runtime'] is runtime._repository_runtime
                                completed = await asyncio.to_thread(run_child_invocation, child)
                                assert completed['status'] == 'done'
                                assert child['_repository_scanner_closed'] and '_repository_scanner_binding' not in child
                                assert execution.resolve_custom_tool_call(event['callId'], content='child inspected nativeLoopSymbol')
                        assert child is not None
                    if entered is not None:
                        assert await asyncio.to_thread(entered.wait, 5), 'native repository callback did not enter'
                        if mode == 'abort':
                            assert execution.abort()
                        else:
                            await runtime.close_async(timeout_s=5)
                    result = await execution.result_async()
                    assert result.status == ('aborted' if entered is not None else 'done'), (result.error, seen)
                    assert execution._terminal.is_set() and runtime.in_flight == 0 and not host._clients
                    task = execution._task
                    if mode != 'child':
                        assert task['_repository_scanner_closed'] and '_repository_scanner_binding' not in task
                    else:
                        assert '_repository_scanner_binding' not in task
                        child_tools = [message for message in seen[2]['messages'] if message.get('role') == 'tool']
                        assert len(child_tools) == 1 and 'nativeLoopSymbol' in str(child_tools[0]['content'])
                        if repository_kind == 'byte_paths':
                            assert 'originalByteSymbol' in str(child_tools[0]['content'])
                            assert 'replacementByteSymbol' in str(child_tools[0]['content'])
                    if entered is None:
                        assert result.content == 'native repository round complete'
                        tools = [message for message in seen[-1]['messages'] if message.get('role') == 'tool']
                        expected = 'native model-loop artifact refused' if mode == 'artifact_refusal' else 'nativeLoopSymbol'
                        assert len(tools) == 1 and expected in str(tools[0]['content']), tools
                        if repository_kind == 'byte_paths' and mode != 'child':
                            assert 'originalByteSymbol' in str(tools[0]['content'])
                            assert 'replacementByteSymbol' in str(tools[0]['content'])
                        assert result.usage.get('prompt_tokens') == 6 and result.usage.get('completion_tokens') == 4
                assert not storage_calls
                assert len(seen) == (4 if mode in {'success', 'child'} else 1 if entered is not None else 2)
                assert len(images) == (0 if mode == 'artifact_refusal' else 1)
                assert len(children) == (2 if mode == 'success' else 0 if mode == 'artifact_refusal' else 1) * (2 if repository_kind != 'git' else 1)
            finally:
                try:
                    await runtime.close_async()
                finally:
                    await host.close()
            assert runtime._repository_runtime.retained_image_bytes() == 0
            assert all(opened._image.closed for opened in images)
            assert image._closed and image._image.result().closed
    try:
        asyncio.run(exercise())
    finally:
        server.shutdown(); server.server_close(); server_thread.join(2)
    assert not server_thread.is_alive()
    if mode != 'artifact_refusal':
        assert_joined(children)
