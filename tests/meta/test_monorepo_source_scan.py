"""Monorepo checks observe one current index without executing inspected code."""
from __future__ import annotations

import importlib.util
import ast
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
# Each path below is created only inside the private Git fixture, not the repo.
_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/alias.py', 'lib/broken.py', 'lib/expressions.py', 'lib/late.py',
    'lib/literals.py', 'lib/nested.py', 'lib/working.py',
}


@pytest.fixture
def source_guard(tmp_path, monkeypatch):
    """A complete private monorepo with minimal package/SDK metadata."""
    files = {
        'VERSION': '0.1.0\n',
        'pyproject.toml': '''[project]
name = "tofu-agent"
version = "0.1.0"
[tool.uv.workspace]
members = ["packages/tofu-search", "plugins/tofu-trading"]
exclude = ["packages/tofu-overleaf-mcp"]
[tool.uv.sources]
tofu-search = { workspace = true }
''',
        'uv.lock': '[[package]]\nname = "tofu-agent"\nversion = "0.1.0"\nsource = { editable = "." }\n',
        'clients/python/pyproject.toml': '[project]\nversion = "0.1.0"\n',
        'clients/python/tofu_sdk/_version.py': '__version__ = "0.1.0"\n',
        'clients/typescript/package.json': json.dumps({'version': '0.1.0'}),
        'clients/typescript/package-lock.json': json.dumps({'version': '0.1.0', 'packages': {'': {'version': '0.1.0'}}}),
        'clients/typescript/src/version.ts': 'export const VERSION = "0.1.0";\n',
        'packages/tofu-search/pyproject.toml': '[project]\nname = "tofu-search"\nversion = "0.1.0"\n',
        'packages/tofu-search/tofu_search/__init__.py': 'raise RuntimeError("source must never execute")\n',
        'packages/tofu-overleaf-mcp/pyproject.toml': '[project]\nname = "tofu-overleaf-mcp"\nversion = "0.1.0"\n',
        'packages/tofu-overleaf-mcp/tofu_overleaf_mcp/__init__.py': 'raise RuntimeError("source must never execute")\n',
        'plugins/tofu-trading/pyproject.toml': '''[project]
name = "tofu-trading"
version = "0.2.1"
dependencies = ["tofu-agent>=0.1,<0.2"]
[tool.uv.sources]
tofu-agent = { workspace = true }
''',
        'plugins/tofu-trading/tofu_trading/__init__.py': '__version__ = "0.2.1"\n',
        'scripts/check_monorepo.py': '# Private fixture entrypoint, never executed.\n',
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True)
    spec = importlib.util.spec_from_file_location('monorepo_source_guard', ROOT / 'scripts/check_monorepo.py')
    guard = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, guard)
    spec.loader.exec_module(guard)
    guard.ROOT = tmp_path
    guard.SEARCH_ROOT = tmp_path / 'packages/tofu-search'
    guard.TRADING_ROOT = tmp_path / 'plugins/tofu-trading'
    guard.OVERLEAF_ROOT = tmp_path / 'packages/tofu-overleaf-mcp'
    guard.__file__ = str(tmp_path / 'scripts/check_monorepo.py')
    return guard


def test_successive_checks_observe_new_tracked_core_files(source_guard):
    guard = source_guard
    assert guard.check() == []
    source = guard.ROOT / 'lib/late.py'
    source.parent.mkdir()
    source.write_text('import tofu_trading\n')
    assert guard.check() == []  # Untracked work is outside the committed-source guard.
    subprocess.run(['git', 'add', 'lib/late.py'], cwd=guard.ROOT, check=True)
    assert guard.check() == ['lib/late.py imports plugin tofu_trading']


@pytest.mark.parametrize('filename', ['line\nbreak.md', '冒号:name\nnext.MD'])
def test_retired_reference_reports_a_tracked_filename_containing_newlines(
        source_guard, filename):
    guard = source_guard
    reference = '../tofu-' + 'search'
    (guard.ROOT / filename).write_text(f'  {reference}  \n')
    subprocess.run(['git', 'add', '--', filename], cwd=guard.ROOT, check=True)

    assert guard.check() == [
        f"retired sibling path {reference!r}: {filename}:1:{reference}"]


def test_retired_references_share_one_scan_of_current_working_content(
        source_guard, monkeypatch):
    guard = source_guard
    search, trading = '../tofu-' + 'search', '../tofu-' + 'trade'
    filename = 'references\nworking.MD'
    source = guard.ROOT / filename
    source.write_text('staged content has no retired paths\n')
    subprocess.run(['git', 'add', '--', filename], cwd=guard.ROOT, check=True)
    shared_line = f'{search} and {trading}'
    source.write_text(f'{search}\n  {shared_line}  \n')
    (guard.ROOT / 'untracked.md').write_text(trading)
    run, scans = subprocess.run, []

    def observe(command, *args, **kwargs):
        if command[:2] == ['git', 'grep'] and '-n' in command:
            scans.append(tuple(command))
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', observe)
    assert guard.check() == [
        f"retired sibling path {search!r}: {filename}:1:{search}; {filename}:2:{shared_line}",
        f"retired sibling path {trading!r}: {filename}:2:{shared_line}",
    ]
    assert len(scans) == 1, 'all retired paths inspect the same source set once'
    source.write_text(f'{trading}\n')
    assert guard.check() == [f"retired sibling path {trading!r}: {filename}:1:{trading}"]
    assert len(scans) == 2, 'a new invocation must inspect current worktree content'


def test_retired_reference_diagnostics_remain_independent(source_guard):
    guard = source_guard
    search, trading = '../tofu-' + 'search', '../tofu-' + 'trade'
    filename = 'many.md'
    (guard.ROOT / filename).write_text(f'{search}\n' * 7 + f'{trading}\n')
    subprocess.run(['git', 'add', '--', filename], cwd=guard.ROOT, check=True)
    search_hits = '; '.join(f'{filename}:{line}:{search}' for line in range(1, 6))
    assert guard.check() == [
        f"retired sibling path {search!r}: {search_hits}",
        f"retired sibling path {trading!r}: {filename}:8:{trading}",
    ]


def test_successive_checks_do_not_keep_removed_search_sources(source_guard):
    guard = source_guard
    relative = 'packages/tofu-search/tofu_search/removed.py'
    source = guard.ROOT / relative
    source.write_text('import lib\n')
    subprocess.run(['git', 'add', relative], cwd=guard.ROOT, check=True)
    assert guard.check() == [f"{relative} imports host roots ['lib']"]
    source.unlink()
    subprocess.run(['git', 'update-index', '--force-remove', relative], cwd=guard.ROOT, check=True)
    assert guard.check() == []


def test_guard_reads_one_index_snapshot_per_invocation(source_guard, monkeypatch):
    guard = source_guard
    run = subprocess.run
    reads = []

    def observe(command, *args, **kwargs):
        if command[:2] == ['git', 'ls-files']:
            reads.append(tuple(command))
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', observe)
    assert guard.check() == []
    assert len(reads) == 1
    assert guard.check() == []
    assert len(reads) == 2


_IMPORT_CONTEXTS = [
    '{body}',
    'def function():\n{body}',
    'async def function():\n{body}',
    'class Container:\n{body}',
    'if False:\n{body}',
    'if True:\n    pass\nelse:\n{body}',
    'for item in ():\n{body}',
    'for item in ():\n    pass\nelse:\n{body}',
    'while False:\n{body}',
    'while False:\n    pass\nelse:\n{body}',
    'with resource():\n{body}',
    'async def function():\n    async for item in stream:\n{body}',
    'async def function():\n    async with resource():\n{body}',
    'try:\n{body}\nexcept Exception:\n    pass',
    'try:\n    pass\nexcept Exception:\n{body}',
    'try:\n    pass\nexcept Exception:\n    pass\nelse:\n{body}',
    'try:\n    pass\nfinally:\n{body}',
    'match value:\n    case {"key": item} if item:\n{body}',
    'class Container:\n    async def function(self):\n        try:\n            pass\n        except Exception:\n{body}',
]
if sys.version_info >= (3, 11):
    _IMPORT_CONTEXTS.append('try:\n    pass\nexcept* Exception:\n{body}')


def _nested_import(context, statement):
    prefix = context.split('{body}')[0]
    preceding = prefix.rstrip().splitlines()
    indent = (len(preceding[-1]) - len(preceding[-1].lstrip()) + 4) if preceding else 0
    return context.replace('{body}', textwrap.indent(statement, ' ' * indent)) + '\n'


@pytest.mark.parametrize('context', _IMPORT_CONTEXTS)
@pytest.mark.parametrize('owner', ['core', 'script'])
def test_imports_in_every_statement_scope_remain_visible(source_guard, context, owner):
    guard = source_guard
    if owner == 'core':
        relative = 'lib/nested.py'
        statement = 'import tofu_trading as optional_plugin\nfrom tofu_trading import web'
        expected = ['lib/nested.py imports plugin tofu_trading']
    else:
        relative = 'scripts/nested.py'
        statement = 'import scripts.required as required\nfrom scripts import required'
        expected = ['declared build script is missing: scripts/required.py']
    source = guard.ROOT / relative
    source.parent.mkdir(exist_ok=True)
    source.write_text(_nested_import(context, statement))
    subprocess.run(['git', 'add', relative], cwd=guard.ROOT, check=True)
    assert guard.check() == expected


def test_import_scan_does_not_expand_expression_payloads(source_guard, monkeypatch):
    guard = source_guard
    source = guard.ROOT / 'lib/expressions.py'
    source.parent.mkdir()
    payload = ','.join(f'call({i}, key=({i}+1), attr=object.value)' for i in range(500))
    source.write_text(f'payload = [{payload}]\nif False:\n    import tofu_trading\n')
    subprocess.run(['git', 'add', 'lib/expressions.py'], cwd=guard.ROOT, check=True)
    children = ast.iter_child_nodes
    expanded_expressions = []

    def observe(node):
        if isinstance(node, ast.expr):
            expanded_expressions.append(type(node).__name__)
        return children(node)

    monkeypatch.setattr(ast, 'iter_child_nodes', observe)
    assert guard.check() == ['lib/expressions.py imports plugin tofu_trading']
    assert not expanded_expressions, 'Import discovery must not traverse expression payloads'


def test_working_copy_imports_override_the_staged_content(source_guard):
    guard = source_guard
    source = guard.ROOT / 'lib/working.py'
    source.parent.mkdir()
    source.write_text('import math\n')
    subprocess.run(['git', 'add', 'lib/working.py'], cwd=guard.ROOT, check=True)
    source.write_text('import tofu_trading\n')
    assert guard.check() == ['lib/working.py imports plugin tofu_trading']


def test_tracked_symlink_imports_are_read_from_the_target(source_guard):
    guard = source_guard
    target = guard.ROOT / 'untracked_target.py'
    target.write_text('import tofu_trading\n')
    source = guard.ROOT / 'lib/alias.py'
    source.parent.mkdir()
    source.symlink_to('../untracked_target.py')
    subprocess.run(['git', 'add', 'lib/alias.py'], cwd=guard.ROOT, check=True)
    assert guard.check() == ['lib/alias.py imports plugin tofu_trading']


def test_literal_mentions_are_data_not_imports(source_guard):
    guard = source_guard
    source = guard.ROOT / 'lib/literals.py'
    source.parent.mkdir()
    source.write_text('"""import tofu_trading"""\nvalue = "from tofu_trading import web"\n')
    subprocess.run(['git', 'add', 'lib/literals.py'], cwd=guard.ROOT, check=True)
    assert guard.check() == []


@pytest.mark.parametrize('content,error', [
    (b'import tofu_trading\ndef broken:', SyntaxError),
    (b'import tofu_trading\n# \xff', UnicodeDecodeError),
])
def test_inspected_source_errors_propagate(source_guard, content, error):
    guard = source_guard
    source = guard.ROOT / 'lib/broken.py'
    source.parent.mkdir()
    source.write_bytes(content)
    subprocess.run(['git', 'add', 'lib/broken.py'], cwd=guard.ROOT, check=True)
    with pytest.raises(error):
        guard.check()
