"""Borrowed public scan ports preserve output, isolation and caller lifecycle."""

# Synthetic repository/package fixtures, never shipped-source anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = ('tests/test_module.py',)
from concurrent.futures import ThreadPoolExecutor
import os

import pytest

from lib.repo_map.api import generate_repo_map
from lib.repo_map.outline import scan

pytestmark = pytest.mark.unit


def test_borrowed_scan_options_and_output_match_canonical(tmp_path):
    (tmp_path/'source').mkdir(); (tmp_path/'tests').mkdir()
    (tmp_path/'source/module.py').write_text('def selected():\n    return reference\n')
    (tmp_path/'tests/test_module.py').write_text('def test_selected():\n    return selected\n')
    calls = []
    class FalseyBorrowedScanner:
        closed = False
        def __bool__(self):
            return False
        def __call__(self, root, **options):
            calls.append((root, options))
            return scan(root, **options)
        def close(self):
            self.closed = True
    borrowed = FalseyBorrowedScanner()
    for rank in (False, True):
        for include_tests in (False, True):
            options = dict(path='source', focus='module', with_rank=rank,
                           include_tests=include_tests, chat_rel_paths={'source/module.py'})
            assert generate_repo_map(tmp_path/'source'/'..', scanner=borrowed, **options) == generate_repo_map(tmp_path, **options)
            assert calls[-1] == (os.path.abspath(tmp_path), {'path':'source','focus':'module',
                                   'include_refs':rank,'include_tests':include_tests})
            assert not borrowed.closed
    assert len(calls) == 4
    borrowed.close(); assert borrowed.closed


def test_port_failure_preserves_exception_and_caller_ownership(tmp_path):
    failure = PermissionError('foreign scope owner')
    class DeniedScanner:
        closed = False
        def __call__(self, root, **options):
            raise failure
        def close(self):
            self.closed = True
    port = DeniedScanner()
    with pytest.raises(PermissionError) as captured:
        generate_repo_map(tmp_path, scanner=port)
    assert captured.value is failure and not port.closed


def test_parallel_ports_do_not_replace_module_scanner(tmp_path):
    roots = [tmp_path/name for name in ('left','right')]
    for root in roots:
        root.mkdir(); (root/'module.py').write_text('def '+root.name+'_symbol():\n    return 1\n')
    def invoke(root):
        expected = os.path.abspath(root)
        def authorized(actual, **options):
            if actual != expected:
                raise PermissionError('foreign root')
            return scan(actual, **options)
        return generate_repo_map(root, scanner=authorized)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(invoke, roots))
    for root, result in zip(roots, results):
        assert result == generate_repo_map(root)
        assert root.name+'_symbol' in result[0]
        assert ('right' if root.name=='left' else 'left')+'_symbol' not in result[0]


def test_invalid_root_never_calls_borrowed_scanner(tmp_path):
    calls = []
    def scanner(root, **options):
        calls.append(root)
        return scan(root, **options)
    with pytest.raises(NotADirectoryError):
        generate_repo_map(tmp_path/'missing', scanner=scanner)
    assert calls == []
