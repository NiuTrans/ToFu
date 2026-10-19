"""Syntax smoke discovery includes WIP and fails closed without bytecode writes."""
from pathlib import Path
import subprocess

import pytest

from tests.smoke import test_smoke as smoke

pytestmark = pytest.mark.unit


@pytest.fixture
def source_repo(tmp_path, monkeypatch):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    monkeypatch.setattr(smoke, 'PROJECT_ROOT', str(tmp_path))
    return tmp_path


def test_discovery_includes_tracked_and_new_sources_without_ignored_scratch(source_repo):
    (source_repo / '.gitignore').write_text('ignored.py\ntracked.py\n')
    for name in ('tracked.py', 'new source\nname.py', 'removed.py', 'ignored.py'):
        (source_repo / name).write_text('answer = 42\n')
    subprocess.run(['git', 'add', '-f', 'tracked.py', 'removed.py'],
                   cwd=source_repo, check=True)
    (source_repo / 'removed.py').unlink()
    (source_repo / '.tofu_scratch').mkdir()
    (source_repo / '.tofu_scratch' / 'scratch.py').write_text('invalid syntax')
    paths = smoke.TestPythonSyntax()._collect_py_files()
    assert {Path(path).name for path in paths} == {'tracked.py', 'new source\nname.py'}


def test_discovery_refuses_failed_git_instead_of_empty_success(source_repo):
    root = source_repo / 'non-repository'
    root.mkdir()
    (source_repo / '.git').rename(source_repo / 'saved-git')
    with pytest.raises(subprocess.CalledProcessError):
        smoke.TestPythonSyntax()._collect_py_files()


def test_syntax_check_detects_new_errors_without_writing_bytecode(source_repo):
    for index in range(51):
        (source_repo / f'source_{index}.py').write_text('answer = 42\n')
    scanner = smoke.TestPythonSyntax()
    scanner.test_all_py_files_compile()
    assert not (source_repo / '__pycache__').exists()
    (source_repo / 'new.py').write_text('def broken(:\n')
    with pytest.raises(AssertionError, match='Syntax errors in 1 file'):
        scanner.test_all_py_files_compile()
