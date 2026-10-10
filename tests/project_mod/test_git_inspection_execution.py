"""Legacy read preparation cannot replay writes or share indexes across cwd."""
from pathlib import Path
import shlex
import subprocess

import pytest

from lib.project_mod.command_analysis import harden_git_inspections
from lib.project_mod import git_inspection_execution as preparation

pytestmark = pytest.mark.unit


@pytest.fixture
def checkouts(tmp_path):
    roots = []
    for name in ('first', 'second'):
        root = tmp_path / name
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        (root / 'file').write_text(name + '\n')
        subprocess.run(['git', '-C', str(root), 'add', 'file'], check=True)
        roots.append(root)
    return roots


def test_legacy_queries_keep_per_checkout_indexes_and_cleanup(checkouts, monkeypatch):
    first, second = checkouts
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: False)
    command = harden_git_inspections('git -C ' + shlex.quote(str(first))
                                    + ' status --short && git -C ' + shlex.quote(str(second))
                                    + ' diff --cached --name-only')
    before = [(root / '.git' / 'index').read_bytes() for root in checkouts]
    with preparation.prepare_git_inspections(command, str(first), environment={}) as execution:
        assert '--no-optional-locks' not in execution
        assert execution.count('GIT_INDEX_FILE=') == 2
        indexes = [Path(word.split('=', 1)[1]) for word in shlex.split(execution)
                   if word.startswith('GIT_INDEX_FILE=')]
        assert indexes[0] != indexes[1]
        assert [index.read_bytes() for index in indexes] == before
        subprocess.run(execution, shell=True, cwd=first, check=True)
    assert all(not index.exists() for index in indexes)
    assert [(root / '.git' / 'index').read_bytes() for root in checkouts] == before


@pytest.mark.parametrize('command', ['python3 -c "print(1)"', 'git add file',
                                     'git diff --stat'])
def test_modern_or_opaque_execution_allocates_no_private_index(checkouts, monkeypatch, command):
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: True)
    monkeypatch.setattr(preparation.tempfile, 'TemporaryDirectory',
                        lambda **kwargs: pytest.fail('unnecessary private-index allocation'))
    command = harden_git_inspections(command)
    with preparation.prepare_git_inspections(command, str(checkouts[0]), environment={}) as execution:
        assert execution == command


def test_private_index_failure_happens_before_any_command_body(checkouts, monkeypatch):
    from lib.project_mod import run_command as commands
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: False)
    def failed_copy(*args):
        raise PermissionError('injected index-copy failure')
    monkeypatch.setattr(preparation.shutil, 'copyfile', failed_copy)
    root = checkouts[0]
    command = 'printf x >> marker && git status --short'
    result = commands.tool_run_command(str(root), command)
    assert '[git-inspection-preparation]' in result
    assert 'Command was not started' in result
    assert not (root / 'marker').exists()


def test_body_error_is_not_reclassified_as_not_started(checkouts, monkeypatch):
    from lib.project_mod import run_command as commands
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: True)
    def body(*args, **kwargs):
        raise preparation.GitInspectionPreparationError('injected body failure')
    monkeypatch.setattr(commands, '_run_command_simple', body)
    with pytest.raises(preparation.GitInspectionPreparationError, match='body failure'):
        commands.tool_run_command(str(checkouts[0]), 'git status --short')


def test_read_preparation_never_replays_neighbouring_write(checkouts, monkeypatch):
    from lib.project_mod import run_command as commands
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: False)
    root = checkouts[0]
    result = commands.tool_run_command(str(root), 'printf x >> marker && git status --short')
    assert '[exit code: 0]' in result
    assert (root / 'marker').read_text() == 'x'


@pytest.mark.parametrize('command', ['git rev-parse --git-path index',
                                     'git config --get core.repositoryformatversion'])
def test_pure_read_preparation_preserves_observable_paths_and_config(checkouts, monkeypatch, command):
    from lib.project_mod import run_command as commands
    root = checkouts[0]
    expected = subprocess.check_output(shlex.split(command), cwd=root).decode().strip()
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: False)
    result = commands.tool_run_command(str(root), command)
    assert '[exit code: 0]' in result
    assert expected in result
    assert 'tofu_git_inspection_' not in result
