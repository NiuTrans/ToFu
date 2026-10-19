"""Built-in Python and excluded sources do not initialize optional grammars."""
import builtins

import pytest
from lib.repo_map import outline

pytestmark = pytest.mark.unit


def fixture_files(tmp_path, monkeypatch, sources):
    paths = []
    for name, text in sources.items():
        (tmp_path / name).write_text(text)
        paths.append(name)
    monkeypatch.setattr(outline, 'list_files', lambda _root: list(paths))
    monkeypatch.setattr(outline, '_CODE_EXTENSIONS_CACHE', None)
    return paths


def watch_catalog_imports(monkeypatch, *, unavailable):
    original = builtins.__import__
    attempts = []
    def guarded(name, *args, **kwargs):
        if name == 'grep_ast' or name.startswith('grep_ast.'):
            attempts.append(name)
            if unavailable:
                raise ImportError('optional parser dependency unavailable')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    return attempts


def test_python_with_metadata_keeps_tags_and_references_without_optional_import(tmp_path, monkeypatch):
    files = fixture_files(tmp_path, monkeypatch, {
        'module.py': 'PUBLIC_VALUE = 1\ndef entrypoint():\n    return PUBLIC_VALUE\n',
        'README.md': '# def not_a_definition()\n',
        'metadata.JSON': '{"text":"entrypoint"}',
        '.gitignore': '*.pyc\n', 'LICENSE': 'license text',
    })
    attempts = watch_catalog_imports(monkeypatch, unavailable=True)
    repo = outline.scan(str(tmp_path))
    assert repo.files == files
    assert [(tag.name, tag.kind) for tag in repo.tags['module.py']] == [
        ('PUBLIC_VALUE', 'var'), ('entrypoint', 'def')]
    assert repo.references['PUBLIC_VALUE'] == {'module.py'}
    assert set(repo.tags) == {'module.py'}
    assert attempts == []


def test_excluded_and_extensionless_files_do_not_load_optional_catalog(tmp_path, monkeypatch):
    files = fixture_files(tmp_path, monkeypatch, {
        name: 'def ignored_source(): pass\n' for name in
        ['README.md', 'data.JSON', 'config.toml', 'binary.SO', '.gitignore', 'LICENSE']
    })
    attempts = watch_catalog_imports(monkeypatch, unavailable=True)
    repo = outline.scan(str(tmp_path))
    assert repo.files == files and repo.tags == {} and repo.references == {}
    assert attempts == []


def test_first_non_python_source_still_initializes_its_real_parser(tmp_path, monkeypatch):
    pytest.importorskip('grep_ast')
    files = fixture_files(tmp_path, monkeypatch, {'module.py': 'def python_symbol(): pass\n'})
    attempts = watch_catalog_imports(monkeypatch, unavailable=False)
    assert [tag.name for tag in outline.scan(str(tmp_path)).tags['module.py']] == ['python_symbol']
    assert attempts == []
    (tmp_path / 'module.js').write_text('function javascriptSymbol() { return 1; }\n')
    files.append('module.js')
    repo = outline.scan(str(tmp_path))
    assert [tag.name for tag in repo.tags['module.js']] == ['javascriptSymbol']
    assert [tag.name for tag in repo.tags['module.py']] == ['python_symbol']
    assert attempts
