"""Normal pinned Granian CLI lifecycle with the actual Headless ASGI app.

No private socket adapter, transport monkeypatch or performance claim. Owned
process group, logs, TLS artifacts and normal upstream worker shutdown are joined.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class CliCleanupError(RuntimeError):
    """A failed owned teardown must stop admission of further stress trials."""


def application_factory():
    from benchmarks.native_http import application
    return application(lifecycle=True)


def compute_application_factory():
    from benchmarks.native_http import application
    return application(lifecycle=False)


def check(protocol, directory, *, operation=None, application_target='benchmarks.native_http_cli:application_factory'):
    import httpx
    import psutil
    from benchmarks.http_stream_lifecycle import check as stream_check, check_h2_reset
    certfile = keyfile = ''
    if protocol == 'h2':
        from lib.server.server_tls import ensure_tls_certificates
        certfile, keyfile = ensure_tls_certificates(
            data_root=str(directory), bind_host='127.0.0.1', environ={})
    # The normal CLI owns binding; this bounded ephemeral-port handoff can
    # race another binder. A bind failure is reported, never silently retried.
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    origin = f'{"https" if protocol == "h2" else "http"}://127.0.0.1:{port}'
    command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), '-m', 'granian',
               application_target, '--factory',
               '--interface', 'asgi', '--host', '127.0.0.1', '--port', str(port),
               '--workers', '1', '--loop', 'asyncio', '--runtime-threads', '1',
               '--task-impl', 'asyncio', '--no-access-log', '--log-level', 'error',
               '--workers-kill-timeout', '5']
    if certfile:
        command.extend(['--ssl-certificate', certfile, '--ssl-keyfile', keyfile])
    log = directory/f'granian-cli-{protocol}.log'
    with log.open('wb') as output:
        process = subprocess.Popen(command, cwd=ROOT, stdout=output,
                                   stderr=output, start_new_session=True)
    children = []; result = None
    try:
        deadline = time.monotonic()+60
        with httpx.Client(verify=certfile or True, trust_env=False, timeout=1) as probe:
            while True:
                if process.poll() is not None:
                    raise RuntimeError('normal CLI exited before ready: '+log.read_text()[-2000:])
                try:
                    if probe.get(origin+'/health/ready').status_code == 200:
                        break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('normal CLI readiness deadline')
                    time.sleep(.05)
        children = psutil.Process(process.pid).children(recursive=True)
        assert children, 'normal CLI did not start a worker process'
        if operation is None:
            result = asyncio.run(stream_check(origin, protocol, certfile or True))
            if protocol == 'h2':
                result['explicit_reset'] = asyncio.run(check_h2_reset(origin, certfile))
        else:
            result = operation(process, children, origin, certfile or True)
        result.update(cli_pid=process.pid, child_pids=[child.pid for child in children],
                      command=command, protocol=protocol)
    except BaseException:
        print(log.read_text()[-2000:], file=sys.stderr)
        raise
    finally:
        primary = sys.exc_info()[1]
        try:
            if process.poll() is None:
                # Capture even children from a failed startup. The dedicated
                # group contains only this experiment's CLI and workers.
                try:
                    children = psutil.Process(process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    pass  # CLI exited between poll and descendant observation.
                process.terminate()
            forced = False
            try:
                code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                code = process.wait(timeout=5)
                forced = True
            _, alive = psutil.wait_procs(children, timeout=5)
            if alive:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Tracked children exited between observation and kill.
                psutil.wait_procs(alive, timeout=5)
                raise CliCleanupError('normal CLI left worker descendants after shutdown')
            if forced:
                raise CliCleanupError('normal CLI failed graceful shutdown; owned group killed and joined')
            if result is not None:
                result.update(shutdown_returncode=code, descendants_joined=True)
                assert code == 0, 'normal CLI shutdown was not successful'
        except Exception as cleanup:
            if primary is None:
                raise
            primary.add_note('normal CLI cleanup also failed: '+type(cleanup).__name__)
            print('normal CLI cleanup also failed: '+repr(cleanup), file=sys.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--protocol', choices=('h1', 'h2'))
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--logs-dir', type=Path)
    args = parser.parse_args()
    assert args.repetitions > 0
    from granian.server import Server
    installed = Path(inspect.getfile(Server))
    names = ('benchmarks/native_http_cli.py', 'benchmarks/native_http.py',
             'benchmarks/http_stream_lifecycle.py', 'tofu_agent/server.py')
    report = {'scope': __doc__, 'whole_system_verdict': 'incomplete',
              'python': sys.version, 'granian': importlib.metadata.version('granian'),
              'installed_server_sha256': hashlib.sha256(installed.read_bytes()).hexdigest(),
              'sources': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names},
              'checks': [], 'failures': [], 'limitations': ['one normal CLI worker; no throughput/cold/RSS admission',
                                           'actual Headless app, not full durable Tofu app',
                                           'ephemeral port handoff races report failure without retry']}
    with tempfile.TemporaryDirectory(prefix='tofu-normal-cli-') as folder:
        root = args.logs_dir or Path(folder)
        root.mkdir(parents=True, exist_ok=True)
        for protocol in ((args.protocol,) if args.protocol else ('h1', 'h2')):
            for repetition in range(args.repetitions):
                trial_root = root/f'{protocol}-{repetition}'
                trial_root.mkdir(exist_ok=True)
                try:
                    result = check(protocol, trial_root)
                    result['repetition'] = repetition;report['checks'].append(result)
                    print(protocol, repetition, 'normal CLI lifecycle and joined worker shutdown pass', flush=True)
                except Exception as error:
                    report['failures'].append({'protocol': protocol, 'repetition': repetition,
                        'type': type(error).__module__+'.'+type(error).__name__, 'message': str(error),
                        'notes': getattr(error, '__notes__', []), 'logs': str(trial_root)})
                    print(protocol, repetition, 'failed', type(error).__name__, flush=True)
                    if isinstance(error, CliCleanupError) or any('cleanup' in note for note in getattr(error, '__notes__', [])):
                        args.output.write_text(json.dumps(report, indent=2)+'\n')
                        raise  # Do not start another trial after unresolved teardown.
                args.output.write_text(json.dumps(report, indent=2)+'\n')
    if report['failures']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
