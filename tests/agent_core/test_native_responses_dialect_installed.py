"""Installed Rust request dialect follows the owned route and shared account."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tarfile
import threading

import pytest
from lib.agent_core.execution_budget import ExecutionBudget
from lib.agent_core.native_process import Budget, NativeProcessClient
from lib.model_routing.native_route import compile_native_route, run_native_route
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.model_routing.test_native_route import Repository, principal, selection
from tests.support.native_process_probe import config

pytestmark = pytest.mark.api


@pytest.mark.parametrize('profile', ['compatible', 'openai', 'codex'])
def test_installed_responses_dialect_uses_owned_route_and_settles_existing_budget(tmp_path, profile):
    archive = os.environ.get('TOFU_NATIVE_RESPONSES_DIALECT_TEST_ARCHIVE')
    if not archive:
        pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    manifest = verify(Path(archive), run=True)
    installed = tmp_path / 'native'
    installed.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(installed, filter='data')
    binary = installed / 'bin/tofu-native'
    binary.chmod(0o755)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path, self.headers['Authorization'], body))
            wire = ('data: ' + json.dumps({'type': 'response.completed', 'response': {
                'output': [{'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': 'done'}]}],
                'usage': {'input_tokens': 3, 'output_tokens': 2}}}) + '\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    task = dict(id='sdk-task', _userId=7, status='running', abort_event=threading.Event(),
                config={'executionLimits': dict(max_tokens=1000, max_tool_calls=None, deadline=None)})
    account = ExecutionBudget(task)
    task['_executionBudget'] = account
    repo = Repository()
    repo.document['connections'][0].update(protocol='openai_responses', responses_profile=profile,
        base_url=f'http://127.0.0.1:{server.server_port}/v1')
    route = compile_native_route(repo, repo.boundary, selection())
    configuration = config('unused', 'sdk-task')
    for key in ('endpoint', 'headers', 'template'):
        configuration.pop(key)
    configuration.update(version='tofu.native-agent-stdio/v1', type='configure', task_id=task['id'],
        tools={}, model_control={'ack_ms': 2000}, model_counter=dict(encoding='cl100k_base',
        output_field='max_tokens', input_bytes=65536, text_bytes=65536, tool_json_bytes=65536,
        depth=128, worker_stack_bytes=1048576))

    async def run(executor):
        model = NativeModelAccountPolicy(principal=principal(), task=task, executor=executor)
        client = NativeProcessClient(binary=binary, sha256=manifest['files']['bin/tofu-native']['sha256'],
            protocol_version=manifest['build']['stdio_contract'], principal=principal(),
            budget=Budget(1, 16*1024*1024, 65536, 65536, 5))
        try:
            result = await run_native_route(client, route, configuration,
                prepare_template=lambda metadata: dict(model=metadata['wire_model'], max_tokens=100,
                    temperature=.4, top_p=.8), callbacks={}, working_directory=tmp_path, model_policy=model)
            assert result.event['status'] == 'done'
            assert result.route['responses_profile'] == profile
            assert client.active_runs == 0
        finally:
            await client.close()
            route.close()

    thread.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            asyncio.run(run(executor))
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert len(seen) == 1
    path, authorization, body = seen[0]
    assert path == '/v1/responses' and authorization == 'Bearer private-fixture-token'
    assert body['model'] == 'alpha-wire-a' and body['store'] is False
    if profile == 'codex':
        assert body['instructions'] == '' and body['reasoning']['summary'] == 'auto'
        assert all(field not in body for field in ('temperature', 'top_p', 'max_output_tokens'))
    else:
        assert body['max_output_tokens'] == 100 and body['temperature'] == .4
    assert account.consumption_snapshot() == (5, 0, True)
    assert account.continuity_snapshot()['pending_model_requests'] == 0
    assert len(repo.secret_calls) == 1
