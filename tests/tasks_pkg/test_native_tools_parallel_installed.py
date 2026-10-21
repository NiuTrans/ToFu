"""Sealed native scheduler → stdio host → canonical gateway → provider replay."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.native_process import Budget, NativeToolError, ProgressLimits
from lib.observability import InstrumentedThreadPoolExecutor
from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tasks_pkg.native_gateway import NativeGateway, compile_native_tool_catalog
from lib.tasks_pkg.native_host import NativeHost
from scripts.build_native_runtime import verify
from tests.support.native_process_probe import config
from tests.tasks_pkg.test_native_gateway import principal, task

pytestmark = pytest.mark.api


@pytest.mark.parametrize('count,mode', [(2, 'legacy'), (8, 'v2'), (8, 'failure'), (8, 'cancel'),
    (8, 'startup_recovery'),
    (8, 'tool_recovery_legacy'), (8, 'tool_recovery_v2'),
    (8, 'tool_recovery_httpx_legacy'), (8, 'tool_recovery_httpx_v2'),
    (8, 'tool_recovery_requests_legacy'), (8, 'tool_recovery_requests_v2'),
    (8, 'tool_recovery_status_httpx_legacy'), (8, 'tool_recovery_status_httpx_v2'),
    (8, 'tool_recovery_status_requests_legacy'), (8, 'tool_recovery_status_requests_v2'),
    (2, 'measure_ordered'), (2, 'measure_parallel'), (8, 'measure_ordered'), (8, 'measure_parallel')])
def test_installed_parallel_tools_keep_exact_duplicate_results_and_join(tmp_path, count, mode, record_property, monkeypatch):
    source = os.environ.get('TOFU_NATIVE_WORKER_TEST_ARCHIVE')
    if not source:
        pytest.skip('explicit verified native worker archive required')
    archive = Path(source)
    manifest = verify(archive)
    starts, startup_faults = [], []
    original_start = threading.Thread.start
    def start(thread):
        if mode == 'startup_recovery' and thread.name.startswith('native-parallel-recovery'):
            starts.append(thread.name)
            if len(starts) == 2:
                startup_faults.append(thread.name)
                raise OSError('transient native callback worker refusal')
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    owner = task()
    recover_tool = mode.startswith('tool_recovery_')
    recover_status = '_status_' in mode
    status_requests = []
    status_code = 429 if '_requests_' in mode else 503
    expected_count = count + int(recover_tool)
    owner['config']['tools']['resultEnvelope'] = 'v2' if mode == 'v2' or mode.endswith('_v2') else 'legacy'
    definitions = [{'type': 'function', 'function': {'name': 'fetch_url', 'parameters': {'type': 'object'}}}]
    owner['_executable_tool_catalog'] = definitions
    if recover_tool:
        from lib.tools.contracts import ToolContractV2
        owner['_toolContractDocumentsByName'] = {'fetch_url': ToolContractV2(
            name='fetch_url', parameters={'type': 'object'}, model_description='Read a source.',
            permission='read', idempotency='read_only', effect_recovery='read_only').search_document()}
    entered, exited = [], []
    lock = threading.Lock()
    all_entered = threading.Event()
    releases = [threading.Event() for _ in range(count)]
    completed = [threading.Event() for _ in range(count)]
    peak = 0
    timing = []
    if mode.startswith('measure_'):
        for release in releases: release.set()
    calls = [{'id': f'original-{i}', 'type': 'function', 'function': {'name': 'fetch_url', 'arguments': '{}'}} for i in range(count)]
    retry = {'id': 'recovered-0', 'type': 'function', 'function': {
        'name': 'fetch_url', 'arguments': json.dumps({'url': 'https://example.test/recover'})}}
    if recover_tool:
        calls[0]['function']['arguments'] = retry['function']['arguments']
    def read(task, tc, name, call_id, args, rn, row, *_rest):
        nonlocal peak
        index = int(call_id.rsplit('-', 1)[1])
        with lock:
            entered.append(call_id)
            peak = max(peak, len(entered) - len(exited))
            if len(entered) == 1: timing.append(time.monotonic())
            if len(entered) == count: all_entered.set()
        try:
            assert releases[index].wait(10), 'test did not release its owned read'
            if mode.startswith('measure_'): time.sleep(.04)
            row['_cacheableResult'] = False  # Both lanes perform identical live work.
            source_result = None
            if recover_status and call_id in {'original-0', 'recovered-0'}:
                import httpx
                import requests
                client = httpx.Client(trust_env=False) if '_httpx_' in mode else requests.Session()
                with client:
                    if '_requests_' in mode:
                        client.trust_env = False
                    response = client.get(f'http://127.0.0.1:{server.server_port}/source', timeout=2)
                    response.raise_for_status()
                    source_result = response.text
            if recover_tool and call_id == 'original-0':
                if '_httpx_' in mode:
                    import httpx
                    raise httpx.RemoteProtocolError('source disconnected during read')
                if '_requests_' in mode:
                    import requests
                    raise requests.exceptions.ConnectionError('source disconnected during read')
                raise ConnectionResetError('source disconnected during read')
            result = source_result if source_result is not None else 'evidence:' + call_id
            _finalize_tool_round(task, rn, row, [{'content': result, 'badge': 'done'}])
            return call_id, result, False
        finally:
            with lock:
                exited.append(call_id)
                if len(exited) == count: timing.append(time.monotonic())
            completed[index].set()
    owner['_tool_env'] = SimpleNamespace(resolve=lambda name: read if name == 'fetch_url' else None)
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            status_requests.append(self.path)
            first = len(status_requests) == 1
            body = b'' if first else b'evidence:recovered-0'
            self.send_response(status_code if first else 200)
            if first:
                self.send_header('Retry-After', '0')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append(body)
            declarations = calls if len(seen) == 1 else [retry] if recover_tool and len(seen) == 2 else []
            delta = {'tool_calls': [dict(call, index=i) for i, call in enumerate(declarations)]} if declarations else {'content': 'complete'}
            wire = ('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': 'tool_calls' if declarations else 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False  # server_close joins provider and source handlers.
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    async def run(executor):
        budget = Budget(1, 32 << 20, 1 << 20, 1 << 20, 3)
        image = NativeArchiveImage(archive=archive, sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'], budget=budget)
        host = NativeHost(image)
        host.enable_worker_reuse({'root_maximum': 1})
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor, configuration=owner['config'],
            tool_list=definitions, permissions={'fetch_url': 'read'}, project_path=None, project_enabled=False,
            model='fixture', round_source=None, decision_policy=None, pending_calls=16, pending_bytes=65536)
        progress = gateway.enable_progress(ProgressLimits(2, 2), 8192)
        observations, terminals = [], []
        class Rounds:
            async def reserve(self, _request): return True
            async def settle(self, result):
                if result.evidence.get('kind') == 'returned': gateway.prepare_response(result)
        class Progress:
            limits = progress.limits
            async def observe(self, request):
                result = await progress.observe(request)
                observations.append(result)
                return result
        class Terminal:
            async def persist(self, request):
                assert len(exited) == expected_count
                assert request.receipt['invariants_satisfied']
                terminals.append(request)
                return 'accepted'
        async def callback(request):
            assert request.model_call_id in {call['id'] for call in calls} | ({retry['id']} if recover_tool else set())
            result = await gateway.execute(request)
            if mode == 'failure' and request.model_call_id == 'original-0':
                raise ValueError('injected host failure after joined tool settlement')
            return result
        settings = config(f'http://127.0.0.1:{server.server_port}/chat', owner['id'])
        settings['tool_calls'] = max(settings['tool_calls'], expected_count)
        settings.update(owner_user_id=1, tools=compile_native_tool_catalog(owner, definitions),
            parallel_tool_names=sorted(gateway.parallel_names), tool_sibling_control=True,
            tool_result_join=True, deadline_ms=15000,
            terminal_persistence=dict(operation_id='commit-parallel', ack_ms=3000, attempts=1, frame_bytes=65536))
        if mode == 'measure_ordered': settings.pop('parallel_tool_names')
        try:
            async with host.client(principal(), budget=budget) as client:
                work = asyncio.create_task(client.run(settings, callbacks={'fetch_url': callback}, working_directory=tmp_path,
                    round_policy=Rounds(), progress_policy=Progress(), terminal_policy=Terminal()))
                try:
                    assert await asyncio.to_thread(all_entered.wait, 5), 'independent tools remained serial'
                    if mode == 'cancel':
                        work.cancel()
                        await asyncio.sleep(.01)
                        work.cancel()
                        assert not work.done() and not terminals
                    # Force reverse completion of identical calls: IDs must not
                    # be inferred from callback arrival or result order.
                    for i in reversed(range(count)):
                        releases[i].set()
                        assert await asyncio.to_thread(completed[i].wait, 3)
                    if mode == 'failure':
                        with pytest.raises(NativeToolError, match='tool callback'):
                            await work
                    elif mode == 'cancel':
                        with pytest.raises(asyncio.CancelledError): await work
                    else:
                        assert (await work)['status'] == 'done'
                finally:
                    for release in releases: release.set()
                    if not work.done(): work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                assert not client._tool_callbacks and client.active_runs == 0
            assert len(terminals) == 1
            if mode in {'failure', 'cancel'}:
                assert not host._worker_pool.idle and not host._worker_pool.workers
            else:
                assert len(host._worker_pool.idle) == 1
                results = [row for row in seen[1]['messages'] if row['role'] == 'tool']
                assert [row['tool_call_id'] for row in results] == [call['id'] for call in calls]
                for row in results:
                    if recover_tool and row['tool_call_id'] == 'original-0':
                        from lib.tools.result_envelope import tool_result_error
                        error = tool_result_error(row['content'])
                        expected_code = ('tool_http_rate_limited' if status_code == 429 else
                            'tool_http_unavailable') if recover_status else 'tool_connection_failed'
                        assert error.code == expected_code and error.retryable
                        assert 'gateway' in error.next_action and 'independent' in error.next_action
                        if recover_status:
                            assert 'Retry-After' in error.next_action and '0 seconds' in error.next_action
                    else:
                        assert 'evidence:' + row['tool_call_id'] in row['content']
                if recover_tool:
                    assert len(seen) == 3
                    retry_results = [row for row in seen[2]['messages']
                        if row.get('role') == 'tool' and row.get('tool_call_id') == retry['id']]
                    assert len(retry_results) == 1 and 'evidence:recovered-0' in retry_results[0]['content']
                    assert {row['toolCallId']: row['status'] for row in owner['toolRounds']} == {
                        **{call['id']: 'done' for call in calls}, 'original-0': 'error', retry['id']: 'done'}
                assert observations[0]['resultEvidenceComplete'] and len(observations[0]['evidenceIds']) == count
        finally:
            gateway.close()
            await host.close()
        assert not host._worker_pool.workers and image._closed
    try:
        with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool',
                thread_name_prefix='native-parallel-recovery') as executor:
            asyncio.run(run(executor))
    finally:
        server.shutdown(); server.server_close(); thread.join()
    expected_ids = [f'original-{i}' for i in range(count)] + ([retry['id']] if recover_tool else [])
    assert sorted(entered) == sorted(exited) == sorted(expected_ids)
    assert peak == (1 if mode == 'measure_ordered' else count)
    record_property('tool_work_seconds', timing[1] - timing[0])
    record_property('peak_active_tools', peak)
    record_property('active_tools_after_join', len(entered) - len(exited))
    assert not owner.get('_activeToolContexts') and '_pending_tool_result_envelopes' not in owner
    assert len(startup_faults) == int(mode == 'startup_recovery')
    assert status_requests == (['/source', '/source'] if recover_status else [])
