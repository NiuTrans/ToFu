"""Optional native extraction stays unloaded until use and binds atomically."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scanner(monkeypatch):
    library = os.environ.get('TOFU_NATIVE_EXTRACTION_LIBRARY')
    grammars = os.environ.get('TOFU_NATIVE_EXTRACTION_GRAMMARS')
    if not library or not grammars:
        pytest.skip('explicit library and grammar directory required')
    monkeypatch.syspath_prepend(str(ROOT / 'benchmarks/repository_index'))
    from native_extraction_library import NativeCatalogBatchScanner
    return NativeCatalogBatchScanner(library, grammars)


def payload():
    return json.dumps(dict(format='tofu.repository-extraction/v2', files=[
        dict(rel_path='sample.js', source='function lazySymbol() {}')])).encode()


def assert_tags(scanner):
    result = json.loads(scanner.invoke(payload(), 4096))
    assert result['files'][0]['tags'][0]['name'] == 'lazySymbol'


def test_concurrent_first_use_publishes_one_complete_binding(scanner, monkeypatch):
    original = ctypes.CDLL
    calls = []
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(ctypes, 'CDLL', counted)
    start = Barrier(8)
    def use(_):
        start.wait(timeout=10)
        assert_tags(scanner)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(use, range(8)))
    assert calls == [str(scanner.binary)]


@pytest.mark.parametrize('failure', [OSError('injected load failure'), KeyboardInterrupt('injected interruption')])
def test_failed_or_interrupted_binding_can_retry(scanner, monkeypatch, failure):
    original = ctypes.CDLL
    calls = []
    def interrupted(path):
        calls.append(path)
        if len(calls) == 1:
            raise failure
        return original(path)
    monkeypatch.setattr(ctypes, 'CDLL', interrupted)
    with pytest.raises(type(failure)):
        assert_tags(scanner)
    assert_tags(scanner)
    assert_tags(scanner)
    assert len(calls) == 2


def test_python_only_scan_needs_no_native_bind_or_catalog_import(scanner, tmp_path):
    (tmp_path / 'module.py').write_text('def pythonOnly():\n    return 1\n')
    (tmp_path / 'notes.txt').write_text('not source')
    code = '''
import ctypes, importlib.abc, pathlib, sys
original_load = ctypes.CDLL
def forbidden_load(path, *args, **kwargs):
    if path == sys.argv[1]:
        raise AssertionError('Python-only scan attempted native extraction load')
    return original_load(path, *args, **kwargs)
ctypes.CDLL = forbidden_load
class RejectOptionalImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('grep_ast', 'tree_sitter_language_pack', 'native_catalog_extensions'):
            raise ImportError('optional parser/catalog import forbidden')
sys.meta_path.insert(0, RejectOptionalImports())
sys.path.insert(0, str(pathlib.Path.cwd() / 'benchmarks/repository_index'))
from native_extraction_library import NativeCatalogBatchScanner
scanner = NativeCatalogBatchScanner(sys.argv[1], sys.argv[2])
result = scanner(sys.argv[3])
assert result.tags['module.py'][0].name == 'pythonOnly'
assert scanner.calls == 0
assert not any(name.split('.')[0] in ('grep_ast', 'tree_sitter_language_pack', 'native_catalog_extensions') for name in sys.modules)
'''
    run = subprocess.run([os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), '-c', code,
                          str(scanner.binary), str(scanner.grammar_directory), str(tmp_path)],
                         cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
