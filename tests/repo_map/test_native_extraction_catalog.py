"""Pinned native catalog parity, no eager parser imports, and actual publication."""
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def artifacts(monkeypatch):
    library = os.environ.get('TOFU_NATIVE_EXTRACTION_LIBRARY')
    grammars = os.environ.get('TOFU_NATIVE_EXTRACTION_GRAMMARS')
    if not library or not grammars:
        pytest.skip('explicit v2 native library and grammar directory required')
    monkeypatch.syspath_prepend(str(ROOT / 'benchmarks/repository_index'))
    return Path(library), Path(grammars)


def test_generated_catalog_matches_exact_installed_upstream():
    try:
        distribution = importlib.metadata.distribution('grep-ast')
    except importlib.metadata.PackageNotFoundError:
        pytest.skip('pinned upstream package required for drift check')
    path = ROOT / 'packages/tofu-repo-kernel/rust/extraction/generate_catalog.py'
    spec = importlib.util.spec_from_file_location('tofu_catalog_generator_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert distribution.version == module.UPSTREAM_VERSION
    for output, expected in module.generate(Path(distribution.locate_file('grep_ast/parsers.py')), ROOT / 'lib/repo_map/_native_catalog.py').items():
        assert output.read_text() == expected, str(output)


def test_native_catalog_matches_tags_and_preserves_v1(artifacts):
    pytest.importorskip('tree_sitter_language_pack')
    from check_native_extraction_catalog import check
    result = check(*artifacts)
    assert result['v1_requires_language'] and result['reversed_batch_parity']
    assert result['unknown_file_local']


def test_native_catalog_preserves_actual_tool_publication(artifacts):
    pytest.importorskip('tree_sitter_language_pack')
    pytest.importorskip('tofu_repo_kernel')
    from check_native_extraction_tool import check
    from native_extraction_library import NativeCatalogBatchScanner
    result = check(*artifacts, scanner_type=NativeCatalogBatchScanner)
    assert result['joined_cleanup'] and result['foreign_owner_refused']
    assert result['foreign_root_refused'] and result['admission_and_file_local_failure_parity']


def test_native_catalog_scans_without_python_parser_packages(artifacts, tmp_path):
    (tmp_path / 'module.js').write_text('function nativeCatalogSymbol() { return 1; }')
    code = '''
import importlib.abc, pathlib, sys
class RejectPythonParserImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('grep_ast', 'tree_sitter_language_pack'):
            raise ImportError('Python parser package forbidden by this check')
sys.meta_path.insert(0, RejectPythonParserImports())
sys.path.insert(0, str(pathlib.Path.cwd() / 'benchmarks/repository_index'))
from native_extraction_library import NativeCatalogBatchScanner
scanner = NativeCatalogBatchScanner(sys.argv[1], sys.argv[2])
result = scanner(sys.argv[3])
assert 'module.js' in result.tags, (sys.argv[3], result.files, result.tags, scanner.calls)
assert result.tags['module.js'][0].name == 'nativeCatalogSymbol'
assert scanner.calls == 1
assert not any(name.split('.')[0] in ('grep_ast', 'tree_sitter_language_pack') for name in sys.modules)
'''
    run = subprocess.run([os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), '-c', code,
                          str(artifacts[0]), str(artifacts[1]), str(tmp_path)],
                         cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
