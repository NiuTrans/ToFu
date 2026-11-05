"""Internal Git observations preserve writer indexes and report unknown state."""
from importlib import import_module
import json
import os
import shlex
import subprocess
import sys

import pytest

from lib import integration_control
from lib.git_checkpoint_policy import git_policy_environment
from lib.project_mod import git_inspection_execution
from lib.project_mod.command_analysis import harden_git_inspections

pytestmark = pytest.mark.unit


def git(root, *arguments):
    return subprocess.run(['git', '-C', str(root), *arguments],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repository'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Inspection Test')
    git(root, 'config', 'user.email', 'inspection@example.invalid')
    (root / 'tracked.txt').write_text('same content\n')
    git(root, 'add', 'tracked.txt')
    git(root, 'commit', '-qm', 'baseline')
    return root


@pytest.mark.parametrize('owner', ['integration_control', 'linear_git_checkpoint'])
@pytest.mark.parametrize('arguments', [
    ['status', '--porcelain=v1'], ['diff', '--exit-code'], ['describe', '--always', '--dirty'],
])
@pytest.mark.parametrize('legacy', [False, True])
def test_internal_observation_never_refreshes_writer_index(repository, monkeypatch, owner, arguments, legacy):
    if legacy:
        monkeypatch.setattr(git_inspection_execution, '_optional_locks_supported', lambda *args: False)
    tracked = repository / 'tracked.txt'
    before_stat = tracked.stat()
    # Stat-only drift forces Git to recheck unchanged bytes and refresh its index.
    os.utime(tracked, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns - 2_000_000_000))
    index = repository / '.git/index'
    before = (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_ino)
    result = import_module('lib.' + owner)._git(repository, arguments)
    assert result.returncode == 0
    if arguments[0] == 'describe':
        assert result.stdout.strip() == git(repository, 'rev-parse', '--short', 'HEAD').stdout.strip()
    else:
        assert not result.stdout
    assert (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_ino) == before
    assert not (repository / '.git/index.lock').exists()


@pytest.mark.parametrize('arguments', [
    ['diff', '--exit-code'], ['describe', '--always', '--dirty'],
])
def test_hardened_shell_inspection_never_refreshes_writer_index(repository, arguments):
    tracked = repository / 'tracked.txt'
    before_stat = tracked.stat()
    os.utime(tracked, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns - 2_000_000_000))
    index = repository / '.git/index'
    before = (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_ino)
    command = harden_git_inspections(shlex.join(['git', *arguments]))
    with git_inspection_execution.prepare_git_inspections(
            command, str(repository), environment=git_policy_environment()) as execution:
        result = subprocess.run(execution, shell=True, cwd=repository,
                                capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_ino) == before
    assert not (repository / '.git/index.lock').exists()


@pytest.mark.parametrize('legacy', [False, True])
def test_shell_read_observes_preceding_index_write_without_replay(repository, monkeypatch, legacy):
    if legacy:
        monkeypatch.setattr(git_inspection_execution, '_optional_locks_supported', lambda *args: False)
    (repository / 'tracked.txt').write_text('new staged version\n')
    command = harden_git_inspections(
        'printf x >> marker && git add tracked.txt && git diff --cached --name-only')
    with git_inspection_execution.prepare_git_inspections(
            command, str(repository), environment=git_policy_environment()) as execution:
        result = subprocess.run(execution, shell=True, cwd=repository,
                                capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'tracked.txt\n'
    assert (repository / 'marker').read_text() == 'x'
    assert git(repository, 'show', ':tracked.txt').stdout == 'new staged version\n'


@pytest.mark.parametrize('selection', ['export', 'unset'])
def test_shell_query_uses_index_selected_at_execution(repository, selection):
    alternate = repository / 'alternate-index'
    subprocess.run(['git', 'read-tree', 'HEAD'], cwd=repository,
                   env={**os.environ, 'GIT_INDEX_FILE': str(alternate)}, check=True)
    (repository / 'tracked.txt').write_text('staged only in ordinary index\n')
    git(repository, 'add', 'tracked.txt')
    environment = git_policy_environment()
    if selection == 'export':
        command = 'export GIT_INDEX_FILE=alternate-index; git diff --cached --name-only'
        expected = ''
    else:
        environment['GIT_INDEX_FILE'] = str(alternate)
        command = 'unset GIT_INDEX_FILE; git diff --cached --name-only'
        expected = 'tracked.txt\n'
    before = {path: path.read_bytes() for path in [repository / '.git/index', alternate]}
    with git_inspection_execution.prepare_git_inspections(
            harden_git_inspections(command), str(repository), environment=environment) as execution:
        result = subprocess.run(execution, shell=True, cwd=repository, env=environment,
                                capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert {path: path.read_bytes() for path in before} == before


def test_lease_free_runtime_before_query_cannot_pin_an_old_index(repository):
    (repository / 'tracked.txt').write_text('new staged content\n')
    script = 'import subprocess; subprocess.run(["git", "add", "tracked.txt"], check=True)'
    command = harden_git_inspections(
        shlex.join([sys.executable, '-c', script]) + ' && git diff --cached --name-only')
    with git_inspection_execution.prepare_git_inspections(
            command, str(repository), environment=git_policy_environment()) as execution:
        result = subprocess.run(execution, shell=True, cwd=repository,
                                capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'tracked.txt\n'


@pytest.mark.parametrize('owner', ['integration_control', 'shell'])
def test_private_observation_preserves_split_index_evidence(repository, owner):
    git(repository, 'update-index', '--split-index')
    (repository / 'tracked.txt').write_text('staged version\n')
    git(repository, 'add', 'tracked.txt')
    index = repository / '.git/index'
    before = index.read_bytes()
    if owner == 'shell':
        command = harden_git_inspections('git diff --cached --name-only')
        with git_inspection_execution.prepare_git_inspections(
                command, str(repository), environment=git_policy_environment()) as execution:
            result = subprocess.run(execution, shell=True, cwd=repository,
                                    capture_output=True, text=True, check=False)
    else:
        result = integration_control._git(repository, ['diff', '--cached', '--name-only'])
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'tracked.txt\n'
    assert index.read_bytes() == before


def test_query_snapshot_failure_never_executes_with_an_empty_index(repository, tmp_path, monkeypatch, capsys):
    from lib.project_mod import git_index_snapshot_exec
    source = repository / '.git/index'
    before = source.read_bytes()
    private = tmp_path / 'removed-parent/index'
    monkeypatch.setattr(git_index_snapshot_exec.os, 'execvpe',
                        lambda *args: pytest.fail('Failed copy cannot execute Git with an empty index'))
    assert git_index_snapshot_exec.main([json.dumps([str(repository)]), str(private), '--', 'git', 'status']) == 126
    assert 'FileNotFoundError' in capsys.readouterr().err
    assert source.read_bytes() == before


def test_parallel_shell_queries_have_independent_index_snapshots(repository):
    (repository / 'tracked.txt').write_text('staged version\n')
    git(repository, 'add', 'tracked.txt')
    index = repository / '.git/index'
    before = index.read_bytes()
    command = harden_git_inspections(
        'git diff --cached --name-only & git diff --cached --name-only & wait')
    with git_inspection_execution.prepare_git_inspections(
            command, str(repository), environment=git_policy_environment()) as execution:
        result = subprocess.run(execution, shell=True, cwd=repository,
                                capture_output=True, text=True, check=False)
    assert result.returncode == 0 and not result.stderr
    assert result.stdout.splitlines() == ['tracked.txt', 'tracked.txt']
    assert index.read_bytes() == before


@pytest.mark.parametrize('owner', ['integration_control', 'linear_git_checkpoint', 'shell'])
def test_inspection_detects_same_stat_content_change(repository, owner):
    git(repository, 'config', 'core.trustctime', 'false')
    tracked = repository / 'tracked.txt'
    original_stat = tracked.stat()
    index = repository / '.git/index'
    # Recreate Git's racy-index boundary: the entry and index share an mtime.
    os.utime(index, ns=(index.stat().st_atime_ns, original_stat.st_mtime_ns))
    tracked.write_text('diff content\n')
    os.utime(tracked, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert tracked.stat().st_size == original_stat.st_size
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    if owner == 'shell':
        command = harden_git_inspections('git diff --name-only')
        with git_inspection_execution.prepare_git_inspections(
                command, str(repository), environment=git_policy_environment()) as execution:
            result = subprocess.run(execution, shell=True, cwd=repository,
                                    capture_output=True, text=True, check=False)
    else:
        result = import_module('lib.' + owner)._git(repository, ['diff', '--name-only'])
    assert result.returncode == 0
    assert result.stdout == 'tracked.txt\n'
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize('owner', ['integration_control', 'linear_git_checkpoint'])
def test_legacy_inspection_preserves_staged_and_unstaged_evidence(repository, monkeypatch, owner):
    monkeypatch.setattr(git_inspection_execution, '_optional_locks_supported', lambda *args: False)
    (repository / 'tracked.txt').write_text('staged version\n')
    git(repository, 'add', 'tracked.txt')
    (repository / 'tracked.txt').write_text('working version\n')
    index = repository / '.git/index'
    before = index.read_bytes()
    result = import_module('lib.' + owner)._git(repository, ['status', '--porcelain=v1'])
    assert result.returncode == 0 and result.stdout == 'MM tracked.txt\n'
    assert index.read_bytes() == before
    assert git(repository, 'show', ':tracked.txt').stdout == 'staged version\n'
    assert (repository / 'tracked.txt').read_text() == 'working version\n'


@pytest.mark.parametrize('owner', ['integration_control', 'linear_git_checkpoint'])
def test_slow_capability_probe_selects_private_index_instead_of_failing(repository, monkeypatch, owner):
    """The probe only chooses between two correct read strategies. A probe that
    outlives its bound on a loaded host is unknown capability, never a failed read."""
    from types import SimpleNamespace
    probes = []

    def slow_probe(argv, **kwargs):
        probes.append(argv[1:])
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])

    monkeypatch.setattr(git_inspection_execution, 'subprocess', SimpleNamespace(
        run=slow_probe, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE,
        TimeoutExpired=subprocess.TimeoutExpired))
    (repository / 'tracked.txt').write_text('staged version\n')
    git(repository, 'add', 'tracked.txt')
    (repository / 'tracked.txt').write_text('working version\n')
    index = repository / '.git/index'
    before = index.read_bytes()
    result = import_module('lib.' + owner)._git(repository, ['status', '--porcelain=v1'])
    assert probes == [['--no-optional-locks', '--version']]
    assert result.returncode == 0 and result.stdout == 'MM tracked.txt\n'
    assert index.read_bytes() == before


@pytest.mark.parametrize('owner', ['integration_control', 'linear_git_checkpoint'])
def test_real_writes_keep_their_index_and_execute_once(repository, monkeypatch, owner):
    monkeypatch.setattr(git_inspection_execution, '_optional_locks_supported',
                        lambda *args: pytest.fail('Writes must not enter read preparation'))
    (repository / 'tracked.txt').write_text('new staged version\n')
    result = import_module('lib.' + owner)._git(repository, ['add', 'tracked.txt'])
    assert result.returncode == 0
    assert git(repository, 'show', ':tracked.txt').stdout == 'new staged version\n'


@pytest.mark.parametrize('corrupt_index', [False, True])
def test_failed_status_is_unknown_never_clean(repository, tmp_path, corrupt_index):
    target = repository if corrupt_index else tmp_path / 'not-a-repository'
    if corrupt_index:
        (repository / '.git/index').write_bytes(b'corrupt index')
    else:
        target.mkdir()
    result = integration_control._porcelain(target)
    assert result['scanned'] is False
    assert result['timedOut'] is False
    assert result['error']


@pytest.mark.parametrize('owner', ['integration_control', 'linear_git_checkpoint'])
def test_legacy_observation_keeps_unborn_index_absent(tmp_path, monkeypatch, owner):
    root = tmp_path / 'unborn'
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'new.txt').write_text('untracked\n')
    monkeypatch.setattr(git_inspection_execution, '_optional_locks_supported', lambda *args: False)
    result = import_module('lib.' + owner)._git(root, ['status', '--porcelain=v1'])
    assert result.returncode == 0 and result.stdout == '?? new.txt\n'
    assert not (root / '.git/index').exists()
