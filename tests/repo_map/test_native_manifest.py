"""Installed artifact snapshot ownership and selective environment projection."""
from collections.abc import Mapping
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


def test_environment_projection_keeps_exact_git_names_without_reading_secrets():
    module = pytest.importorskip('tofu_repo_kernel._process_discovery')
    values = {'HOME': '', 'PATH': '/fixture/bin', 'LANG': 'C',
              'GIT_DIR': '../metadata', 'GIT_CONFIG_COUNT': '2',
              'GIT_CONFIG_KEY_0': 'core.excludesFile', 'GIT_CONFIG_VALUE_0': 'ignore',
              'GIT_CONFIG_KEY_001': 'core.bare', 'GIT_CONFIG_VALUE_001': 'false'}
    denied = {'API_KEY', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'TOFU_STORAGE_TOKEN',
              'GIT_ASKPASS', 'GIT_CONFIG_KEY_', 'GIT_CONFIG_KEY_+1',
              'GIT_CONFIG_VALUE_1_0', 'GIT_CONFIG_KEY_\u0660'}
    class Source(Mapping):
        def __getitem__(self, key):
            assert key not in denied, 'unrelated environment value was read'
            return values[key]
        def __iter__(self):
            return iter([*values, *denied])
        def __len__(self):
            return len(values) + len(denied)
    assert module.discovery_environment(Source()) == values
    assert module.discovery_environment({}) == {}


@pytest.fixture
def private_package(tmp_path):
    package = pytest.importorskip('tofu_repo_kernel')
    target = tmp_path/'installed'
    shutil.copytree(Path(package.__file__).parent, target/'tofu_repo_kernel',
                    ignore=shutil.ignore_patterns('__pycache__'))
    return target


def run_fresh(private_package, script):
    launcher = shlex.split(os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable))
    result = subprocess.run([*launcher, '-c', script], capture_output=True, text=True,
        timeout=60, env=dict(os.environ, PYTHONPATH=str(private_package)+os.pathsep+
                             os.environ.get('PYTHONPATH', '')))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != 'linux', reason='sealed process discovery requires Linux')
def test_concurrent_artifact_loaders_share_one_manifest_snapshot(private_package):
    run_fresh(private_package, '''
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tofu_repo_kernel as package
original = Path.read_text
reads = []
def observed(self, *args, **kwargs):
    if self.name == 'build.json': reads.append(str(self))
    return original(self, *args, **kwargs)
Path.read_text = observed
loaders = [package.load_kernel, package.load_discovery, package.load_discovery_process]
try:
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda i: loaders[i % 3](), range(96)))
    assert len(reads) == 1
    for index in range(3):
        assert len({id(value) for value in results[index::3]}) == 1
    assert results[0].solve([0, 1], [0], [1.0], [1.0]) == [1.0]
finally:
    Path.read_text = original
    package.load_discovery_process().close()
''')


@pytest.mark.skipif(sys.platform != 'linux', reason='sealed process discovery requires Linux')
def test_manifest_snapshot_cannot_admit_replaced_artifact_bytes(private_package):
    run_fresh(private_package, '''
from pathlib import Path
import hashlib, json
import tofu_repo_kernel as package
root = Path(package.__file__).parent
manifest = root/'build.json'
record = json.loads(manifest.read_text())
package.load_kernel()
# Metadata changes cannot replace the build snapshot already selected by this
# process, but each not-yet-loaded artifact must still verify its actual bytes.
worker = root/record['discovery']['worker']['executable']
worker.write_bytes(b'changed executable')
record['discovery']['worker']['sha256'] = hashlib.sha256(worker.read_bytes()).hexdigest()
record['discovery']['library_sha256'] = '0'*64
manifest.write_text(json.dumps(record))
assert package.load_discovery() is not None
try:
    package.load_discovery_process()
except ValueError as error:
    assert 'digest differs' in str(error)
else:
    raise AssertionError('new metadata admitted different executable bytes')
''')


def test_invalid_base_record_does_not_poison_future_load(private_package):
    run_fresh(private_package, '''
from pathlib import Path
import json
import tofu_repo_kernel as package
manifest = Path(package.__file__).parent/'build.json'
original = manifest.read_text()
record = json.loads(original)
record['format'] = 'unsupported'
manifest.write_text(json.dumps(record))
try:
    package.load_kernel()
except ValueError as error:
    assert 'unsupported' in str(error)
else:
    raise AssertionError('invalid base record admitted')
manifest.write_text(original)
assert package.load_kernel().solve([0, 1], [0], [1.0], [1.0]) == [1.0]
''')
