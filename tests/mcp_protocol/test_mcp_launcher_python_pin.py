"""MCP launchers resolve under the server's Python minor, not the newest managed one.

Without an interpreter request uv/uvx take the NEWEST interpreter in the managed
store, and that store (``UV_PYTHON_INSTALL_DIR`` under ``TOFU_MCP_CACHE_DIR``) is
shared with ``run_command`` children, so one ``uv python install`` there moves
every launcher. Under the supply cutoff (``UV_EXCLUDE_NEWER``) compiled
dependencies have no wheels for an interpreter released after it: measured
2026-11-06, ``uvx --from tools/github-batch-mcp`` picked a managed CPython 3.15.1,
built ``pydantic-core==2.46.4`` from source, and ``initialize`` timed out at 42 s.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

import lib.mcp.client as mcp_client
from lib.mcp.stdio_environment import build_stdio_environment
from lib.project_mod.run_command import _get_cmd_env

pytestmark = pytest.mark.unit

SERVER_MINOR = f'{sys.version_info.major}.{sys.version_info.minor}'

_INHERITED_TOOL_SETTINGS = (
    'UV_PYTHON', 'UV_EXCLUDE_NEWER', 'npm_config_before', 'TOFU_MCP_SUPPLY_CUTOFF',
    'UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR',
    'UV_LINK_MODE', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'PIP_CACHE_DIR',
    'npm_config_cache', 'CARGO_HOME', 'npm_config_prefix', 'NPM_CONFIG_PREFIX',
)


@pytest.fixture
def launcher_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(tmp_path / 'cache'))
    for key in _INHERITED_TOOL_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    # Never consult the deployment's proxy pool during a hermetic MCP test.
    monkeypatch.setattr('lib.mcp.client._vendor._propagate_pool_proxy_env', lambda env: None)
    return tmp_path


def test_launcher_resolves_under_the_server_python_minor(launcher_environment):
    environment = build_stdio_environment({'command': 'uvx'}, prepare_directories=False)

    assert environment['UV_EXCLUDE_NEWER']
    assert environment['UV_PYTHON'] == SERVER_MINOR


def test_prewarm_warms_the_interpreter_the_launch_uses(launcher_environment, monkeypatch):
    warmed = {}

    class RecordingSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(argv, *, env, **kwargs):
            warmed.update(env)
            return subprocess.CompletedProcess(argv, 0, '', '')

    monkeypatch.setattr(mcp_client, 'subprocess', RecordingSubprocess)
    monkeypatch.setattr(mcp_client, 'vendored_launch_argv', lambda command: [
        'uv', 'run', '--no-project', '--with-editable', str(launcher_environment), command])

    ready, _ = mcp_client.prewarm_vendored_launcher('pin-probe-mcp')

    launch = build_stdio_environment({'command': 'pin-probe-mcp'}, prepare_directories=False)
    assert ready
    assert warmed['UV_PYTHON'] == launch['UV_PYTHON'] == SERVER_MINOR


@pytest.mark.parametrize('source', ['server configuration', 'operator environment'])
def test_explicit_interpreter_selection_wins(launcher_environment, monkeypatch, source):
    config = {'command': 'uvx'}
    if source == 'server configuration':
        config['env'] = {'UV_PYTHON': '3.13'}
    else:
        monkeypatch.setenv('UV_PYTHON', '3.13')

    environment = build_stdio_environment(config, prepare_directories=False)

    assert environment['UV_PYTHON'] == '3.13'


def test_disabling_the_supply_cutoff_releases_the_pin(launcher_environment, monkeypatch):
    monkeypatch.setenv('TOFU_MCP_SUPPLY_CUTOFF', '')

    environment = build_stdio_environment({'command': 'uvx'}, prepare_directories=False)

    assert 'UV_EXCLUDE_NEWER' not in environment
    assert 'UV_PYTHON' not in environment


def test_command_children_keep_uv_interpreter_selection(launcher_environment):
    """Commands share the managed store but not the launcher pin.

    ``UV_PYTHON`` outranks ``.python-version`` and project ``requires-python``,
    so leaking it would silently retarget a user's ``uv run``/``uv venv``.
    """
    project = launcher_environment / 'project'
    project.mkdir()

    command = _get_cmd_env(str(project), project_root=str(project))
    launcher = build_stdio_environment({'command': 'uvx'}, prepare_directories=False)

    assert command['UV_PYTHON_INSTALL_DIR'] == launcher['UV_PYTHON_INSTALL_DIR']
    assert 'UV_PYTHON' not in command
