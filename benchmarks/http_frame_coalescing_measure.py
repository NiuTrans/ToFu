"""Actual owned Hypercorn/Headless HTTP: original yields vs complete SSE frames.

Same asyncio, auth, TLS, JSON and deterministic compute in both arms. Synthetic
compute excludes full-agent/storage claims. CPU is an interval; RSS is a separate
post-workload snapshot, not peak/retained-memory admission.
"""
import argparse as argparse
import asyncio as asyncio
import hashlib as hashlib
import inspect as inspect
import json as json
import os as os
from pathlib import Path as Path
import platform as platform
import sys as sys
import tempfile as tempfile
from types import FunctionType as FunctionType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks import http_runtime as http_runtime
from benchmarks.http_frame_coalescing import create_app as coalesced_app


async def serve_child(args):
    from tofu_agent.server import create_app
    source = inspect.getsource(http_runtime.serve_child)
    old = 'from hypercorn.asyncio import serve'
    assert source.count(old) == 1
    source = source.replace(old, 'from lib.hypercorn_runtime import serve')
    old = 'from tofu_agent.server import HeadlessServerConfig, create_app'
    assert source.count(old) == 1
    source = source.replace(old, 'from tofu_agent.server import HeadlessServerConfig')
    namespace = dict(http_runtime.serve_child.__globals__,
                     create_app=coalesced_app if args.loop == 'coalesced' else create_app)
    exec(compile(source, '<owned-headless-frame-server>', 'exec'), namespace)
    await namespace['serve_child'](args)


def checked_trial_source(function):
    source = inspect.getsource(function)
    boundary = '    finally:\n        process.terminate()'
    assert source.count(boundary) == 1
    # The original body keeps its request accounting. Replace only its final
    # owner cleanup, preserving a primary request failure on cleanup failure.
    body = source[:source.index(boundary)]
    before_try = '    try:\n        deadline = ' if '    try:\n        deadline = ' in body else '    try:\n        deadline='
    assert body.count(before_try) == 1
    body = body.replace(before_try, '    primary_failure = None\n' + before_try)
    return body + ('    except BaseException as failure:\n'
                   '        primary_failure = failure\n        raise\n'
                   '    finally:\n        _checked_shutdown(process, primary_failure)\n')


def checked_shutdown(process, primary_failure):
    try:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError('HTTP fixture needed forced shutdown')
        if process.returncode != 0:
            raise RuntimeError(f'HTTP fixture exited with {process.returncode}')
    except BaseException as cleanup_failure:
        if primary_failure is None:
            raise
        primary_failure.add_note(f'HTTP fixture cleanup failed: {type(cleanup_failure).__name__}: {cleanup_failure}')


def trial(variant, protocol, case, samples, concurrency, directory, memory=False):
    namespace = dict(http_runtime.trial.__globals__, __file__=__file__, _checked_shutdown=checked_shutdown)
    exec(compile(checked_trial_source(http_runtime.trial), '<checked-owned-http-trial>', 'exec'), namespace)
    result = namespace['trial'](variant, protocol, case, samples, concurrency, directory, memory)
    result['shutdown_exit_code'] = 0
    result['shutdown_joined'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', action='store_true')
    parser.add_argument('--loop', choices=['reference', 'coalesced'], default='reference')
    parser.add_argument('--socket-fd', type=int)
    parser.add_argument('--certfile', default='')
    parser.add_argument('--keyfile', default='')
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--concurrency', type=int, default=16)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.server:
        asyncio.run(serve_child(args))
        return
    if args.samples < 1 or args.pairs < 1 or not 1 <= args.concurrency <= 256 or args.output is None:
        parser.error('require output, positive samples/pairs and concurrency 1..256')
    sys.executable = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    names = ('benchmarks/http_frame_coalescing.py', 'benchmarks/http_frame_coalescing_check.py',
             'benchmarks/http_frame_coalescing_measure.py', 'benchmarks/http_runtime.py',
             'tofu_agent/server.py', 'lib/hypercorn_runtime.py', 'lib/api_response.py')
    sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    report = dict(format='tofu.http-frame-coalescing/v1', whole_system_verdict='incomplete',
                  production_promoted=False, samples=args.samples, required_pairs=args.pairs,
                  scope=__doc__, sources=sources,
                  runtime=dict(python=sys.version, platform=platform.platform(), affinity=sorted(os.sched_getaffinity(0))),
                  workloads=[])
    with tempfile.TemporaryDirectory(prefix='tofu-http-frame-pairs-') as temporary:
        directory = Path(temporary)
        for protocol in ('h1', 'h2'):
            for case in ('short_requests', 'authentication', 'sse_fanout'):
                row = dict(protocol=protocol, case=case, pairs=[])
                report['workloads'].append(row)
                for pair in range(args.pairs):
                    measurements = {}
                    for variant in (('reference', 'coalesced') if pair % 2 == 0 else ('coalesced', 'reference')):
                        for name, digest in sources.items():
                            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
                        value = trial(variant, protocol, case, args.samples, args.concurrency, directory)
                        memory = trial(variant, protocol, case, args.samples, args.concurrency, directory, memory=True)
                        assert value['server_pid'] != memory['server_pid']
                        value['memory_server_pid'] = memory['server_pid']
                        value['rss_after_requests_bytes'] = memory['rss_after_requests_bytes']
                        measurements[variant] = value
                    for name, digest in sources.items():
                        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
                    row['pairs'].append(measurements)
                    args.output.write_text(json.dumps(report, indent=2) + '\n')
                    print(protocol, case, pair, {key: round(measurements['coalesced'][key] / measurements['reference'][key], 3)
                        for key in ('p50_ms', 'p99_ms', 'server_cpu_ms_per_request', 'rss_after_requests_bytes')}, flush=True)


if __name__ == '__main__':
    main()
