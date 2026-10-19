"""Real native Stop joins silent HTTP, counted settlement and canonical commit."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch

import pytest
from lib.agent_core.abort_signal import AbortSignal
from lib.agent_core.execution_budget import ExecutionBudget
from lib.agent_core.native_process import NativeProcessClient, Budget
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy
from tests.agent_core.test_native_task_terminal import principal
from tests.support.native_process_probe import config

pytestmark = pytest.mark.api

@pytest.mark.parametrize('worker_reuse', [False, True])
@pytest.mark.parametrize('already_stopped', [True, False])
def test_installed_stop_settles_and_persists_without_deadline_rewrite(tmp_path, already_stopped,worker_reuse):
    source = os.environ.get('TOFU_NATIVE_TASK_STOP_TEST_ARCHIVE')
    if not source:
        pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    archive = Path(source)
    manifest = verify(archive, run=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    entered, release = threading.Event(), threading.Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            entered.set()
            assert release.wait(10), 'test did not release silent peer'
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    signal = AbortSignal()
    task = {'id': 'stop-task', '_userId': 7, 'status': 'running', 'abort_event': signal,
            'config': {'executionLimits': {'max_tokens': 1000, 'max_tool_calls': None, 'deadline': None}}}
    account = ExecutionBudget(task)
    task['_executionBudget'] = account
    settings = config(f'http://127.0.0.1:{server.server_port}/chat', task['id'])
    settings['tools'] = {}
    settings['deadline_ms'] = 30000
    settings['deadline_unix_ms'] = 18446744073709551615
    settings['policy']['request_headers_ms'] = 20000
    settings['model_control'] = {'ack_ms': 2000}
    settings['model_counter'] = dict(encoding='cl100k_base', output_field='max_tokens',
        input_bytes=65536, text_bytes=65536, tool_json_bytes=65536,
        depth=128, worker_stack_bytes=1048576)
    settings['template']['max_tokens'] = 100
    settings['terminal_persistence'] = dict(operation_id='stop-commit', ack_ms=2000, attempts=2, frame_bytes=65536)
    projections = []
    def project(current, request):
        event = json.loads(request.event_json)
        projections.append(event)
        current['status'] = event['status']
    async def run(executor):
        client = NativeProcessClient.from_archive(archive=archive, sha256=digest,
            application_version=manifest['build']['application_version'], principal=principal(),
            budget=Budget(1, 16*1024*1024, 65536, 65536, 5))
        pool=shared=None
        if worker_reuse:
            from lib.agent_core.native_image import NativeArchiveImage
            from lib.agent_core.native_worker_pool import NativeWorkerPool
            shared=NativeArchiveImage(archive=archive,sha256=digest,application_version=manifest['build']['application_version'],budget=client.budget)
            pool=NativeWorkerPool(image=shared,idle_workers=2);client._worker_pool=pool
        model = NativeModelAccountPolicy(principal=principal(), task=task, executor=executor)
        terminal = NativeTaskTerminalPolicy(principal=principal(), task=task, executor=executor,
            operation_id='stop-commit', project=project, candidate_bytes=65536)
        if already_stopped: signal.set()
        try:
            with patch('lib.tasks_pkg.native_terminal._persist', return_value=True) as writer:
                invocation = asyncio.create_task(client.run(settings, callbacks={}, working_directory=tmp_path,
                    model_policy=model, terminal_policy=terminal, stop_signal=signal))
                if already_stopped:
                    result = await asyncio.wait_for(invocation, 10)
                    assert result['status'] == 'aborted'
                else:
                    assert await asyncio.to_thread(entered.wait, 10), 'native HTTP did not enter'
                    await asyncio.to_thread(signal.set)
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(invocation, 5)
                assert terminal.accepted
                assert writer.call_count == 1
                assert task['status'] == 'aborted'
                assert len(projections) == 1
                assert client.active_runs == 0
                assert signal._observers is None
                assert account.continuity_snapshot()['pending_model_requests'] == 0
                assert settings['deadline_unix_ms'] == 18446744073709551615
        finally:
            image = client._image.result() if client._image is not None else None
            await client.close()
            if pool is not None:await pool.close();await shared.close()
        assert image is not None and image.closed
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            asyncio.run(run(executor))
    finally:
        release.set()
        server.shutdown()
        thread.join(5)
        server.server_close()
    assert len(requests) == int(not already_stopped)
    assert account.consumption_snapshot()[2] is already_stopped
