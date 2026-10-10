"""Public repository-map semantics for multilingual and deeply nested sources."""
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import sys

import pytest

from lib.repo_map.api import generate_repo_map
from lib.repo_map.outline import scan

pytest.importorskip('grep_ast')
pytestmark = pytest.mark.unit


def test_javascript_order_signatures_and_ancestor_depth(tmp_path):
    source = ('class Outer {\n'
              '  method() {\n'
              '    function nested() { return 1; }\n'
              '  }\n'
              '}\n'
              'function top() {}\n')
    (tmp_path / 'module.js').write_text(source)
    tags = scan(str(tmp_path)).tags['module.js']
    assert [(tag.name, tag.kind, tag.line, tag.indent, tag.signature) for tag in tags] == [
        ('Outer', 'class', 1, 0, 'class Outer {'),
        ('method', 'def', 2, 2, 'method() {'),
        ('nested', 'def', 3, 3, 'function nested() { return 1; }'),
        ('top', 'def', 6, 0, 'function top() {}'),
    ]
    text, stats = generate_repo_map(str(tmp_path), max_tokens=2048)
    assert all(name in text for name in ('Outer', 'method', 'nested', 'top'))
    assert stats['files_shown'] == stats['files_total'] == 1


def test_parser_failure_does_not_hide_other_files(tmp_path):
    (tmp_path / 'broken.js').write_text('function unavailableGrammar() {}')
    (tmp_path / 'healthy.py').write_text('def healthy_symbol():\n    return 1\n')
    with patch('grep_ast.tsl.get_parser', side_effect=RuntimeError('grammar unavailable')), \
            patch('grep_ast.grep_ast.get_parser', side_effect=RuntimeError('grammar unavailable')):
        text, stats = generate_repo_map(str(tmp_path), max_tokens=2048)
    assert 'healthy_symbol' in text
    assert 'unavailableGrammar' not in text
    assert stats['files_total'] == stats['files_shown'] == 1


def test_invalid_parse_result_is_file_local(tmp_path):
    (tmp_path / 'broken.js').write_text('function unavailableGrammar() {}')
    (tmp_path / 'healthy.py').write_text('def healthy_symbol():\n    return 1\n')
    parser = SimpleNamespace(parse=lambda source: None)
    with patch('grep_ast.tsl.get_parser', return_value=parser), \
            patch('grep_ast.grep_ast.get_parser', return_value=parser):
        text, stats = generate_repo_map(str(tmp_path), max_tokens=2048)
    assert 'healthy_symbol' in text and 'unavailableGrammar' not in text
    assert stats['files_shown'] == 1


def test_missing_parser_dependency_keeps_python_map(tmp_path):
    (tmp_path / 'module.js').write_text('function unavailableDependency() {}')
    (tmp_path / 'healthy.py').write_text('def healthy_symbol():\n    return 1\n')
    with patch.dict(sys.modules, {'grep_ast.tsl': None}):
        text, stats = generate_repo_map(str(tmp_path), max_tokens=2048)
    assert 'healthy_symbol' in text and 'unavailableDependency' not in text
    assert stats['files_shown'] == 1


def test_deep_javascript_blocks_keep_definitions(tmp_path):
    # A valid file below the normal source-size admission threshold must not
    # disappear merely because syntax traversal exceeds Python recursion.
    source = 'function outer() {\n' + '{\n' * 1200 + 'function inner() {}\n' + '}\n' * 1200 + '}\n'
    (tmp_path / 'deep.js').write_text(source)
    tags = scan(str(tmp_path)).tags.get('deep.js', [])
    assert [(tag.name, tag.indent) for tag in tags] == [('outer', 0), ('inner', 1)]
    text, stats = generate_repo_map(str(tmp_path), max_tokens=2048)
    assert 'outer' in text and 'inner' in text
    assert stats['files_shown'] == 1


def test_concurrent_maps_keep_project_results_separate(tmp_path):
    roots = []
    for index in range(8):
        root = tmp_path / str(index)
        root.mkdir()
        (root / 'module.js').write_text(f'function projectSymbol{index}() {{ return {index}; }}')
        roots.append(root)

    def generate(index):
        text, stats = generate_repo_map(str(roots[index % len(roots)]), max_tokens=512)
        assert f'projectSymbol{index % len(roots)}' in text
        assert all(f'projectSymbol{other}' not in text for other in range(len(roots))
                   if other != index % len(roots))
        assert stats['files_shown'] == 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(generate, range(64)))
