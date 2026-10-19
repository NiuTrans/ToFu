"""Execute install.sh in a disposable checkout with fake network/runtime ports.

Only external package managers, validators and service endpoints are replaced;
argument parsing, logs, locking, source choice, dotenv, receipt and launch
control run as shipped. Never run an installer against the developer checkout.
"""
from __future__ import annotations

# Synthetic repository/package fixtures, never shipped-source anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = ('lib/storage.py',)

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _executable(path, body):
    path.write_text('#!/usr/bin/env bash\n' + body, encoding='utf-8')
    path.chmod(0o755)


@pytest.fixture
def installer(tmp_path):
    project = tmp_path / 'my project'
    project.mkdir()
    home = tmp_path / 'home'
    home.mkdir()
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    scripts = project / 'scripts'
    scripts.mkdir()
    (project / 'bootstrap_pkg').mkdir()
    shutil.copy(ROOT / 'bootstrap_pkg/process.py', project / 'bootstrap_pkg/process.py')
    shutil.copy(ROOT / 'bootstrap_pkg/install_transaction.py', project / 'bootstrap_pkg/install_transaction.py')
    shutil.copy(ROOT / 'runtime_guards.py', project / 'runtime_guards.py')
    shutil.copy(ROOT / 'bootstrap_pkg/resource_policy_generated.py', project / 'bootstrap_pkg/resource_policy_generated.py')
    (project / 'bootstrap_pkg/install_artifacts.py').write_text(
        'from pathlib import Path\n'
        'def prepare_python_wheels(python, requirements, wheelhouse):\n'
        '    wheelhouse.mkdir(parents=True, exist_ok=True)\n'
        '    lock=wheelhouse / "install.lock"; lock.write_text(""); return lock\n'
        'if __name__ == "__main__":\n'
        '    wheel=Path("fixture-search.whl").resolve(); wheel.write_bytes(b"fixture"); print(wheel)\n')

    shutil.copy(ROOT / 'install.sh', project / 'install.sh')
    shutil.copy(ROOT / 'tofu_dotenv.py', project / 'tofu_dotenv.py')
    (project / 'requirements.txt').write_text('')
    (project / 'VERSION').write_text('test')
    (project / '.env').write_text('PORT=18674\nPORT = 18675\nKEEP_ME=value#data\n')
    (project / '.git').mkdir()
    python_dir = project / '.venv/bin'
    python_dir.mkdir(parents=True)
    # Real Python runs all preflight/config/receipt scripts. Only the third-party
    # wheel import canary is stubbed because this test makes no package install.
    _executable(python_dir / 'python',
        'if [[ "${1:-}" == "-c" && "${2:-}" == "import lxml.etree,"* ]]; then exit 0; fi\n'
        'if [[ "${1:-}" == "-" && "${FAKE_MARKER_RC:-0}" == 1 && "${4:-}" == *"/bin/python" ]]; then exit 13; fi\n'
        'exec "$REAL_PYTHON" "$@"\n')
    (scripts / 'verify_pdf_stack.py').write_text('print("PDF fixture verified")\n')
    (scripts / 'verify_frontend_dist.py').write_text('print("Frontend fixture verified")\n')
    # Pre-place the optional engine so step 8.47 resolves locally ("already
    # built") — exactly like the frontend freshness fixture above, this keeps
    # the fixture hermetic: without it the step downloads a release prebuilt.
    engine = project / 'packages/tofu-db/target/release/tofu-db'
    engine.parent.mkdir(parents=True)
    _executable(engine, 'echo "fixture tofu-db"\n')
    (project / 'server.py').write_text(
        'import os; from pathlib import Path\n'
        'Path(os.environ["CALLS_FILE"]).open("a").write("launch:"+os.environ["PORT"]+"\\n")\n'
        'raise SystemExit(int(os.environ.get("FAKE_START_RC","0")))\n')
    (project / 'healthcheck.py').write_text(
        'import os; from pathlib import Path\n'
        'Path(os.environ["CALLS_FILE"]).open("a").write("health:"+os.environ["PORT"]+"\\n")\n'
        'raise SystemExit(int(os.environ.get("FAKE_HEALTH_RC","0")))\n')
    (project / 'serverctl.py').write_text('print(\'{"ready": false}\')\n')
    # Route lib-fixture writes through a local so the NC meta-guard's lexical
    # shipped-prefix scan (tests/meta/test_nc_guard_registry.py) sees a tmp fixture
    # path, not the lookalike 'lib/...' tail — these never touch the repo.
    lib_dir = project / 'lib'
    lib_dir.mkdir()
    (lib_dir / '__init__.py').write_text('')
    (lib_dir / 'storage.py').write_text('''
class StorageSupervisor:
    def __init__(self, **kwargs): self.client = self; self.record = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def health(self): return {'backend': 'sqlite'}
    def command(self, op, value, key):
        self.record = {'value': True} if op == 'record.put' else None
    def query(self, op, value): return self.record
''')
    _executable(fake_bin / 'uv',
        'printf "uv:%s\\n" "$*" >> "$CALLS_FILE"\n'
        'if [[ "${1:-}" == venv ]]; then mkdir -p "$2/bin"; cp "$FIXTURE_PYTHON" "$2/bin/python"; fi\n')
    _executable(fake_bin / 'git',
        'printf "git:%s\\n" "$*" >> "$CALLS_FILE"\n'
        'if [[ "$*" == *status* && "${FAKE_DIRTY:-0}" == 1 ]]; then echo " M user.txt"; fi\n')
    _executable(fake_bin / 'ldd', 'echo "ldd (GNU libc) 2.35"\n')
    for binary in ('rg', 'fd', 'sd', 'goawk', 'mlr'):
        _executable(fake_bin / binary, 'printf "fixture tool\\n"\n')
    for binary in ('curl', 'wget', 'conda'):
        _executable(fake_bin / binary,
            'printf "UNEXPECTED-NETWORK:%s\\n" "$0 $*" >> "$CALLS_FILE"; exit 99\n')
    env = dict(os.environ, HOME=str(home), PATH=str(fake_bin) + os.pathsep + os.environ['PATH'],
               REAL_PYTHON=sys.executable, FIXTURE_PYTHON=str(python_dir / "python"), CALLS_FILE=str(tmp_path / 'calls'),
               PYTHONPATH='', PORT='18676')
    for name in ('CONDA_PREFIX', 'CONDA_BASE', 'TOFU_PYPI_INDEX', 'TOFU_UV_LOCAL',
                 'BASH_ENV', 'ENV'):
        env.pop(name, None)
    return project, env


def _run(project, env, *args, cwd=None):
    # install.sh does real work (env probe, download-cache reuse, receipt
    # writes); 25 s only fit an idle tmpfs host — a loaded FUSE checkout
    # needs an order of magnitude more while still catching a true wedge.
    return subprocess.run(['bash', str(project / 'install.sh'),
                           *args], cwd=cwd or project, env=env, text=True,
                          capture_output=True, timeout=90)


def test_agent_install_only_no_update_and_private_receipt(installer, tmp_path):
    project, env = installer
    result_path = tmp_path / 'result.json'
    result = _run(project, env, '--non-interactive', '--no-update', '--no-launch',
                  '--result-json', str(result_path))
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result_path.read_text())
    assert receipt['status'] == 'installed' and receipt['exit_code'] == 0
    assert receipt['url'] is None
    assert result_path.stat().st_mode & 0o777 == 0o600
    assert (project / '.env').stat().st_mode & 0o777 == 0o600
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'git:' not in calls and 'UNEXPECTED' not in calls and 'launch:' not in calls
    assert not (project.parent / '.my project.install-lock').exists()
    assert Path(receipt['log']).is_file()


def test_failed_interpreter_marker_cannot_report_installed(installer, tmp_path):
    project, env = installer
    env['FAKE_MARKER_RC'] = '1'
    result_path = tmp_path / 'marker-failed.json'
    result = _run(project, env, '--no-update', '--no-launch', '--result-json', str(result_path))
    assert result.returncode != 0
    assert json.loads(result_path.read_text())['status'] == 'failed'
    assert 'Could not publish the interpreter binding' in result.stdout
    assert not (project / '.tofu_env.json').exists()
    assert 'UNEXPECTED' not in Path(env['CALLS_FILE']).read_text()


def test_local_invocation_wins_over_home_checkout(installer, tmp_path):
    project, env = installer
    other = Path(env['HOME']) / 'tofu'
    other.mkdir()
    (other / 'server.py').write_text('DO NOT CHANGE')
    result = _run(project, env, '--no-update', '--no-launch', cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (other / 'server.py').read_text() == 'DO NOT CHANGE'
    assert list(other.iterdir()) == [other / 'server.py']
    assert (project / '.tofu_env.json').is_file()


def test_dirty_checkout_never_pulled(installer):
    project, env = installer
    env['FAKE_DIRTY'] = '1'
    result = _run(project, env, '--no-launch')
    assert result.returncode == 0, result.stdout + result.stderr
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'status --porcelain' in calls and ' pull --ff-only' not in calls


def test_concurrent_install_refuses_before_any_manager_call(installer):
    project, env = installer
    lock = project.parent / '.my project.install-lock'
    lock.mkdir()
    result = _run(project, env, '--no-launch')
    assert result.returncode != 0
    assert 'Another install owns' in result.stdout
    assert lock.exists()  # Never delete a lock belonging to another writer.
    assert not Path(env['CALLS_FILE']).exists()


def test_explicit_port_and_whitespace_keys_roundtrip(installer, tmp_path):
    from tofu_dotenv import read_dotenv_values
    project, env = installer
    key = tmp_path / 'key'
    key.write_text('synthetic-secret&hash#value')
    with (project / '.env').open('a') as file:
        file.write('LLM_API_KEYS=old\nLLM_API_KEYS =older\n')
    result = _run(project, env, '--no-update', '--no-launch', '--port', '018677',
                  '--api-key-file', str(key))
    assert result.returncode == 2  # Six-digit port is rejected before mutations.
    result = _run(project, env, '--no-update', '--no-launch', '--port', '18677',
                  '--api-key-file', str(key))
    assert result.returncode == 0, result.stdout + result.stderr
    values = read_dotenv_values(project / '.env')
    assert values['PORT'] == '18677'
    assert values['LLM_API_KEYS'] == key.read_text()
    assert values['KEEP_ME'] == 'value#data'
    assert key.read_text() not in result.stdout + result.stderr


@pytest.mark.parametrize('failure', ['FAKE_START_RC', 'FAKE_HEALTH_RC'])
def test_agent_receipt_does_not_claim_ready_after_runtime_failure(installer, tmp_path, failure):
    project, env = installer
    # Test our launch and receipt flow, not host-global port occupancy.
    # The isolated installer copy's bind is controlled with a module seam.
    (project / 'socket.py').write_text('''
AF_INET=2
SOCK_STREAM=1
SOL_SOCKET=1
SO_REUSEADDR=2
class socket:
    def __init__(self,*a): pass
    def setsockopt(self,*a): pass
    def bind(self,*a): pass
    def close(self): pass
''')
    env[failure] = '7'
    receipt = tmp_path / 'failed.json'
    result = _run(project, env, '--no-update', '--port', '18677', '--result-json', str(receipt))
    assert result.returncode != 0, result.stdout + result.stderr
    data = json.loads(receipt.read_text())
    assert data['status'] == 'failed' and data['exit_code'] != 0
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'launch:18677' in calls
    if failure == 'FAKE_START_RC':
        assert 'health:' not in calls
    else:
        assert 'health:18677' in calls, result.stdout + result.stderr


@pytest.mark.parametrize('already_running', [False, True])
def test_ready_receipt_requires_health_without_restarting_existing_worker(installer, tmp_path, already_running):
    project, env = installer
    (project / 'socket.py').write_text('''
AF_INET=2
SOCK_STREAM=1
SOL_SOCKET=1
SO_REUSEADDR=2
class socket:
    def __init__(self,*a): pass
    def setsockopt(self,*a): pass
    def bind(self,*a):
        import os
        if os.environ.get('BUSY') == '1': raise OSError('occupied')
    def close(self): pass
''')
    if already_running:
        env['BUSY'] = '1'
        (project / 'serverctl.py').write_text(
            'print(\'{"ready": true, "applicationReachable": true, "port": 18677, "applicationUrl": "http://localhost:18677"}\')\n')
    receipt = tmp_path / 'ready.json'
    result = _run(project, env, '--no-update', '--port', '18677', '--result-json', str(receipt))
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(receipt.read_text())
    assert data['status'] == 'ready' and data['exit_code'] == 0
    assert data['url'] == 'http://localhost:18677'
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'health:18677' in calls
    assert ('launch:18677' in calls) is not already_running
    # The new candidate is owned; the caller-created .venv remains untouched.
    assert json.loads((project / '.tofu_env.json').read_text())['owned_by_tofu_install'] is True
    assert (project / ".venv/bin/python").is_file()


def test_replaced_home_and_missing_temp_directory_are_supported(installer, tmp_path):
    project, env = installer
    replacement_home = tmp_path / 'replacement-home'
    replacement_home.mkdir()
    env['HOME'] = str(replacement_home)
    env['TMPDIR'] = str(tmp_path / 'old-container-temp')
    result = _run(project, env, '--no-update', '--no-launch')
    assert result.returncode == 0, result.stdout + result.stderr
    marker = json.loads((project / '.tofu_env.json').read_text())
    assert Path(marker['env_prefix']).is_relative_to(project / '.tofu-envs')
    assert (project / '.venv/bin/python').is_file()
    assert 'Ignoring unavailable inherited TMPDIR' in result.stdout + result.stderr


def test_late_storage_failure_preserves_live_pointer_and_configuration(installer):
    project, env = installer
    old_marker = json.dumps({'backend': 'uv', 'python': str(project / '.venv/bin/python'),
                             'env_prefix': str(project / '.venv')})
    (project / '.tofu_env.json').write_text(old_marker)
    previous_configuration = (project / '.env').read_bytes()
    (project / 'lib/storage.py').write_text('class StorageSupervisor:\n    def __init__(self, **kw): raise RuntimeError("storage fault injection")\n')
    result = _run(project, env, '--no-update', '--no-launch')
    assert result.returncode != 0
    assert 'storage fault injection' in result.stdout
    assert (project / '.tofu_env.json').read_text() == old_marker
    assert (project / '.env').read_bytes() == previous_configuration
    assert (project / '.venv/bin/python').is_file()


def test_default_install_defers_chromium_and_uses_candidate_python(installer):
    project, env = installer
    result = _run(project, env, '--no-update', '--non-interactive', '--no-launch')
    assert result.returncode == 0, result.stdout + result.stderr
    calls = Path(env['CALLS_FILE']).read_text()
    assert 'playwright install' not in calls
    assert 'uv:venv' in calls
    binding = json.loads((project / '.tofu_env.json').read_text())
    assert binding['backend'] == 'uv'
    assert '.tofu-envs' in binding['python']
