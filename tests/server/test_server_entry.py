"""Executable handoff and pre-import environment regression contracts."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from bootstrap_pkg import server_entry

pytestmark = pytest.mark.unit
ROOT = Path(__file__).parents[2]


def _entry_environment():
    environment = os.environ.copy()
    for key in ('TOFU_SERVER_WORKER', '_TOFU_VIA_BOOTSTRAP', 'TOFU_RUN_SERVER',
                '_TOFU_REEXEC_PORT'):
        environment.pop(key, None)
    return environment


@pytest.mark.parametrize('args,code,output', [
    (['--help'], 0, 'Starts Tofu through the project-local manager.'),
    (['--version'], 0, 'Tofu '),
    (['--unknown-option'], 2, 'unsupported server option'),
])
def test_real_entry_diagnostics_do_not_import_application(tmp_path, args, code, output):
    # The shipped executable runs normally. A subprocess import hook fails
    # loudly if help/version/argument handling touches the application.
    (tmp_path / 'sitecustomize.py').write_text(
        "import sys\n"
        "class RejectApplication:\n"
        " def find_spec(self, fullname, *args):\n"
        "  if fullname == 'lib' or fullname.startswith('lib.'):\n"
        "   raise RuntimeError('application imported before handoff')\n"
        "sys.meta_path.insert(0, RejectApplication())\n")
    environment = _entry_environment()
    environment['PYTHONPATH'] = str(tmp_path)
    result = subprocess.run([sys.executable, str(ROOT / 'server.py'), *args],
                            cwd=tmp_path, env=environment, capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == code, result.stdout + result.stderr
    assert output.lower() in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize('key,value', [
    ('TOFU_SERVER_WORKER', '1'), ('_TOFU_VIA_BOOTSTRAP', '1'),
    ('TOFU_RUN_SERVER', '1'), ('_TOFU_REEXEC_PORT', '15000'),
    ('container-pid', '1'),
])
def test_owned_worker_bypasses_manager(monkeypatch, key, value):
    for name in ('TOFU_SERVER_WORKER', '_TOFU_VIA_BOOTSTRAP', 'TOFU_RUN_SERVER',
                 '_TOFU_REEXEC_PORT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server_entry.os, 'getpid', lambda: 1 if key == 'container-pid' else 42)
    if key != 'container-pid':
        monkeypatch.setenv(key, value)
    control = types.ModuleType('serverctl')
    control.managed_start = lambda *_a, **_kw: pytest.fail('worker recursively delegated')
    monkeypatch.setitem(sys.modules, 'serverctl', control)
    server_entry.delegate_server_start([])


def test_handoff_forwards_options_and_exit_status(monkeypatch, tmp_path):
    for key in ('TOFU_SERVER_WORKER', '_TOFU_VIA_BOOTSTRAP', 'TOFU_RUN_SERVER',
                '_TOFU_REEXEC_PORT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(server_entry.os, 'getpid', lambda: 42)
    monkeypatch.setattr(server_entry, '_PROJ_DIR', str(tmp_path))
    calls = []
    control = types.ModuleType('serverctl')
    control.managed_start = lambda *a, **kw: calls.append((a, kw)) or 7
    monkeypatch.setitem(sys.modules, 'serverctl', control)
    with pytest.raises(SystemExit) as result:
        server_entry.delegate_server_start(['--port', '16000', '--no-tls'])
    assert result.value.code == 7
    assert calls == [((['--port', '16000', '--no-tls'],),
                      {'wait': 180.0, 'source': 'python-server.py'})]


def test_import_context_skips_executable_wrappers():
    # Exercise the real entry wrappers without importing the heavy app. The
    # owning executable context is the boundary, not the helper module name.
    tree = ast.parse((ROOT / 'server.py').read_text())
    wrappers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in ('_delegate_executable_to_manager',
                                  '_load_early_resource_environment')]
    namespace = {'__name__': 'server'}
    exec(compile(ast.Module(body=wrappers, type_ignores=[]), 'server.py', 'exec'), namespace)
    assert namespace['_delegate_executable_to_manager']() is None
    assert namespace['_load_early_resource_environment']() is None


def test_early_environment_loaded_by_imported_helper(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('TOFU_NUMERIC_THREADS=3\nTOFU_DATA_DIR=/tmp/entry-data\n')
    monkeypatch.setattr(server_entry, '_PROJ_DIR', str(tmp_path))
    environment = dict(os.environ)
    environment.pop('TOFU_PROJECT_PATH', None)
    environment.pop('TOFU_DATA_DIR', None)
    environment['TOFU_NUMERIC_THREADS'] = '5'
    monkeypatch.setattr(server_entry.os, 'environ', environment)
    server_entry.load_early_resource_environment()
    assert os.environ['TOFU_DATA_DIR'] == '/tmp/entry-data'
    assert os.environ['TOFU_PROJECT_PATH'] == str(tmp_path)
    assert os.environ['TOFU_NUMERIC_THREADS'] == '5'
