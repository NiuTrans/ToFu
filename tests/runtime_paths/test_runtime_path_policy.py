"""Path resolution shares a startup decision and never clobbers probe names."""
import os
from pathlib import Path
import subprocess
import sys
import pytest

pytestmark = pytest.mark.unit


def test_frozen_probe_preserves_existing_user_file(tmp_path):
    marker = tmp_path / '.tofu_write_probe'
    marker.write_text('user content: do not truncate or delete')
    env = dict(os.environ)
    env.pop('TOFU_DATA_DIR', None)
    result = subprocess.run([sys.executable, '-c', '''
import sys
sys.frozen = True
sys.executable = sys.argv[1]
import lib.runtime_paths
import lib.log
''', str(tmp_path / 'Tofu')], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert marker.exists(), 'startup removed an existing user file'
    assert marker.read_text() == 'user content: do not truncate or delete'


def test_readonly_resolution_import_does_not_create_directories(tmp_path):
    env = dict(os.environ, TOFU_DATA_DIR=str(tmp_path / 'missing'))
    result = subprocess.run([sys.executable, '-c', '''
import os
from runtime_path_policy import resolve_runtime_base
assert resolve_runtime_base(allow_probe=False) == os.environ['TOFU_DATA_DIR']
import debug.inspect_conversation
'''], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'missing').exists()


def test_startup_path_snapshot_is_shared_under_concurrent_imports(tmp_path):
    env = dict(os.environ)
    env.pop('TOFU_DATA_DIR', None)
    result = subprocess.run([sys.executable, '-c', '''
import sys, threading, time
from concurrent.futures import ThreadPoolExecutor
import runtime_path_policy as policy
sys.frozen = True
sys.executable = sys.argv[1]
count = 0
barrier = threading.Barrier(8)
def probe(path):
    global count
    count += 1
    time.sleep(0.02)
    return True
def resolve(_):
    barrier.wait()
    return policy.get_runtime_base()
policy._probe_writable = probe
with ThreadPoolExecutor(max_workers=8) as workers:
    values = list(workers.map(resolve, range(8)))
assert count == 1, count
assert len(set(values)) == 1
import lib.runtime_paths as paths
import lib.log as logs
assert paths._BASE == values[0]
assert logs.LOG_DIR == values[0] + '/logs'
assert count == 1
''', str(tmp_path / 'Tofu')], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_frozen_readonly_resolution_requires_explicit_root(monkeypatch, tmp_path):
    import runtime_path_policy as policy
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    def forbidden(_path):
        pytest.fail('read-only path resolution attempted a writable probe')
    monkeypatch.setattr(policy, '_probe_writable', forbidden)
    with pytest.raises(ValueError, match='explicit TOFU_DATA_DIR'):
        policy.resolve_runtime_base({}, allow_probe=False)
    assert policy.resolve_runtime_base({'TOFU_DATA_DIR': str(tmp_path / 'data')},
                                       allow_probe=False) == str(tmp_path)
