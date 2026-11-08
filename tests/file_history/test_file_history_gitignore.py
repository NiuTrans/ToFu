"""File-history backups must not be commit-able into the user's repository.

TB2.1 sanitize-git-repo: the agent scrubbed secrets, then ``git add -A``
committed .tofu/file-history/backups/ — which still held the original
secret-bearing files. The store is per-developer undo state, so the store
itself must exclude itself from version control without editing the user's
tracked .gitignore (which would be an unrequested change to a project file).
"""
import subprocess

import pytest

from lib.file_history import store

pytestmark = pytest.mark.unit


# Assembled at runtime: a contiguous key-shaped literal would trip the Open
# export's secret scan; the value is a fake test fixture.
_FAKE_AWS_KEY = 'AKIA' + '1234567890123456'


def test_ensure_store_writes_a_nested_ignore_rule(tmp_path):
    store.ensure_store(str(tmp_path))
    ignore = tmp_path / '.tofu' / 'file-history' / '.gitignore'
    assert ignore.is_file()
    body = ignore.read_text()
    assert '*' in body.splitlines()
    assert '!.gitignore' in body.splitlines()


def test_the_repository_gitignore_is_never_touched(tmp_path):
    (tmp_path / '.gitignore').write_text('*.pyc\n')
    store.ensure_store(str(tmp_path))
    assert (tmp_path / '.gitignore').read_text() == '*.pyc\n'


def test_git_add_all_does_not_stage_backups(tmp_path):
    (tmp_path / '.gitignore').write_text('')
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'src.py').write_text(f'secret = "{_FAKE_AWS_KEY}"\n')
    store.ensure_store(str(tmp_path))
    blob = tmp_path / '.tofu' / 'file-history' / 'backups' / 'ab' / 'hash@v1'
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text(f'secret = "{_FAKE_AWS_KEY}"\n')
    subprocess.run(['git', 'add', '-A'], cwd=tmp_path, check=True)
    staged = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=tmp_path,
                            capture_output=True, text=True, check=True).stdout.splitlines()
    assert 'src.py' in staged
    # Only the store's own .gitignore may be staged; no backup bytes.
    assert not any(path.startswith('.tofu/file-history/backups/') for path in staged), staged


def test_sibling_tofu_directories_are_not_ignored(tmp_path):
    store.ensure_store(str(tmp_path))
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    memory = tmp_path / '.tofu' / 'memories' / 'lesson.md'
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text('# lesson\n')
    (tmp_path / 'src.py').write_text('x = 1\n')
    subprocess.run(['git', 'add', '-A'], cwd=tmp_path, check=True)
    staged = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=tmp_path,
                            capture_output=True, text=True, check=True).stdout.splitlines()
    assert '.tofu/memories/lesson.md' in staged


def test_an_existing_store_gains_the_rule_on_next_open(tmp_path):
    directory = store.store_dir(str(tmp_path))
    import os
    os.makedirs(directory)
    assert not (tmp_path / '.tofu' / 'file-history' / '.gitignore').exists()
    store.ensure_store(str(tmp_path))
    assert (tmp_path / '.tofu' / 'file-history' / '.gitignore').is_file()


def test_ensure_store_is_idempotent_and_preserves_a_custom_rule(tmp_path):
    store.ensure_store(str(tmp_path))
    ignore = tmp_path / '.tofu' / 'file-history' / '.gitignore'
    ignore.write_text('# operator-owned\n')
    store.ensure_store(str(tmp_path))
    assert ignore.read_text() == '# operator-owned\n'
