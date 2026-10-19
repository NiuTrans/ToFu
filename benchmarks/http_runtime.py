#!/usr/bin/env python3
"""Real Headless API/Hypercorn comparison: asyncio vs optional uvloop.

This measures the production HTTP/auth/SSE adapters with deterministic compute
ports. It excludes agent inference, persistence and full-app services; never
promote its result as whole-app or native-framework admission. Server processes,
sockets, certificates and client pools belong to one experiment and are cleaned
up on failure. Raw latency, server CPU and separate server RSS are retained.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TOKEN = 'synthetic-benchmark-token'


class ComputeFixture:
    """Bounded deterministic compute port, deliberately not an agent engine."""
    def __init__(self):
        from lib.identity import PrincipalContext
        self.principal = PrincipalContext.system(subject_id='benchmark.http', owner_user_id=900001,
                                                  scopes={'benchmark:http'})
        self.default_model = {'creator_id': 'openai', 'model_id': 'gpt-4o'}
        self.model_routing = None
        self.model_routing_source = 'synthetic-compute-port'
        self.capacity, self.in_flight, self.closed = 16, 0, False
        self.executions = OrderedDict()
        self.sequence = 0

    def start(self, request):
        self.sequence += 1
        result = ExecutionFixture(str(self.sequence), request)
        self.executions[result.task_id] = result
        while len(self.executions) > 256:
            self.executions.popitem(last=False)
        return result

    def get(self, task_id):
        return self.executions.get(task_id)


class ExecutionFixture:
    def __init__(self, task_id, request):
        self.task_id, self.request_id, self.request = task_id, task_id, request
        self.model, self.status, self.timeout_s = 'gpt-4o', 'done', 60.0

    async def events_async(self, *, cursor=0):
        for index in range(cursor, 8):
            await asyncio.sleep(.001)
            yield {'seq': index, 'type': 'delta', 'content': 'synthetic 中文'}
        yield {'seq': 8, 'type': 'done', 'finishReason': 'stop'}

    async def result_async(self, _timeout):
        return SimpleNamespace(to_dict=self.snapshot)

    def snapshot(self):
        return {'id': self.request_id, 'task_id': self.task_id, 'status': self.status,
                'model': self.model, 'content': 'synthetic 中文', 'finish_reason': 'stop'}

    def event_page(self, *, cursor=0):
        return {'events': [], 'next_cursor': cursor, 'cursor': cursor}

    def abort(self):
        self.status = 'aborted'
        return True


async def serve_child(args):
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    from tofu_agent.server import HeadlessServerConfig, create_app
    application = create_app(runtime=ComputeFixture(), model_routing_setup=object(),
                             config=HeadlessServerConfig(token=TOKEN, auth_mode='token', setup_enabled=False))
    config = Config()
    config.bind = [f'fd://{args.socket_fd}']
    config.accesslog = config.errorlog = None
    config.graceful_timeout = 2
    if args.certfile:
        config.certfile, config.keyfile = args.certfile, args.keyfile
    stopped = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
    await serve(application, config, shutdown_trigger=stopped.wait)


async def requests(origin, protocol, case, samples, concurrency, verify):
    import httpx
    headers = {'Authorization': f'Bearer {TOKEN}'}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    raw = []
    client_factory = lambda: httpx.AsyncClient(base_url=origin, http2=protocol == 'h2', verify=verify,
                                              trust_env=False, headers=headers, limits=limits, timeout=30)
    warmup_requests = 0
    async with AsyncExitStack() as pools:
        client = await pools.enter_async_context(client_factory())
        async def one(measure=True):
            started = time.perf_counter_ns()
            if case == 'sse_fanout':
                async with client.stream('POST', '/api/v1/agent/run', json={
                        'messages': [{'role': 'user', 'content': 'synthetic'}], 'stream': True}) as response:
                    assert response.status_code == 200
                    assert response.http_version == ('HTTP/2' if protocol == 'h2' else 'HTTP/1.1')
                    chunks = [json.loads(line[6:]) async for line in response.aiter_lines()
                              if line.startswith('data: ') and line != 'data: [DONE]']
                    assert [row['data']['seq'] for row in chunks] == list(range(9))
                    assert all(row.get('delta', {}).get('content') == 'synthetic 中文' for row in chunks[:8])
                    assert chunks[-1]['event'] == 'done'
            else:
                response = await client.get('/api/v1/capabilities', headers=(
                    {'Authorization': 'Bearer incorrect'} if case == 'authentication' else {}))
                assert response.status_code == (401 if case == 'authentication' else 200)
                assert response.http_version == ('HTTP/2' if protocol == 'h2' else 'HTTP/1.1')
                if case != 'authentication':
                    assert response.json()['runtime'] == 'tofu-agent'
            if measure:
                raw.append((time.perf_counter_ns() - started) / 1e6)
        await asyncio.gather(*(one(False) for _ in range(20)))
        warmup_requests += 20
        cohort_requests = 0
        for offset in range(0, samples, concurrency):
            if cohort_requests >= 256:
                await client.aclose()
                client = await pools.enter_async_context(client_factory())
                await asyncio.gather(*(one(False) for _ in range(20)))
                warmup_requests += 20
                cohort_requests = 0
            count = min(concurrency, samples - offset)
            await asyncio.gather(*(one() for _ in range(count)))
            cohort_requests += count
    return raw, warmup_requests


def trial(loop, protocol, case, samples, concurrency, directory, memory=False):
    import httpx
    import psutil
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    certfile = keyfile = ''
    if protocol == 'h2':
        from lib.server.server_tls import ensure_tls_certificates
        certfile, keyfile = ensure_tls_certificates(data_root=str(directory), bind_host='127.0.0.1', environ={})
    origin = f'{"https" if protocol == "h2" else "http"}://127.0.0.1:{port}'
    verify = certfile if certfile else True
    argv = [sys.executable, str(Path(__file__).resolve()), '--server', '--loop', loop,
            '--socket-fd', str(listener.fileno()), '--certfile', certfile, '--keyfile', keyfile]
    log = directory / f'{loop}-{protocol}-{case}-{time.time_ns()}.log'
    with log.open('wb') as output:
        process = subprocess.Popen(argv, pass_fds=(listener.fileno(),), stdout=output, stderr=output)
    listener.close()
    server = psutil.Process(process.pid)
    try:
        deadline = time.monotonic() + 60
        with httpx.Client(verify=verify, trust_env=False, timeout=1) as probe:
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f'Server exited: {log.read_text()[-2000:]}')
                try:
                    if probe.get(origin + '/health/ready').status_code == 200:
                        break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Owned server readiness expired')
                    time.sleep(.05)
        cpu_before = server.cpu_times()
        raw, warmup_requests = asyncio.run(requests(origin, protocol, case, samples, concurrency, verify))
        cpu_after = server.cpu_times()
        ordered = sorted(raw)
        return {'loop': loop, 'protocol': protocol, 'case': case, 'server_pid': process.pid,
                'raw_wall_ms': raw if not memory else [],
                'p50_ms': statistics.median(raw), 'p95_ms': ordered[int((samples - 1) * .95)],
                'p99_ms': ordered[int((samples - 1) * .99)],
                'server_cpu_ms_per_request': (cpu_after.user + cpu_after.system - cpu_before.user - cpu_before.system) * 1000 / (samples + warmup_requests),
                'warmup_requests': warmup_requests,
                'rss_after_requests_bytes': server.memory_info().rss,
                'compute_port': 'synthetic', 'correctness_passed': True}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError('Server failed graceful shutdown')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', action='store_true')
    parser.add_argument('--loop', choices=['asyncio', 'uvloop'], default='asyncio')
    parser.add_argument('--socket-fd', type=int)
    parser.add_argument('--certfile', default='')
    parser.add_argument('--keyfile', default='')
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--concurrency', type=int, default=16)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.server:
        if args.loop == 'uvloop':
            import uvloop
            with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
                runner.run(serve_child(args))
        else:
            asyncio.run(serve_child(args))
        return
    if args.samples < 100 or args.pairs < 3 or not 1 <= args.concurrency <= 256 or args.output is None:
        parser.error('require output, positive concurrency, 100 samples and three pairs')
    report = {'format': 'tofu.http-runtime-experiment/v1', 'whole_system_verdict': 'incomplete',
              'scope': 'production Headless API transport with synthetic compute; excludes full app/agent/storage',
              'connection_cohort': 'fresh client pool each 256 requests; server default keepalive limit retained',
              'runtime': {'python': sys.version, 'platform': platform.platform(),
                          'affinity': sorted(os.sched_getaffinity(0))},
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'workloads': []}
    with tempfile.TemporaryDirectory(prefix='tofu-http-runtime-') as temporary:
        directory = Path(temporary)
        for protocol in ('h1', 'h2'):
            for case in ('short_requests', 'authentication', 'sse_fanout'):
                row = {'protocol': protocol, 'case': case, 'pairs': []}
                for pair in range(args.pairs):
                    order = ('asyncio', 'uvloop') if pair % 2 == 0 else ('uvloop', 'asyncio')
                    measurements = {loop: trial(loop, protocol, case, args.samples, args.concurrency, directory)
                                    for loop in order}
                    for loop in order:
                        memory = trial(loop, protocol, case, 100, args.concurrency, directory, memory=True)
                        measurements[loop]['memory_server_pid'] = memory['server_pid']
                        measurements[loop]['rss_after_requests_bytes'] = memory['rss_after_requests_bytes']
                    row['pairs'].append(measurements)
                    ratios = {metric: measurements['uvloop'][metric] / measurements['asyncio'][metric]
                              for metric in ('p50_ms', 'p99_ms', 'server_cpu_ms_per_request', 'rss_after_requests_bytes')}
                    print(protocol, case, pair, json.dumps(ratios), flush=True)
                report['workloads'].append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    main()
