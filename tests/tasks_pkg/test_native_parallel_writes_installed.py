"""Sealed native scheduler starts every write of a round at once.

Owner decision 2026-10-28 (docs/contracts/TOOL_EXECUTION_POLICY.md, "Native
application rounds"): the real path is native scheduler -> stdio host ->
NativeGateway default application approval -> canonical pipeline. N slow
writes to different files cost about one write; writes to one file keep call
order; results replay in provider order.
"""
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
from lib.agent_core.native_process import Budget
from lib.observability import InstrumentedThreadPoolExecutor
from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tasks_pkg.native_gateway import NativeGateway, compile_native_tool_catalog
from lib.tasks_pkg.native_host import NativeHost
from lib.tasks_pkg.tool_dispatch._approval import consume_approval_receipt
from scripts.build_native_runtime import verify
from tests.support.native_process_probe import config
from tests.tasks_pkg.test_native_gateway import principal, task

pytestmark = pytest.mark.api

_SECONDS = 0.4


def _provider(calls):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            declarations = calls if len(seen) == 1 else []
            delta = ({'tool_calls': [dict(call, index=i) for i, call in enumerate(declarations)]}
                     if declarations else {'content': 'written'})
            wire = ('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason':
                'tool_calls' if declarations else 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    return server, thread, seen


@pytest.mark.parametrize('count', [4, 8])
@pytest.mark.parametrize('target', ['distinct', 'same'])
def test_installed_writes_start_together_unless_they_share_a_file(tmp_path, count, target, record_property):
    source = os.environ.get('TOFU_NATIVE_WORKER_TEST_ARCHIVE')
    if not source:
        pytest.skip('explicit verified native worker archive required')
    archive = Path(source)
    manifest = verify(archive)
    owner = task()
    definitions = [
        {'type': 'function', 'function': {'name': 'fetch_url', 'parameters': {'type': 'object'}}},
        {'type': 'function', 'function': {'name': 'write_file', 'parameters': {'type': 'object',
            'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
            'required': ['path', 'content']}}},
    ]
    owner['_executable_tool_catalog'] = definitions
    lock = threading.Lock()
    spans, live, peak = [], 0, 0

    def write(task_, tc, name, call_id, args, rn, row, *_rest):
        nonlocal live, peak
        started = time.monotonic()
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(_SECONDS)
        consume_approval_receipt(task_, name, call_id, args)
        _finalize_tool_round(task_, rn, row, [{'title': 'write', 'content': 'written', 'badge': 'done'}])
        with lock:
            live -= 1
            spans.append((call_id, started, time.monotonic()))
        return call_id, 'written:' + call_id, False

    owner['_tool_env'] = SimpleNamespace(resolve=lambda name: write if name == 'write_file' else None,
        write_names={'write_file'}, idempotent_names=set(), confirmation_names=set())
    paths = [str(tmp_path / (f'file-{i}.txt' if target == 'distinct' else 'shared.txt')) for i in range(count)]
    calls = [{'id': f'write-{i}', 'type': 'function', 'function': {'name': 'write_file',
        'arguments': json.dumps({'path': paths[i], 'content': f'body {i}'})}} for i in range(count)]
    server, thread, seen = _provider(calls)

    async def run(executor):
        budget = Budget(1, 32 << 20, 1 << 20, 1 << 20, 3)
        image = NativeArchiveImage(archive=archive, sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'], budget=budget)
        host = NativeHost(image)
        gateway = NativeGateway(principal=principal(), task=owner, executor=executor,
            configuration={'autoApply': True}, tool_list=definitions,
            permissions={'fetch_url': 'read', 'write_file': 'write'}, project_path=str(tmp_path),
            project_enabled=True, model='fixture', round_source=lambda: 0, decision_policy=None,
            pending_calls=16, pending_bytes=1 << 20)
        # The composition run_native_task selects for the application.
        gateway.select_concurrent_dispatch()

        class Rounds:
            async def reserve(self, _request):
                return True

            async def settle(self, result):
                if result.evidence.get('kind') == 'returned':
                    gateway.prepare_response(result)

        class Terminal:
            async def persist(self, _request):
                return 'accepted'

        settings = config(f'http://127.0.0.1:{server.server_port}/chat', owner['id'])
        settings.update(owner_user_id=1, tools=compile_native_tool_catalog(owner, definitions),
            tool_calls=max(8, count), approval_control={'receipts': 16, 'identity_bytes': 1 << 20},
            concurrent_tool_dispatch=True, tool_sibling_control=True, tool_result_join=True,
            deadline_ms=60000,
            terminal_persistence=dict(operation_id='commit-writes', ack_ms=3000, attempts=1, frame_bytes=65536))
        try:
            async with host.client(principal(), budget=budget) as client:
                return await client.run(settings, callbacks=gateway.callbacks(), working_directory=tmp_path,
                    round_policy=Rounds(), terminal_policy=Terminal(), approval_policy=gateway)
        finally:
            gateway.close()
            await host.close()

    try:
        with InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='tool',
                thread_name_prefix='native-writes-installed') as executor:
            result = asyncio.run(run(executor))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result['status'] == 'done', result
    replay = [row['tool_call_id'] for row in seen[1]['messages'] if row['role'] == 'tool']
    assert replay == [call['id'] for call in calls]
    assert len(spans) == count
    work = max(end for _, _, end in spans) - min(start for _, start, _ in spans)
    record_property('tool_work_seconds', round(work, 3))
    record_property('peak_active_writes', peak)
    if target == 'distinct':
        assert peak == count, f'{count} writes to different files peaked at {peak}'
        assert work < _SECONDS * count * 0.6, (
            f'{count} writes of {_SECONDS}s took {work:.2f}s; serial would be {_SECONDS * count:.1f}s')
    else:
        ordered = sorted(spans, key=lambda span: span[1])
        assert peak == 1 and [span[0] for span in ordered] == [call['id'] for call in calls]
        assert all(later[1] >= earlier[2] for earlier, later in zip(ordered, ordered[1:]))
    assert not owner.get('_activeToolContexts') and '_pending_tool_result_envelopes' not in owner
