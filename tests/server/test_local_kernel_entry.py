"""Launch the local kernel the way the Android app does, against the real server.

The CLI reads the per-install secret from stdin and reports the bound port as
one JSON line; the in-process API returns it. Both boot the complete server
with a private data directory and an explicit, minimal environment.
"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from tests._jsdom import require_shipped_graph

pytestmark = [pytest.mark.api, pytest.mark.slow, pytest.mark.timeout(900)]

ROOT = Path(__file__).resolve().parents[2]
_SECRET = 'tofu_admin_' + '89abcdef01234567' * 4
_WRONG_SECRET = 'tofu_admin_' + 'e' * 64
_READY_SECONDS = 600


def _environment(tmp_path: Path) -> dict[str, str]:
    keep = ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'USER', 'LOGNAME')
    env = {key: os.environ[key] for key in keep if key in os.environ}
    home = tmp_path / 'home'
    home.mkdir()
    env.update(HOME=str(home), PYTHONPATH=str(ROOT), PYTHONUNBUFFERED='1',
               PYTHONNOUSERSITE='1')
    return env


def _request(port: int, path: str, *, method='GET', headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
    try:
        connection.request(method, path, headers={
            'Host': f'127.0.0.1:{port}', **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _wait_ready(port: int) -> None:
    deadline = time.monotonic() + _READY_SECONDS
    status = None
    while time.monotonic() < deadline:
        status, _ = _request(port, '/api/v4/meta')
        if status == 200:
            return
        time.sleep(0.5)
    raise AssertionError(f'/api/v4/meta never answered 200 (last {status})')


def _assert_default_deny(port: int) -> None:
    keys = '/api/v4/keys'
    assert _request(port, keys)[0] == 401
    assert _request(port, '/')[0] == 401
    assert _request(port, keys, headers={
        'Authorization': f'Bearer {_WRONG_SECRET}'})[0] == 401
    for header, value in (('Authorization', f'Bearer {_SECRET}'),
                          ('x-api-key', _SECRET),
                          ('Cookie', f'tofu_session={_SECRET}')):
        assert _request(port, keys, headers={header: value})[0] == 200, header
    assert _request(port, '/', headers={'Cookie': f'tofu_session={_SECRET}'})[0] == 200
    assert _request(port, '/api/v4/meta')[0] == 200
    assert _request(port, '/api/v4/meta', headers={'Host': 'rebound.example'})[0] == 403
    assert _request(port, '/api/v4/update/check', headers={
        'Authorization': f'Bearer {_SECRET}'})[0] == 409


def _process_tree(pid: int) -> list[int]:
    tree, pending = [], [pid]
    while pending:
        current = pending.pop()
        tree.append(current)
        try:
            tasks = list(Path(f'/proc/{current}/task').iterdir())
        except (FileNotFoundError, ProcessLookupError):
            if current == pid:
                raise
            continue
        for task in tasks:
            try:
                children = (task / 'children').read_text().split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            pending.extend(int(child) for child in children)
    return tree


def _socket_inodes(pid: int) -> set[str]:
    inodes = set()
    for fd in Path(f'/proc/{pid}/fd').iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith('socket:['):
            inodes.add(target[len('socket:['):-1])
    return inodes


def _is_loopback(hex_address: str) -> bool:
    if len(hex_address) == 8:
        return hex_address.endswith('7F')
    return hex_address in ('00000000000000000000000001000000',
                           '0000000000000000FFFF00000100007F')


def _assert_no_listener_off_loopback(pid: int) -> None:
    """No TCP listener off loopback and no UDP socket on the LAN port 15001.

    A short-lived child, such as the environment probe that runs after
    readiness, can exit between enumeration and inspection. A child that is
    gone holds no socket; the kernel process itself must stay inspectable.
    """
    for member in _process_tree(pid):
        try:
            inodes = _socket_inodes(member)
            tables = [(table, Path(f'/proc/{member}/net/{table}').read_text())
                      for table in ('tcp', 'tcp6', 'udp', 'udp6')]
        except (FileNotFoundError, ProcessLookupError):
            if member == pid:
                raise
            continue
        for table, text in tables:
            for line in text.splitlines()[1:]:
                fields = line.split()
                address, port_hex = fields[1].split(':')
                if fields[9] not in inodes:
                    continue
                if table.startswith('tcp') and fields[3] == '0A':
                    assert _is_loopback(address), (member, table, fields[1])
                if table.startswith('udp'):
                    assert int(port_hex, 16) != 15001, (member, table, fields[1])


def _assert_secret_never_left_memory(pid: int, tmp_path: Path) -> None:
    for member in _process_tree(pid):
        for name in ('cmdline', 'environ'):
            try:
                content = Path(f'/proc/{member}/{name}').read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                if member == pid:
                    raise
                continue
            assert _SECRET.encode() not in content
    for path in tmp_path.rglob('*'):
        if path.is_file() and not path.is_symlink():
            assert _SECRET.encode() not in path.read_bytes(), path


def _stderr_tail(path: Path) -> str:
    return path.read_text(errors='replace')[-4000:] if path.exists() else ''


def test_cli_reads_the_secret_from_stdin_and_reports_its_loopback_port(tmp_path):
    require_shipped_graph()
    stderr = tmp_path / 'stderr.log'
    with stderr.open('w') as stderr_file:
        process = subprocess.Popen(
            [sys.executable, 'local_kernel.py', '--data-dir', str(tmp_path / 'data'),
             '--port', '0'],
            cwd=ROOT, env=_environment(tmp_path), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=stderr_file, text=True)
        try:
            process.stdin.write(_SECRET + '\n')
            process.stdin.close()
            line = process.stdout.readline()
            assert line, _stderr_tail(stderr)
            record = json.loads(line)
            assert record['event'] == 'listening'
            assert (record['host'], record['pid']) == ('127.0.0.1', process.pid)
            port = record['port']
            assert port > 0

            _wait_ready(port)
            _assert_default_deny(port)
            _assert_no_listener_off_loopback(process.pid)
            _assert_secret_never_left_memory(process.pid, tmp_path)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            exit_code = process.wait(timeout=180)
    assert exit_code == 0, _stderr_tail(stderr)


def test_a_second_kernel_on_the_same_data_directory_exits_before_serving(tmp_path):
    require_shipped_graph()
    command = [sys.executable, 'local_kernel.py', '--data-dir', str(tmp_path / 'data')]
    first_log, second_log = tmp_path / 'first.log', tmp_path / 'second.log'
    with first_log.open('w') as first_err, second_log.open('w') as second_err:
        first = subprocess.Popen(
            command, cwd=ROOT, env=_environment(tmp_path), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=first_err, text=True)
        try:
            first.stdin.write(_SECRET + '\n')
            first.stdin.close()
            line = first.stdout.readline()
            assert line, _stderr_tail(first_log)
            port = json.loads(line)['port']

            (tmp_path / 'second').mkdir()
            second = subprocess.Popen(
                command, cwd=ROOT, env=_environment(tmp_path / 'second'),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=second_err,
                text=True)
            try:
                second.stdin.write(_SECRET + '\n')
                second.stdin.close()
                assert second.wait(timeout=120) == 2, _stderr_tail(second_log)
            finally:
                if second.poll() is None:
                    second.kill()
                    second.wait(timeout=60)
            assert second.stdout.read() == ''
            assert 'already serves this data directory' in _stderr_tail(second_log)

            _wait_ready(port)
        finally:
            if first.poll() is None:
                first.send_signal(signal.SIGTERM)
            exit_code = first.wait(timeout=180)
    assert exit_code == 0, _stderr_tail(first_log)


def test_in_process_api_returns_the_bound_port_and_stops_without_exiting(tmp_path):
    require_shipped_graph()
    driver = tmp_path / 'host_app.py'
    driver.write_text(textwrap.dedent('''
        import json, os, sys
        import local_kernel

        secret = sys.stdin.readline().strip()
        port = local_kernel.start(sys.argv[1], secret, port=0)
        print(json.dumps({'port': port,
                          'runtime': [os.environ['_TOFU_RUNTIME_HOST'],
                                      os.environ['_TOFU_RUNTIME_PORT']],
                          'auth_mode': os.environ['TOFU_AUTH_MODE']}), flush=True)
        sys.stdin.readline()
        stopped = local_kernel.stop(timeout=180)
        try:
            local_kernel.start(sys.argv[1], secret, port=0)
            restart = 'started'
        except RuntimeError:
            restart = 'refused'
        print(json.dumps({'stopped': stopped, 'restart': restart}), flush=True)
    '''))
    stderr = tmp_path / 'stderr.log'
    with stderr.open('w') as stderr_file:
        process = subprocess.Popen(
            [sys.executable, str(driver), str(tmp_path / 'data')],
            cwd=ROOT, env=_environment(tmp_path), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=stderr_file, text=True)
        try:
            process.stdin.write(_SECRET + '\n')
            process.stdin.flush()
            line = process.stdout.readline()
            assert line, _stderr_tail(stderr)
            started = json.loads(line)
            port = started['port']
            assert started['runtime'] == ['127.0.0.1', str(port)]
            assert started['auth_mode'] == 'private'

            _wait_ready(port)
            _assert_default_deny(port)
            _assert_no_listener_off_loopback(process.pid)
            _assert_secret_never_left_memory(process.pid, tmp_path)

            process.stdin.write('stop\n')
            process.stdin.close()
            line = process.stdout.readline()
            assert line, _stderr_tail(stderr)
            assert json.loads(line) == {'stopped': True, 'restart': 'refused'}
            # The host process outlives stop() and then exits on its own.
            exit_code = process.wait(timeout=180)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=60)
    assert exit_code == 0, _stderr_tail(stderr)
