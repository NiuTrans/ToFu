"""Explicit native extraction artifact against canonical installed grammars."""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def native_library_artifacts():
    library = os.environ.get('TOFU_NATIVE_EXTRACTION_LIBRARY')
    directory = os.environ.get('TOFU_NATIVE_EXTRACTION_GRAMMARS')
    if not library or not directory:
        pytest.skip('explicit native library and grammar artifacts required')
    pytest.importorskip('tree_sitter_language_pack')
    return Path(library), Path(directory)


@pytest.fixture
def collector_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'benchmarks/repository_index'))


def test_native_library_has_exact_tags_and_atomic_caller_output(native_library_artifacts, collector_imports):
    from check_native_extraction_library import check
    result = check(*native_library_artifacts)
    assert result['reversed_batch_parity'] and result['refused_output_unchanged']
    assert result['file_local_errors']


def test_native_library_preserves_actual_tool_publication(native_library_artifacts, collector_imports):
    pytest.importorskip('tofu_repo_kernel')
    from check_native_extraction_tool import check
    from native_extraction_library import NativeLibraryBatchScanner
    result = check(*native_library_artifacts, scanner_type=NativeLibraryBatchScanner)
    assert result['joined_cleanup'] and result['foreign_root_refused']
    assert result['foreign_owner_refused'] and result['admission_and_file_local_failure_parity']


def test_native_extraction_matches_canonical_tags_and_owned_failures():
    binary = os.environ.get('TOFU_NATIVE_EXTRACTION_BINARY')
    directory = os.environ.get('TOFU_NATIVE_EXTRACTION_GRAMMARS')
    if not binary or not directory:
        pytest.skip('explicit native extraction candidate and grammar artifacts required')
    pytest.importorskip('tree_sitter_language_pack')
    from benchmarks.repository_index.check_native_extraction import check
    result = check(Path(binary), Path(directory))
    assert result['exact_tag_fields'] and result['per_file_errors']
    assert result['reversed_batch_parity'] and result['malformed_and_over_budget_output_withheld']


def test_native_extraction_preserves_actual_tool_publication():
    binary = os.environ.get('TOFU_NATIVE_EXTRACTION_BINARY')
    directory = os.environ.get('TOFU_NATIVE_EXTRACTION_GRAMMARS')
    if not binary or not directory:
        pytest.skip('explicit native extraction candidate and grammar artifacts required')
    pytest.importorskip('tree_sitter_language_pack')
    pytest.importorskip('tofu_repo_kernel')
    # These retained standalone collectors share their documented script imports.
    import sys
    from benchmarks.repository_index import check_native_extraction
    path = str(Path(check_native_extraction.__file__).parent)
    sys.path.insert(0, path)
    try:
        from benchmarks.repository_index.check_native_extraction_tool import check
        result = check(Path(binary), Path(directory))
    finally:
        sys.path.remove(path)
    assert result['joined_cleanup'] and result['foreign_root_refused']
    assert result['foreign_owner_refused']
