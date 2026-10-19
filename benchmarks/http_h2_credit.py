"""Witness HTTP/2 cancellation independently of connection receive credit.

Real Headless routes run on owned TLS servers. A raw h2 client exhausts both
receive windows, proves abort handling through headers plus a separate observer,
then grants only connection credit. SSE remains blocked while the abort body
completes. Reset, producer join and a subsequent request use that same socket.
This is a protocol/lifecycle diagnostic, never a performance or default gate.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import ssl
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def check(origin, verify):
    """Keep stream credit at zero while explicitly restoring connection credit."""
    import httpx
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, ResponseReceived, StreamEnded
    from benchmarks.http_runtime import TOKEN

    address = urlsplit(origin)
    tls = ssl.create_default_context(cafile=verify)
    tls.set_alpn_protocols(['h2'])
    reader, writer = await asyncio.wait_for(asyncio.open_connection(
        address.hostname, address.port, ssl=tls, server_hostname=address.hostname), 5)
    conn = H2Connection(config=H2Configuration(client_side=True, header_encoding='utf-8'))
    received = {1: 0, 3: 0, 5: 0}
    headers = {}; ended = set(); bodies = {3: bytearray(), 5: bytearray()}

    async def flush():
        writer.write(conn.data_to_send())
        await writer.drain()

    async def receive():
        data = await reader.read(65536)
        assert data, 'unexpected H2 connection EOF'
        for event in conn.receive_data(data):
            if isinstance(event, ResponseReceived):
                headers[event.stream_id] = dict(event.headers)
            elif isinstance(event, DataReceived):
                assert event.stream_id in received, 'unexpected response stream'
                received[event.stream_id] += event.flow_controlled_length
                if event.stream_id in bodies:
                    bodies[event.stream_id].extend(event.data)
                    assert len(bodies[event.stream_id]) <= 4096, 'control response exceeded credit budget'
            elif isinstance(event, StreamEnded):
                ended.add(event.stream_id)
        await flush()  # SETTINGS acknowledgement, never implicit DATA credit.

    def request(stream, method, path):
        conn.send_headers(stream, [(':method', method), (':scheme', 'https'),
            (':authority', address.netloc), (':path', path),
            ('authorization', f'Bearer {TOKEN}')], end_stream=True)

    try:
        async with asyncio.timeout(20), httpx.AsyncClient(base_url=origin,
                verify=verify, http2=True, trust_env=False,
                headers={'Authorization': f'Bearer {TOKEN}'}, timeout=5) as observer:
            assert writer.get_extra_info('ssl_object').selected_alpn_protocol() == 'h2'
            conn.initiate_connection()
            await flush()
            created = await observer.post('/api/v1/agent/run', json={'messages':[
                {'role':'user', 'content':'synthetic'}]}, headers={'Prefer':'respond-async'})
            assert created.status_code == 202
            task_id = created.json()['task_id']
            request(1, 'GET', f'/api/v1/tasks/{task_id}/stream')
            await flush()
            while received[1] < 65535:
                await receive()
            assert headers[1][':status'] == '200'
            assert received[1] == 65535 and conn.inbound_flow_control_window == 0
            held = (await observer.get(f'/benchmark/stream/{task_id}')).json()
            assert held['active'] == 1 and 0 < held['produced'] < 512, held
            assert held['status'] == 'running', held

            request(3, 'POST', f'/api/v1/tasks/{task_id}/abort')
            await flush()
            while 3 not in headers:
                await receive()
            assert headers[3][':status'] == '200'
            aborted = (await observer.get(f'/benchmark/stream/{task_id}')).json()
            assert aborted['status'] == 'aborted', 'abort was not handled: '+str(aborted)
            assert received[3] == 0 and 3 not in ended
            starved = dict(connection_window=conn.inbound_flow_control_window,
                           sse_bytes=received[1], abort_bytes=received[3], abort_status=200)

            # RFC 9113 §5.2: connection and stream credits are independent.
            # No SSE data is acknowledged and no stream WINDOW_UPDATE is sent.
            conn.increment_flow_control_window(4096)
            await flush()
            while 3 not in ended:
                await receive()
            abort_body = json.loads(bodies[3])
            assert abort_body['abort_requested'] is True
            assert abort_body['task_id'] == task_id
            assert received[1] == 65535, 'paused SSE received additional credit'

            conn.reset_stream(1, error_code=8)
            await flush()
            request(5, 'GET', '/api/v1/capabilities')
            await flush()
            while 5 not in ended:
                await receive()
            assert headers[5][':status'] == '200'
            assert json.loads(bodies[5])['runtime'] == 'tofu-agent'
            async with asyncio.timeout(2):
                while True:
                    final = (await observer.get(f'/benchmark/stream/{task_id}')).json()
                    if final['active'] == 0:
                        break
                    await asyncio.sleep(.01)
            assert final['closed'] == 1 and final['status'] == 'aborted', final
            return dict(held=held, aborted_before_credit=aborted, starved=starved,
                final=final, flow_bytes=received, connection_credit_released=4096,
                stream_credit_released=0, abort_body=abort_body, connection_reused=True)
    finally:
        primary = sys.exc_info()[1]
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 2)
        except Exception as cleanup:
            if primary is None:
                raise
            primary.add_note('H2 diagnostic socket cleanup failed: '+repr(cleanup))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--logs-dir', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=3)
    args = parser.parse_args()
    if args.pairs <= 0:
        raise ValueError('pairs must be positive')
    from benchmarks.native_http import trial
    from benchmarks.native_http_cli import check as cli_check
    import granian._granian
    names = ('benchmarks/http_h2_credit.py', 'benchmarks/native_http.py',
        'benchmarks/native_http_cli.py', 'benchmarks/http_stream_lifecycle.py',
        'benchmarks/http_runtime.py', 'tofu_agent/server.py', 'lib/hypercorn_runtime.py')
    sources = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    report = dict(scope=__doc__, source_commit=subprocess.check_output(
        ['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(), sources=sources,
        python=sys.version, platform=platform.platform(), versions={
            name:importlib.metadata.version(name) for name in ('granian','hypercorn','h2','httpx','httpcore')},
        granian_binary_sha256=hashlib.sha256(Path(granian._granian.__file__).read_bytes()).hexdigest(),
        rows=[], failures=[], performance_admitted=False, default_selection_admitted=False,
        limits=['Synthetic compute; actual Headless auth/routes, no durable model task',
            'Does not retroactively observe flow windows in the historical HTTPX timeout',
            'No latency, CPU, memory or whole-system qualification'])
    args.logs_dir.mkdir(parents=True, exist_ok=True)
    def operation(_process, _children, origin, verify):
        return asyncio.run(check(origin, verify))
    for pair in range(args.pairs):
        for backend in ('owned','granian') if pair % 2 == 0 else ('granian','owned'):
            directory = args.logs_dir / f'{pair}-{backend}'
            directory.mkdir(exist_ok=False)
            try:
                assert all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in sources.items())
                if backend == 'granian':
                    row = cli_check('h2', directory, operation=operation)
                else:
                    row = trial('owned','h2','lifecycle',directory,operation=operation,
                                executable=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
                row.update(backend=backend, pair=pair)
                report['rows'].append(row)
                print(backend, pair, 'credit isolation and joined cleanup pass', flush=True)
            except BaseException as error:
                report['failures'].append(dict(backend=backend,pair=pair,type=type(error).__name__,
                    message=str(error),notes=getattr(error,'__notes__',[])))
                raise  # Never admit another process after a failed observation/cleanup.
            finally:
                args.output.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
