"""Deployment proves a candidate daemon/library pair before publishing either."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from serverctl_pkg.native_engine import verify_library
from tests.deploy.test_deploy_tofudb_engine import _candidate, _deploy, _digest


pytestmark = pytest.mark.unit
SOURCE_SHA256 = 'a' * 64


@pytest.fixture
def engine_pair(tmp_path):
    compiler = shutil.which('cc')
    if compiler is None:
        pytest.skip('a C compiler is required for the real library identity port')
    identity = json.dumps({
        'format': 'tofu-db-build.v1',
        'source_sha256': SOURCE_SHA256,
        'embedded_allocator': 'mimalloc' if sys.platform == 'linux' else 'system',
    })
    candidate = _candidate(
        tmp_path,
        body=('if [ "$1" = "--build-info" ]; then\n'
              f"  echo '{identity}'\n"
              'else\n  echo "usage: tofu-db fake-engine"\nfi'),
    )

    def write_library(source=SOURCE_SHA256, allocator=int(sys.platform == 'linux')):
        library = candidate.with_name('libtofu_db.so')
        code = (
            '#include <stddef.h>\n#include <stdint.h>\n'
            '#include <string.h>\n#include <sys/types.h>\n'
            'ssize_t tofudb_embed_source_sha256(void *output, size_t capacity) {\n'
            '  if (capacity < 65) return -1;\n'
            f'  memcpy(output, "{source}", 65);\n'
            '  return 64;\n}\n'
            f'uint32_t tofudb_embed_allocator_kind(void) {{ return {allocator}; }}\n'
        )
        subprocess.run(
            [compiler, '-shared', '-fPIC', '-x', 'c', '-', '-o', str(library)],
            input=code, text=True, capture_output=True, timeout=30, check=True,
        )
        return library

    return candidate, write_library


def test_deploy_publishes_a_matching_library_pair(tmp_path, engine_pair):
    candidate, write_library = engine_pair
    library = write_library()

    result = _deploy(tmp_path, candidate)

    assert result.returncode == 0, result.stderr
    destination = tmp_path / 'certified' / candidate.name
    receipt = json.loads(destination.with_name('build.json').read_text())
    assert receipt['source_sha256'] == SOURCE_SHA256
    assert receipt['library_sha256'] == _digest(library)
    verify_library(destination)


@pytest.mark.parametrize('source,allocator', [
    ('b' * 64, int(sys.platform == 'linux')),
    (SOURCE_SHA256, 1 - int(sys.platform == 'linux')),
])
def test_deploy_refuses_mismatched_library_before_replacing_engine(
        tmp_path, engine_pair, source, allocator):
    candidate, write_library = engine_pair
    write_library()
    assert _deploy(tmp_path, candidate).returncode == 0
    certified = tmp_path / 'certified'
    before = {path.name: path.read_bytes() for path in certified.iterdir()}
    write_library(source, allocator)

    result = _deploy(tmp_path, candidate)

    assert result.returncode == 1
    assert 'library' in result.stderr
    assert {path.name: path.read_bytes() for path in certified.iterdir()} == before


def test_deploy_refuses_an_unattestable_library_before_publication(tmp_path, engine_pair):
    candidate, _ = engine_pair
    candidate.with_name('libtofu_db.so').write_bytes(b'not a library')

    result = _deploy(tmp_path, candidate)

    assert result.returncode == 1
    assert 'library' in result.stderr
    assert not (tmp_path / 'certified' / candidate.name).exists()
