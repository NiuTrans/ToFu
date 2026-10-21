"""Exact child PATH, persistent tools, Python scope and native pipeline contracts."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import venv

import pytest

from lib.tools.process_environment import (
    apply_tool_paths, environment_report, resolve_executable, validate_environment,
)
from lib.mcp.stdio_environment import build_stdio_environment
from lib.project_mod.run_command import _get_cmd_env, tool_run_command
from lib.project_mod.tools import _exec_run_command
from lib.project_mod import config

pytestmark = pytest.mark.unit


def executable(path, body='echo tool-ok'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)
    return path


@pytest.fixture
def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    cache = tmp_path / 'cache'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(cache))
    for key in ('UV_TOOL_BIN_DIR', 'UV_TOOL_DIR', 'UV_CACHE_DIR',
                'PIP_CACHE_DIR', 'npm_config_cache', 'XDG_CACHE_HOME',
                'XDG_DATA_HOME', 'CARGO_HOME', 'npm_config_prefix',
                'NPM_CONFIG_PREFIX'):
        monkeypatch.delenv(key, raising=False)
    # Never consult the deployment's proxy pool during a hermetic MCP test.
    monkeypatch.setattr('lib.mcp.client._vendor._propagate_pool_proxy_env', lambda env: None)
    return home, cache


def test_profile_is_idempotent_and_installer_directories_exist_in_path_before_install(tmp_path):
    environment = {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    apply_tool_paths(environment, cache_root=str(tmp_path / 'cache'))
    first = environment.copy()
    apply_tool_paths(environment, cache_root=str(tmp_path / 'cache'))
    assert environment == first
    cli = executable(tmp_path / '.local/bin/profile-cli')
    assert resolve_executable('profile-cli', environment) == str(cli)
    assert environment['UV_TOOL_DIR'] == str(tmp_path / 'cache/share/uv/tools')


def test_command_and_mcp_reuse_same_persistent_tool_and_caches(tmp_path, isolated_environment):
    _, cache = isolated_environment
    command_env = _get_cmd_env(str(tmp_path))
    cli = executable(Path(command_env['UV_TOOL_BIN_DIR']) / 'installed-cli')
    for environment in (_get_cmd_env(str(tmp_path)), build_stdio_environment({})):
        assert resolve_executable('installed-cli', environment) == str(cli)
        assert environment['UV_TOOL_DIR'] == str(cache / 'share/uv/tools')
        assert environment['UV_CACHE_DIR'] == command_env['UV_CACHE_DIR']
        assert environment['npm_config_cache'] == command_env['npm_config_cache']


def test_mcp_configuration_path_wins_over_parent_and_runtime(tmp_path, isolated_environment, monkeypatch):
    parent = executable(tmp_path / 'parent/custom-mcp', 'echo parent')
    child = executable(tmp_path / 'configured/custom-mcp', 'echo configured')
    monkeypatch.setenv('PATH', str(parent.parent))
    environment = build_stdio_environment({'env': {'PATH': str(child.parent), 'MCP_SECRET': 'hidden'}})
    assert resolve_executable('custom-mcp', environment) == str(child)
    assert subprocess.check_output([resolve_executable('custom-mcp', environment)], env=environment, text=True).strip() == 'configured'
    assert 'hidden' not in json.dumps(environment_report(environment, ['custom-mcp']))


def test_mcp_explicit_tool_bin_is_discovered(tmp_path, isolated_environment):
    cli = executable(tmp_path / 'custom-tools/custom-mcp')
    environment = build_stdio_environment({'env': {'UV_TOOL_BIN_DIR': str(cli.parent)}})
    assert resolve_executable('custom-mcp', environment) == str(cli)


@pytest.mark.parametrize('environment', [{'BAD=KEY': 'x'}, {'KEY': 3}, {'KEY': 'x\0y'}, {'': 'x'}])
def test_invalid_environment_fails_without_echoing_values(environment):
    with pytest.raises(ValueError) as error:
        validate_environment(environment)
    assert 'x\0y' not in str(error.value)


def test_root_virtualenv_survives_sticky_subdirectory_and_preserves_installed_module(tmp_path, isolated_environment):
    venv.EnvBuilder(with_pip=False).create(tmp_path / '.venv')
    python = tmp_path / '.venv/bin/python'
    site = subprocess.check_output([str(python), '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip()
    Path(site, 'installed_once.py').write_text('VALUE = "installed-once"\n')
    sub = tmp_path / 'sub'
    sub.mkdir()
    conv = 'test-persistent-python-environment'
    config.set_conv_roots(conv, str(tmp_path))
    try:
        first = _exec_run_command({'command': 'python -c "import installed_once; print(installed_once.VALUE)"', 'working_dir': 'sub'}, str(tmp_path), conv, 'task-env', {})
        second = _exec_run_command({'command': 'python -c "import installed_once, sys; print(installed_once.VALUE); print(sys.prefix)"'}, str(tmp_path), conv, 'task-env', {})
        assert '\ninstalled-once\n' in first
        assert '\ninstalled-once\n' in second
        assert str(tmp_path / '.venv') in second
    finally:
        config.clear_conv_state(conv)


def test_project_search_stops_at_explicit_authorized_root(tmp_path, isolated_environment, monkeypatch):
    venv.EnvBuilder(with_pip=False).create(tmp_path / '.venv')
    project = tmp_path / 'other-project'
    project.mkdir()
    monkeypatch.setenv('VIRTUAL_ENV', '/stale/activation')
    monkeypatch.setenv('PYTHONHOME', '/stale/python')
    environment = _get_cmd_env(str(project), project_root=str(project))
    assert 'VIRTUAL_ENV' not in environment
    assert 'PYTHONHOME' not in environment
    assert str(tmp_path / '.venv/bin') not in environment['PATH'].split(os.pathsep)


def test_incomplete_virtualenv_refuses_python_instead_of_using_server_python(tmp_path, isolated_environment):
    (tmp_path / '.venv').mkdir()
    (tmp_path / '.venv/pyvenv.cfg').write_text('home = /missing\n')
    output = tool_run_command(str(tmp_path), "python -c 'print(\"should-not-run\")'")
    assert 'Project .venv is incomplete' in output
    assert '\nshould-not-run\n' not in output


def test_pipe_installer_and_subsequent_call_reuse_tool_without_installing_again(tmp_path, isolated_environment):
    home, _ = isolated_environment
    installer = tmp_path / 'install.sh'
    installer.write_text('mkdir -p "$HOME/.local/bin"\nprintf "#!/bin/sh\\necho pipe-tool-ready\\n" > "$HOME/.local/bin/pipe-tool"\nchmod +x "$HOME/.local/bin/pipe-tool"\n')
    # Native stdin forwarding to sh; the installer-created directory was in
    # the frozen PATH even before the first command started.
    first = tool_run_command(str(tmp_path), f'cat {shlex.quote(str(installer))} | sh && pipe-tool')
    assert '\npipe-tool-ready\n' in first
    stamp = (home / '.local/bin/pipe-tool').stat().st_mtime_ns
    second = tool_run_command(str(tmp_path), 'pipe-tool')
    assert '\npipe-tool-ready\n' in second
    assert (home / '.local/bin/pipe-tool').stat().st_mtime_ns == stamp


def test_restricted_command_tool_cache_stays_in_workspace(tmp_path, isolated_environment):
    from lib.project_mod.abs_path_guard import set_restricted, reset_restricted
    token = set_restricted(True)
    try:
        environment = _get_cmd_env(str(tmp_path), project_root=str(tmp_path))
        assert os.path.commonpath([environment['UV_TOOL_DIR'], str(tmp_path)]) == str(tmp_path)
        assert environment['HOME'] == str(tmp_path)
    finally:
        reset_restricted(token)


def test_node_proxy_flag_is_not_injected_into_older_runtime(tmp_path, isolated_environment, monkeypatch):
    monkeypatch.delenv('NODE_OPTIONS', raising=False)
    environment = build_stdio_environment({})
    assert environment['NODE_USE_ENV_PROXY'] == '1'
    assert '--use-env-proxy' not in environment.get('NODE_OPTIONS', '')


def test_diagnostic_uses_exact_command_environment_and_hides_credentials(tmp_path, isolated_environment, monkeypatch):
    executable(tmp_path / 'home/.local/bin/diagnostic-cli')
    monkeypatch.setenv('UNREGISTERED_API_KEY', 'must-not-appear')
    result = subprocess.run([sys.executable, 'scripts/check_tool_environment.py', '--cwd', str(tmp_path), '--launcher', 'diagnostic-cli'], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['executables']['diagnostic-cli'].endswith('/.local/bin/diagnostic-cli')
    assert 'must-not-appear' not in result.stdout + result.stderr


def test_mcp_owner_resolves_configured_child_path_before_stdio_launch(tmp_path, isolated_environment, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    import mcp
    import mcp.client.stdio
    from lib.mcp.client._bridge import MCPBridge, _MCPServerHandle
    from lib.mcp.client._errors import MCPConnectError
    import lib.mcp.client as facade

    binary = executable(tmp_path / 'configured/owner-mcp')
    captured = {}

    @asynccontextmanager
    async def capture_transport(params, **kwargs):
        captured['command'] = params.command
        captured['env'] = params.env
        raise RuntimeError('transport stopped after capturing launch')
        yield  # An async transport boundary, intentionally fails before I/O.

    monkeypatch.setattr(mcp, 'Client', None, raising=False)
    monkeypatch.setattr(mcp.client.stdio, 'stdio_client', capture_transport)
    monkeypatch.setattr(facade, 'vendored_launch_argv', lambda command: None)

    async def run():
        bridge = MCPBridge(owner_user_id=31)
        handle = _MCPServerHandle('configured', {'command': 'owner-mcp', 'env': {'PATH': str(binary.parent)}})
        loop = asyncio.get_running_loop()
        handle._ready_future = loop.create_future()
        handle._closed_future = loop.create_future()
        handle._shutdown_event = asyncio.Event()
        await bridge._server_owner(handle)
        with pytest.raises(MCPConnectError, match='transport stopped'):
            await handle._ready_future
        assert handle._closed_future.done()
        assert handle._stderr_file is None

    asyncio.run(run())
    assert captured['command'] == str(binary)
    assert captured['env']['PATH'] == str(binary.parent)


def test_curl_pipe_installer_uses_native_streaming_shell(tmp_path, isolated_environment):
    import http.server
    import threading
    import shutil

    curl = shutil.which('curl')
    if not curl:
        pytest.skip('curl is not installed on this test host')
    body = b'mkdir -p "$HOME/.local/bin"\nprintf "#!/bin/sh\\necho curl-pipe-ready\\n" > "$HOME/.local/bin/curl-pipe-tool"\nchmod +x "$HOME/.local/bin/curl-pipe-tool"\n'

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # A hermetic installer fixture carries no request logs.

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        command = f'{shlex.quote(curl)} --noproxy "*" -fsS http://127.0.0.1:{server.server_port}/install.sh | sh && curl-pipe-tool'
        output = tool_run_command(str(tmp_path), command)
        assert '\ncurl-pipe-ready\n' in output
        assert '[exit code: 0]' in output
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def test_command_resolution_uses_child_working_directory_for_relative_path(tmp_path):
    executable(tmp_path / 'bin/relative-cli')
    environment = {'PATH': 'bin'}
    report = environment_report(environment, ['relative-cli', './bin/relative-cli'], cwd=str(tmp_path))
    assert set(report['executables'].values()) == {str(tmp_path / 'bin/relative-cli')}


def test_mcp_diagnostic_profile_does_not_create_directories(tmp_path, isolated_environment):
    _, cache = isolated_environment
    assert not cache.exists()
    environment = build_stdio_environment({}, prepare_directories=False)
    assert environment['UV_TOOL_BIN_DIR'] == str(cache / 'bin')
    assert not cache.exists()


def test_offline_pip_install_once_is_reused_from_project_subdirectory(tmp_path, isolated_environment):
    import zipfile

    venv.EnvBuilder(with_pip=True).create(tmp_path / '.venv')
    wheel = tmp_path / 'reuse_probe-1.0-py3-none-any.whl'
    files = {
        'reuse_probe.py': 'def main():\n    print("pip-install-reused")\n',
        'reuse_probe-1.0.dist-info/METADATA': 'Metadata-Version: 2.1\nName: reuse-probe\nVersion: 1.0\n',
        'reuse_probe-1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nGenerator: environment-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        'reuse_probe-1.0.dist-info/entry_points.txt': '[console_scripts]\nreuse-probe = reuse_probe:main\n',
    }
    files['reuse_probe-1.0.dist-info/RECORD'] = ''.join(f'{name},,\n' for name in [*files, 'reuse_probe-1.0.dist-info/RECORD'])
    with zipfile.ZipFile(wheel, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    installed = tool_run_command(str(tmp_path), f'python -m pip install --no-index --no-deps {shlex.quote(str(wheel))} && reuse-probe')
    assert '\npip-install-reused\n' in installed
    assert '[exit code: 0]' in installed
    binary = tmp_path / '.venv/bin/reuse-probe'
    stamp = binary.stat().st_mtime_ns
    sub = tmp_path / 'sub'
    sub.mkdir()
    reused = tool_run_command(str(sub), 'reuse-probe', environment_root=str(tmp_path))
    assert '\npip-install-reused\n' in reused
    assert '[exit code: 0]' in reused
    assert binary.stat().st_mtime_ns == stamp


def test_mcp_install_job_recognizes_existing_tool_without_warming_again(tmp_path, isolated_environment, monkeypatch):
    import lib.mcp.client as facade

    home, _ = isolated_environment
    executable(home / '.local/bin/reusable-mcp')
    monkeypatch.setattr(facade, 'vendored_launch_argv', lambda command: None)
    monkeypatch.setattr(facade, '_install_jobs', {})

    def unexpected_warm(command):
        pytest.fail('An installed launcher must not trigger another installation')

    monkeypatch.setattr(facade, 'prewarm_vendored_launcher', unexpected_warm)
    assert facade.start_install_job('reusable-mcp')['state'] == 'ready'


def test_mcp_preserves_installed_runtime_before_interpreter_siblings(tmp_path, monkeypatch):
    # PATH has three tiers: the repo-bundled node toolchain, the user's
    # ~/.local/bin, then interpreter siblings. This test owns the SECOND
    # ordering only; the bundle tier has its own suite
    # (tests/tools/test_bundled_toolchain_path_precedence.py).
    monkeypatch.setattr('lib.tools.process_environment.bundled_toolchain_bins',
                        lambda: [])
    local_bin = tmp_path / 'home/.local/bin'
    interpreter_bin = tmp_path / 'python/bin'
    local_node = executable(local_bin / 'node')
    executable(interpreter_bin / 'node')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('PATH', str(local_bin) + os.pathsep + str(interpreter_bin))
    monkeypatch.setattr(sys, 'executable', str(interpreter_bin / 'python'))
    environment = build_stdio_environment({}, prepare_directories=False)
    assert resolve_executable('node', environment) == str(local_node)
