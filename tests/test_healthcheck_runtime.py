#!/usr/bin/env python3
"""Guard tests: healthcheck.py --runtime mode + install.sh probe wiring.

Background — the "did my install actually work?" gap (2026-07):
  install.sh ended with `exec python server.py` and NEVER verified the boot.
  healthcheck.py was a dev-time source lint (syntax / imports / vendor files)
  that no installer ever ran and that checked nothing about a live server.
  A fresh user whose server failed to boot (port busy, DB unwritable) or came
  up with no LLM credential got no signal — just raw startup logs.

  The fix: healthcheck.py grows a `--runtime [--port N] [--wait SEC]` mode
  that probes a RUNNING server (/api/health → storage ready → index page →
  LLM credential → browser engine) and exits 0/1. The lifecycle-managed
  `python server.py` now returns after readiness, so install.sh starts it and
  then runs the probe in the foreground. This preserves ordering, exit status,
  and readable logs instead of orphaning a probe over server output.

  Behavioural tests spawn healthcheck.py as a subprocess against a fake
  in-process HTTP server; static guards pin the install.sh wiring so a later
  refactor can't silently drop the probe (NEUTER: delete the probe line or
  the --runtime branch and tests 5/6 go red).
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HC = os.path.join(ROOT, 'healthcheck.py')
INSTALL_SH = os.path.join(ROOT, 'install.sh')

_HEALTH_OK = {
    'ok': True,
    'version': '0.0.0-test',
    'bootId': 'abc123def456',
    'storage': {'ready': True, 'state': 'ready', 'backend': 'sqlite', 'pid': 42},
}


def _make_handler(health_payload):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/api/health':
                body = json.dumps(health_payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == '/':
                body = b'<html><head></head><body>tofu</body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    return _Handler


def _serve(health_payload=None, start_delay=0.0):
    """Start a fake tofu server on a free port. Returns (server, port)."""
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _make_handler(health_payload or _HEALTH_OK))

    def _run():
        if start_delay:
            time.sleep(start_delay)
        srv.serve_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _run_runtime(*args, timeout=90, env=None):
    return subprocess.run(
        [sys.executable, HC, '--runtime', *args],
        capture_output=True, text=True, timeout=timeout, cwd=ROOT, env=env)


def _install_prefix():
    """install.sh source up to the backend fork (no network/uv/conda run)."""
    source = open(INSTALL_SH, encoding='utf-8').read()
    return source[:source.index('#  Step 0.6: Choose install backend')]


def _fake_uname(bin_dir, arch):
    """A `uname` shim that reports Linux + the requested machine arch."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / 'uname'
    path.write_text(
        '#!/bin/bash\n'
        'case "$1" in\n'
        '  -s) echo Linux ;;\n'
        '  -m) echo %s ;;\n'
        '  *) /usr/bin/uname "$@" ;;\n'
        'esac\n' % arch)
    path.chmod(0o755)


# ── Behavioural (failing-first: pre-change, --runtime ran the dev lint and
#    never printed these lines) ────────────────────────────────────────

def test_runtime_healthy_server_passes():
    srv, port = _serve()
    try:
        r = _run_runtime('--port', str(port))
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'server reachable' in r.stdout
    assert 'storage sidecar ready' in r.stdout
    assert 'index page serves HTML' in r.stdout


def test_runtime_tls_preference_falls_back_to_verified_http_endpoint():
    """A stale TLS preference must not hide a healthy loopback worker."""
    srv, port = _serve()
    try:
        env = dict(os.environ, TOFU_TLS='1')
        result = _run_runtime('--port', str(port), env=env)
    finally:
        srv.shutdown()

    assert result.returncode == 0, result.stdout + result.stderr
    assert f'server reachable at http://127.0.0.1:{port}' in result.stdout


def test_runtime_dead_port_fails_fast():
    port = _free_port()  # nothing listens here
    r = _run_runtime('--port', str(port), '--wait', '0')
    assert r.returncode == 1
    assert 'not answering' in r.stdout
    assert '\x1b[' not in r.stdout


def test_runtime_wait_polls_until_server_boots():
    # Server only starts answering 0.5s in; --wait must ride over that.
    srv, port = _serve(start_delay=0.5)
    try:
        r = _run_runtime('--port', str(port), '--wait', '15')
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'server reachable' in r.stdout


def test_runtime_missing_storage_snapshot_fails_closed():
    payload = {key: value for key, value in _HEALTH_OK.items()
               if key != 'storage'}
    srv, port = _serve(health_payload=payload)
    try:
        r = _run_runtime('--port', str(port))
    finally:
        srv.shutdown()
    assert r.returncode == 1
    assert 'storage sidecar NOT ready' in r.stdout


def test_runtime_unhealthy_storage_fails():
    payload = dict(
        _HEALTH_OK,
        storage={
            'ready': False,
            'state': 'restarting',
            'backend': 'sqlite',
            'last_error': 'sidecar exited unexpectedly (137)',
        },
    )
    srv, port = _serve(health_payload=payload)
    try:
        r = _run_runtime('--port', str(port))
    finally:
        srv.shutdown()
    assert r.returncode == 1
    assert 'storage sidecar NOT ready' in r.stdout
    assert 'state=restarting' in r.stdout


def test_runtime_browser_is_optional_unless_explicitly_required(tmp_path):
    package = tmp_path / 'playwright'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'sync_api.py').write_text(
        'def sync_playwright():\n'
        '    raise RuntimeError("simulated browser launch failure")\n',
        encoding='utf-8')
    env = dict(os.environ)
    env['PYTHONPATH'] = str(tmp_path) + os.pathsep + env.get('PYTHONPATH', '')
    srv, port = _serve()
    try:
        optional = _run_runtime('--port', str(port), env=env)
        required = _run_runtime(
            '--port', str(port), '--require-browser', env=env)
    finally:
        srv.shutdown()

    assert optional.returncode == 0, optional.stdout + optional.stderr
    assert 'Chromium cannot launch' in optional.stdout
    assert 'server usable' in optional.stdout
    assert required.returncode == 1
    assert 'Chromium cannot launch' in required.stdout


# ── Static wiring guards (NEUTER: remove the wiring → these go red) ──

def test_install_sh_probes_after_managed_start_without_orphaning():
    src = open(INSTALL_SH, encoding='utf-8').read()
    start_at = src.find('if ! "$ENV_PYTHON" server.py; then')
    probe_at = src.find('if "$ENV_PYTHON" healthcheck.py --runtime')
    assert start_at != -1, 'install.sh lost managed startup'
    assert probe_at != -1, 'install.sh lost the post-install runtime probe'
    assert start_at < probe_at, 'managed startup must become ready before its probe runs'
    failure_at = src.find('required runtime validation failed', probe_at)
    exit_at = src.find('exit 1', failure_at)
    complete_at = src.find('Installation complete — Tofu is ready', probe_at)
    assert failure_at != -1 and exit_at != -1
    assert complete_at != -1 and probe_at < complete_at
    assert '--require-browser' in src[start_at:probe_at + 250]
    assert 'exec python server.py' not in src
    assert '( python healthcheck.py --runtime' not in src
    assert 'if ! python server.py; then' not in src
    assert 'if python healthcheck.py --runtime' not in src
    assert 'json.load(sys.stdin).get("applicationUrl")' in src
    assert 'Tofu is ready: http://localhost:${PORT}' not in src


def test_installer_protects_env_and_escapes_secret_replacements():
    src = open(INSTALL_SH, encoding='utf-8').read()
    assert 'chmod 600 "$ENV_FILE"' in src
    assert 'chmod 600 "$TOFU_INSTALL_LOG"' in src
    assert 'escaped_value="${escaped_value//\\\\/\\\\\\\\}"' in src
    assert 'escaped_value="${escaped_value//&/\\\\&}"' in src
    assert 'escaped_value="${escaped_value//|/\\\\|}"' in src


def test_help_entrypoints_are_fast_and_do_not_start_work(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), PORT='15997')
    commands = [
        ([sys.executable, HC, '--help'], 'audit a source checkout'),
        ([sys.executable, os.path.join(ROOT, 'bootstrap.py'), '--help'],
         'normal managed startup'),
        (['bash', INSTALL_SH, '--help'], 'without downloading or changing files'),
    ]
    for command, expected in commands:
        result = subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, text=True,
            timeout=5)
        assert result.returncode == 0, result.stdout + result.stderr
        assert expected in result.stdout
    assert not (tmp_path / 'tofu').exists()
    assert not (tmp_path / 'logs').exists()


def test_install_help_still_works_when_home_is_unset(tmp_path):
    env = dict(os.environ)
    env.pop('HOME', None)
    help_result = subprocess.run(
        ['bash', INSTALL_SH, '--help'], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=5)
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert 'Usage: install.sh [OPTIONS]' in help_result.stdout

    install_result = subprocess.run(
        ['bash', INSTALL_SH, '--no-launch'], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=5)
    assert install_result.returncode == 2
    assert 'HOME is not set' in install_result.stderr
    assert not (tmp_path / 'logs').exists()


def test_install_rejects_unsafe_key_files_before_side_effects(tmp_path):
    multiline = tmp_path / 'multiline.key'
    multiline.write_text('first-line\nsecond-line\n', encoding='utf-8')
    oversized = tmp_path / 'oversized.key'
    oversized.write_bytes(b'x' * 8193)

    for key_file, expected in (
            (multiline, 'must be one non-empty line'),
            (oversized, 'must be 8192 bytes or smaller')):
        result = subprocess.run(
            ['bash', INSTALL_SH, '--api-key-file', str(key_file)],
            cwd=tmp_path, env=dict(os.environ, HOME=str(tmp_path)),
            capture_output=True, text=True, timeout=5)
        assert result.returncode == 2
        assert expected in result.stderr
        assert 'first-line' not in result.stdout + result.stderr
        assert not (tmp_path / 'tofu').exists()
        assert not (tmp_path / 'logs').exists()


@pytest.mark.parametrize(
    ('secret', 'expected'),
    [
        (' leading-space', 'cannot start/end with whitespace or quotes'),
        ('trailing-space ', 'cannot start/end with whitespace or quotes'),
        ('"quoted-key"', 'cannot start/end with whitespace or quotes'),
        ('first-key,second-key', 'must contain exactly one key'),
    ],
)
def test_install_rejects_keys_that_cannot_round_trip_through_dotenv(
        tmp_path, secret, expected):
    key_file = tmp_path / 'non-roundtrip.key'
    key_file.write_text(secret + '\n', encoding='utf-8')

    result = subprocess.run(
        ['bash', INSTALL_SH, '--api-key-file', str(key_file)], cwd=tmp_path,
        env=dict(os.environ, HOME=str(tmp_path)), capture_output=True, text=True,
        timeout=5)

    assert result.returncode == 2
    assert expected in result.stderr
    assert secret not in result.stdout + result.stderr
    assert not (tmp_path / 'tofu').exists()
    assert not (tmp_path / 'logs').exists()


def test_install_accepts_crlf_key_file_as_one_line(tmp_path):
    key_file = tmp_path / 'windows.key'
    key_file.write_bytes(b'crlf-secret-value\r\n')
    result = subprocess.run(
        ['bash', INSTALL_SH, '--api-key-file', str(key_file), '--port', 'bad'],
        cwd=tmp_path, env=dict(os.environ, HOME=str(tmp_path)),
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert '--port must be an integer' in result.stderr
    assert 'must be one non-empty line' not in result.stderr
    assert 'crlf-secret-value' not in result.stdout + result.stderr
    assert not (tmp_path / 'logs').exists()


def test_install_log_does_not_make_empty_clone_target_nonempty(tmp_path):
    """Execute the real installer prefix with only ``git`` replaced.

    A pre-existing empty --dir is a valid clone target. The transcript must be
    staged beside it, then remain live under the cloned checkout after source
    resolution; creating ``<target>/logs`` before clone breaks this workflow.
    """
    source = open(INSTALL_SH, encoding='utf-8').read()
    stop = source.index('#  Step 0.6: Choose install backend')
    fake_git = r'''
git() {
    if [[ "${1-}" != "clone" ]]; then
        return 0
    fi
    local clone_target=""
    for clone_target in "$@"; do :; done
    if [[ -e "${clone_target}/logs" ]]; then
        echo "clone target was contaminated before git clone" >&2
        return 64
    fi
    mkdir -p "$clone_target"
    : > "${clone_target}/server.py"
    : > "${clone_target}/requirements.txt"
}
'''
    target = tmp_path / 'empty-target'
    target.mkdir()
    result = subprocess.run(
        ['bash', '-s', '--', '--dir', str(target)],
        input=fake_git + source[:stop], cwd=tmp_path,
        env=dict(os.environ, HOME=str(tmp_path)),
        capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'clone target was contaminated' not in result.stderr
    logs = list((target / 'logs').glob('install-*.log'))
    assert len(logs) == 1
    assert logs[0].stat().st_mode & 0o777 == 0o600
    assert 'Repository cloned' in logs[0].read_text(encoding='utf-8')
    assert 'Install log moved to:' in result.stdout
    assert not list(tmp_path.glob('.install-*.pending'))


def test_arch_whitelist_rejects_unsupported_arch(tmp_path):
    """riscv64/s390x/ppc64le must fail fast with a Docker pointer, not fall
    through to a misleading Miniforge-mirror failure."""
    bin_dir = tmp_path / 'bin'
    _fake_uname(bin_dir, 'riscv64')
    target = tmp_path / 'empty-target'
    target.mkdir()
    env = dict(os.environ, HOME=str(tmp_path),
               PATH=f"{bin_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ['bash', '-s', '--', '--dir', str(target)],
        input=_install_prefix(), cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'Unsupported architecture: riscv64' in result.stdout + result.stderr
    assert 'Docker image' in result.stdout + result.stderr
    assert 'Miniforge mirrors failed' not in result.stdout + result.stderr


def test_disk_preflight_fails_below_threshold(tmp_path):
    """A nearly-full install-dir parent must fail before any download, with a
    clear message, instead of ENOSPC surfacing deep in uv/conda."""
    bin_dir = tmp_path / 'bin'
    _fake_uname(bin_dir, 'x86_64')
    df = bin_dir / 'df'
    df.write_text(
        '#!/bin/bash\n'
        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
        "printf '/dev/sda1 1000000 999000 1000 100%% /\\n'\n")
    df.chmod(0o755)
    target = tmp_path / 'empty-target'
    target.mkdir()
    env = dict(os.environ, HOME=str(tmp_path),
               PATH=f"{bin_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ['bash', '-s', '--', '--dir', str(target)],
        input=_install_prefix(), cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'Not enough free disk space' in result.stdout + result.stderr


@pytest.mark.parametrize(
    ('port_explicit', 'port_from_env', 'requested_port',
     'expected_port', 'expected_file_port'),
    [
        (0, 0, '15000', '16666', '16666'),
        (1, 0, '17777', '17777', '17777'),
        (0, 1, '15599', '15599', '16666'),
    ],
)
def test_real_installer_env_block_preserves_or_explicitly_changes_port(
        tmp_path, port_explicit, port_from_env, requested_port,
        expected_port, expected_file_port):
    """Execute the production Step 9 block, not a Python imitation of it."""
    source = open(INSTALL_SH, encoding='utf-8').read()
    start = source.index('_set_env_var() {')
    stop = source.index('#  Step 9.5: Post-install Sidecar smoke test', start)
    config_block = source[start:stop]
    env_file = tmp_path / '.env'
    env_file.write_text('PORT="16666"\nKEEP=this\n', encoding='utf-8')

    script = f'''set -euo pipefail
info() {{ printf 'info: %s\\n' "$*"; }}
ok() {{ printf 'ok: %s\\n' "$*"; }}
fail() {{ printf 'fail: %s\\n' "$*" >&2; exit 1; }}
OS=Linux
INSTALL_DIR={shlex.quote(ROOT)}
ENV_FILE={shlex.quote(str(env_file))}
ENV_PYTHON={shlex.quote(sys.executable)}
_ENV_FILE_EXISTED=1
PORT={shlex.quote(requested_port)}
PORT_EXPLICIT={port_explicit}
PORT_FROM_ENV={port_from_env}
API_KEY=''
DB_BACKEND_CHOICE=sqlite
PG_INSTALLED_MAJOR=''
{config_block}
printf 'resolved-port=%s\\n' "$PORT"
'''
    result = subprocess.run(
        ['bash', '-c', script], cwd=ROOT, capture_output=True, text=True,
        timeout=10)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f'resolved-port={expected_port}' in result.stdout
    values = dict(
        line.split('=', 1) for line in env_file.read_text(encoding='utf-8').splitlines()
        if '=' in line)
    assert values['PORT'].strip('"') == expected_file_port
    assert values['KEEP'] == 'this'
    assert env_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ('args', 'message'),
    [
        (['--port'], '--port requires a value'),
        (['--port', 'not-a-port'], '--port must be an integer from 1 to 65535'),
        (['--port=70000'], '--port must be an integer from 1 to 65535'),
        (['--api-key-file'], '--api-key-file requires a value'),
        (['--api-key-file', '/definitely/missing/tofu-key'],
         '--api-key-file must name a readable regular file'),
        (['--api-key', 'legacy-value', '--api-key-file', '/tmp/unused'],
         '--api-key and --api-key-file cannot be combined'),
        (['--unknown'], 'unknown option: --unknown'),
    ],
)
def test_install_argument_errors_fail_before_side_effects(tmp_path, args, message):
    result = subprocess.run(
        ['bash', INSTALL_SH, *args], cwd=tmp_path,
        env=dict(os.environ, HOME=str(tmp_path)), capture_output=True, text=True,
        timeout=5)
    assert result.returncode == 2
    assert message in result.stderr
    assert "install.sh --help" in result.stderr
    assert not (tmp_path / 'tofu').exists()
    assert not (tmp_path / 'logs').exists()


def test_healthcheck_rejects_invalid_runtime_arguments_before_checks():
    result = _run_runtime('--port', 'not-a-port', timeout=5)
    assert result.returncode == 2
    assert 'must be an integer from 1 to 65535' in result.stderr
    assert 'Python Syntax Check' not in result.stdout

    result = subprocess.run(
        [sys.executable, HC, '--require-browser'], cwd=ROOT,
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert '--require-browser require --runtime' in result.stderr


def test_version_entrypoints_return_without_starting_the_server(tmp_path):
    version = open(os.path.join(ROOT, 'VERSION'), encoding='utf-8').read().strip()
    env = dict(os.environ, HOME=str(tmp_path), PORT='15996')
    for script in ('server.py', 'bootstrap.py', 'serverctl.py'):
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, script), '--version'],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == f'Tofu {version}'
    assert not (tmp_path / 'tofu').exists()
    assert not (tmp_path / 'logs').exists()


def test_bootstrap_rejects_silently_ignored_server_options_before_boot(tmp_path):
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'bootstrap.py'), '--port', '16000'],
        cwd=tmp_path, env=dict(os.environ, HOME=str(tmp_path)),
        capture_output=True, text=True, timeout=5)

    assert result.returncode == 2
    assert 'server command-line options are not supported' in result.stderr
    assert 'python server.py [SERVER_OPTIONS]' in result.stderr
    assert not (tmp_path / 'logs').exists()


@pytest.mark.parametrize('port', ['not-a-port', '0', '65536'])
def test_bootstrap_rejects_invalid_port_as_configuration_not_dependency_failure(
        tmp_path, port):
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'bootstrap.py')], cwd=tmp_path,
        env=dict(os.environ, HOME=str(tmp_path), PORT=port),
        capture_output=True, text=True, timeout=5)

    assert result.returncode == 2
    assert 'Invalid startup configuration' in result.stderr
    assert f'got {port!r}' in result.stderr
    assert 'serverctl.py doctor' in result.stderr
    assert 'Starting Tofu' not in result.stderr


@pytest.mark.parametrize(
    ('contents', 'message'),
    [
        pytest.param(
            b'PORT=15000\n\xff', 'must be valid UTF-8', id='invalid-utf8'),
        pytest.param(
            b'X' * (256 * 1024 + 1),
            'exceeds the 262144-byte limit',
            id='oversized',
        ),
    ],
)
def test_bootstrap_explains_unreadable_dotenv_without_traceback(
        tmp_path, contents, message):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    shutil.copy(os.path.join(ROOT, 'bootstrap.py'), checkout / 'bootstrap.py')
    shutil.copy(os.path.join(ROOT, 'tofu_dotenv.py'), checkout / 'tofu_dotenv.py')
    shutil.copytree(os.path.join(ROOT, 'bootstrap_pkg'), checkout / 'bootstrap_pkg')
    (checkout / '.env').write_bytes(contents)

    result = subprocess.run(
        [sys.executable, str(checkout / 'bootstrap.py')],
        cwd=checkout,
        env=dict(os.environ, HOME=str(tmp_path)),
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert 'Invalid project .env' in result.stderr
    assert message in result.stderr
    assert 'serverctl.py doctor' in result.stderr
    assert 'Traceback' not in result.stderr
    assert 'Starting Tofu' not in result.stderr


@pytest.mark.parametrize(
    ('server_args', 'message'),
    [
        (['--port', '70000'], '--port must be an integer from 1 to 65535'),
        (['--workers', '2'], '--workers must be 1'),
        (['--unknown'], 'unsupported server option'),
        (['positional'], 'positional server arguments are not supported'),
    ],
)
def test_server_rejects_invalid_lifecycle_options_before_start(
        tmp_path, server_args, message):
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'server.py'), *server_args],
        cwd=tmp_path, env=dict(os.environ, HOME=str(tmp_path), PORT='15995'),
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert message in result.stderr
    assert not (tmp_path / 'logs').exists()


def test_healthcheck_runtime_branch_precedes_dev_lint():
    src = open(HC, encoding='utf-8').read()
    branch_at = src.find("if '--runtime' in sys.argv:")
    lint_at = src.find('section("1. Python Syntax Check")')
    assert branch_at != -1, 'healthcheck.py lost the --runtime entry branch'
    assert lint_at != -1
    # The runtime branch sys.exit()s; placed after the dev lint it would
    # never fire (and the lint would slow-fail on a broken fresh install).
    assert branch_at < lint_at, '--runtime branch must precede the dev-lint sections'
