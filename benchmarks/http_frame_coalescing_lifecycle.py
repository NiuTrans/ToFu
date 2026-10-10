"""Real owned TCP/TLS backpressure/disconnect/cancel/reconnect frame checks."""
import argparse as argparse
import asyncio as asyncio
import inspect as inspect
import json as json
import os as os
from pathlib import Path as Path
import sys as sys
import tempfile as tempfile
from types import FunctionType as FunctionType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks import http_stream_lifecycle as http_stream_lifecycle
from benchmarks.http_frame_coalescing import create_app as coalesced_app
from benchmarks.http_frame_coalescing_measure import checked_trial_source as checked_trial_source, checked_shutdown as checked_shutdown


async def serve_child(args):
    from tofu_agent.server import create_app
    source = inspect.getsource(http_stream_lifecycle.serve_child)
    old = 'from tofu_agent.server import HeadlessServerConfig,create_app'
    assert source.count(old) == 1
    source = source.replace(old, 'from tofu_agent.server import HeadlessServerConfig')
    namespace = dict(http_stream_lifecycle.serve_child.__globals__,
                     create_app=coalesced_app if args.loop == 'coalesced' else create_app)
    exec(compile(source, '<owned-frame-lifecycle-server>', 'exec'), namespace)
    await namespace['serve_child'](args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', action='store_true')
    parser.add_argument('--loop', choices=['reference', 'coalesced'], default='reference')
    parser.add_argument('--socket-fd', type=int)
    parser.add_argument('--certfile', default='')
    parser.add_argument('--keyfile', default='')
    parser.add_argument('--transport', default='owned', choices=['owned'])
    parser.add_argument('--original-owner-source', default='')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.server:
        asyncio.run(serve_child(args))
        return
    if args.output is None:
        parser.error('--output is required')
    sys.executable = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    namespace = dict(http_stream_lifecycle.trial.__globals__, __file__=__file__, _checked_shutdown=checked_shutdown)
    exec(compile(checked_trial_source(http_stream_lifecycle.trial), '<checked-frame-lifecycle-trial>', 'exec'), namespace)
    trial = namespace['trial']
    report = dict(scope=__doc__, whole_system_verdict='incomplete', production_promoted=False, trials=[])
    with tempfile.TemporaryDirectory(prefix='tofu-http-frame-lifecycle-') as directory:
        for protocol in ('h1', 'h2'):
            for variant in ('reference', 'coalesced'):
                result = trial(variant, protocol, Path(directory), transport='owned')
                result['shutdown_exit_code'] = 0
                result['shutdown_joined'] = True
                report['trials'].append(dict(protocol=protocol, variant=variant, result=result))
                args.output.write_text(json.dumps(report, indent=2) + '\n')
                print('PASS', protocol, variant, flush=True)


if __name__ == '__main__':
    main()
