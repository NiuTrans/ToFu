"""Contract tests for lib/repo_map + tools/repo_map.py."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lib.repo_map.api import generate_repo_map
from lib.repo_map.outline import (

    _extract_python_tags,
    _is_test_path,
    scan,
)

# Intentional references to paths that do not (and must not)
# exist in the real repo — negative-case fixture anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test_foo.py', 'lib/storage/lock.py', 'lib/testing_helpers.py', 'lib/tests/util.py', 'tests/test_main.py'}

pytestmark = pytest.mark.unit


def _write(root: Path, rel_path: str, body: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding='utf-8')


# ── Python AST extractor ────────────────────────────────────────────────────

def test_python_extractor_finds_classes_and_functions(tmp_path):
    src = '''
    import os

    CONSTANT = 1
    _PRIVATE = 2

    def top_level(a, b):
        pass

    class Foo:
        def method(self, x):
            pass

        class Nested:
            pass
    '''
    tags = _extract_python_tags('m.py', textwrap.dedent(src))
    names = [t.name for t in tags]
    assert 'top_level' in names
    assert 'Foo' in names
    assert 'method' in names
    assert 'Nested' in names
    assert 'CONSTANT' in names        # public top-level constants are anchors
    assert '_PRIVATE' not in names    # private scratch is excluded
    # Signature contains the source line
    foo = next(t for t in tags if t.name == 'Foo')
    assert foo.signature.startswith('class Foo')
    assert foo.line > 0


def test_python_extractor_survives_syntax_errors(tmp_path):
    tags = _extract_python_tags('bad.py', 'def broken(:\n')
    assert tags == []


def test_python_signatures_preserve_ast_source_segments():
    """Physical lines, byte columns and statement ends preserve signatures."""
    import ast
    source = ('UNICODE = "中文"; OTHER = 2\r\n'
              '@decorate\r\n'
              'def example(\r\n    arg="中文",\r\n):\r\n'
              '    class Nested:\r\n        def method(self): return arg\r\n')
    tags = _extract_python_tags('unicode.py', source)
    expected = {}
    for node in ast.walk(ast.parse(source)):
        name = getattr(node, 'name', None)
        if isinstance(node, ast.Assign):
            name = node.targets[0].id
        if name:
            expected[name] = ast.get_source_segment(source, node).splitlines()[0].strip()[:160]
    assert {tag.name: tag.signature for tag in tags} == expected


def test_large_sources_keep_references_without_extracting_definitions(tmp_path):
    source = 'from engine import ImportantEngine\n' + '# filler\n' * 70000
    _write(tmp_path, 'large.py', source)
    result = scan(str(tmp_path))
    assert not result.tags.get('large.py')
    assert result.references['ImportantEngine'] == {'large.py'}


@pytest.mark.parametrize('personalized', [False, True])
def test_rank_preserves_multigraph_multiplicity(personalized, monkeypatch):
    import networkx as nx
    from networkx.algorithms.link_analysis.pagerank_alg import _pagerank_python
    from lib.repo_map.outline import RepoMap
    from lib.repo_map.rank import sumbd
    repo = RepoMap(root='fixture')
    repo.definitions = {'Engine': {'a.py', 'b.py', 'c.py'},
                        'Helper': {'c.py', 'd.py'}, 'Leaf': {'e.py'}}
    repo.references = {'Engine': {'b.py', 'd.py', 'e.py', 'f.py'},
                       'Helper': {'a.py', 'e.py'}, 'Leaf': {'a.py', 'c.py'}}
    graph = nx.MultiDiGraph()
    for name, definers in repo.definitions.items():
        for definer in definers:
            for referencer in repo.references[name] - definers:
                graph.add_edge(f'file:{referencer}', f'ident:{name}')
                graph.add_edge(f'ident:{name}', f'file:{definer}')
    personal = {'file:a.py': 50.0} if personalized else None
    expected = {node[5:]: value for node, value in
                _pagerank_python(graph, personalization=personal).items()
                if node.startswith('file:')}
    # Exercise the production graph assembly against an independent PageRank
    # oracle even in slim installations without optional SciPy.
    monkeypatch.setattr(nx, 'pagerank', _pagerank_python)
    assert sumbd(repo, {'a.py'} if personalized else None) == pytest.approx(expected)


# ── Test-path detection ──────────────────────────────────────────────────────

@pytest.mark.parametrize(('path', 'expected'), [
    ('tests/test_foo.py', True),
    ('test/foo.py', True),
    ('lib/tests/util.py', True),
    ('conftest.py', True),
    ('foo_test.py', True),
    ('test_bar.py', True),
    ('x.test.ts', True),
    ('x.spec.ts', True),
    ('lib/storage/lock.py', False),
    ('lib/testing_helpers.py', False),
    ('contest.py', False),
    ('tests.py', False),
])
def test_is_test_path(path, expected):
    assert _is_test_path(path) is expected


# ── End-to-end map generation ────────────────────────────────────────────────

def test_end_to_end_orders_by_references(tmp_path):
    """The file every other file imports must rank before the leaf."""
    _write(tmp_path, 'core/engine.py', '''
        class Engine:
            def start(self): pass
        def make_engine(): return Engine()
    ''')
    _write(tmp_path, 'api/server.py', '''
        from core.engine import make_engine
        def create_app(): return make_engine()
    ''')
    _write(tmp_path, 'worker/job.py', '''
        from core.engine import Engine
        def run(): return Engine().start()
    ''')
    _write(tmp_path, 'util/logging.py', '''
        import logging
        def get_logger(): return logging.getLogger()
    ''')

    text, stats = generate_repo_map(str(tmp_path), max_tokens=2048,
                                    include_tests=False)
    # engine.py must be listed before server.py / job.py (referenced by both)
    engine_idx = text.index('core/engine.py')
    server_idx = text.index('api/server.py')
    job_idx = text.index('worker/job.py')
    assert engine_idx < server_idx < job_idx or engine_idx < job_idx
    assert stats['files_shown'] == stats['files_total']


def test_include_tests_filters_test_files(tmp_path):
    _write(tmp_path, 'src/main.py', 'def main(): pass')
    _write(tmp_path, 'tests/test_main.py', '''
        from src.main import main
        def test_main(): main()
    ''')
    text_default, stats_default = generate_repo_map(
        str(tmp_path), max_tokens=2048, include_tests=False)
    assert 'test_main.py' not in text_default

    text_with, stats_with = generate_repo_map(
        str(tmp_path), max_tokens=2048, include_tests=True)
    assert 'test_main.py' in text_with
    assert stats_with['files_total'] > stats_default['files_total']


def test_focus_filters_files(tmp_path):
    _write(tmp_path, 'storage/lock.py', 'def acquire(): pass')
    _write(tmp_path, 'storage/cache.py', 'def get(): pass')
    _write(tmp_path, 'ui/button.py', 'def render(): pass')
    text, _ = generate_repo_map(str(tmp_path), focus='storage',
                                max_tokens=2048, with_rank=False)
    assert 'storage/lock.py' in text
    assert 'ui/button.py' not in text


def test_path_scopes_to_subdirectory(tmp_path):
    _write(tmp_path, 'core/a.py', 'def a(): pass')
    _write(tmp_path, 'ui/b.py', 'def b(): pass')
    text, _ = generate_repo_map(str(tmp_path), path='core',
                                max_tokens=2048, with_rank=False)
    assert 'core/a.py' in text
    assert 'ui/b.py' not in text


def test_untracked_files_are_mapped(tmp_path):
    """git ls-files --others must catch files created before `git add`."""
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    _write(tmp_path, 'new_file.py', 'def newly_created(): pass')
    text, _ = generate_repo_map(str(tmp_path), max_tokens=1024)
    assert 'newly_created' in text


def test_generated_files_are_skipped(tmp_path):
    _write(tmp_path, 'app.generated.js',
           'function big() {' + 'x = 1; '.join([''] * 500) + '}')
    _write(tmp_path, 'src/main.py', 'def main(): pass')
    text, _ = generate_repo_map(str(tmp_path), max_tokens=2048,
                                with_rank=False)
    assert 'app.generated.js' not in text
    assert 'main.py' in text


def test_max_tokens_bounds_output(tmp_path):
    for i in range(30):
        _write(tmp_path, f'pkg/mod{i}.py',
               f'def func_{i}_a(): pass\ndef func_{i}_b(): pass\n'
               f'def func_{i}_c(): pass\ndef func_{i}_d(): pass\n')
    text, stats = generate_repo_map(str(tmp_path), max_tokens=512,
                                    with_rank=False)
    # 512 tokens ≈ 1536 chars; allow a small slack for the footer.
    assert len(text) <= 3000
    assert stats['omitted'] > 0
    assert 'more files' in text or 'more file ' in text


def test_empty_repo_returns_clean_empty(tmp_path):
    text, stats = generate_repo_map(str(tmp_path), max_tokens=1024)
    assert stats['files_total'] == 0
    assert 'no files' in text.lower() or text.strip() == ''


# ── Public API end-to-end (the CLI wrapper the suite once invoked is gone;
# the shipped entry point is lib.repo_map.api.generate_repo_map) ─────────────

def test_api_end_to_end(tmp_path):
    _write(tmp_path, 'main.py', 'def entry(): pass')
    _text, stats = generate_repo_map(str(tmp_path), with_rank=False)
    assert stats['files_total'] == 1


def test_api_rejects_missing_root(tmp_path):
    with pytest.raises((FileNotFoundError, NotADirectoryError)):
        generate_repo_map(str(tmp_path / 'nope'), with_rank=False)
