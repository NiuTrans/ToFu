"""Legacy read preparation cannot replay writes or share indexes across cwd."""
from pathlib import Path
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys

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
    allocated = []
    allocate = preparation._private_index_directory
    def capture_directory(stack):
        directory = allocate(stack)
        allocated.append(Path(directory))
        return directory
    monkeypatch.setattr(preparation, '_private_index_directory', capture_directory)
    with preparation.prepare_git_inspections(command, str(first), environment={}) as execution:
        assert '--no-optional-locks' not in execution
        assert len(allocated) == 1
        indexes = sorted(allocated[0].iterdir())
        assert len(indexes) == 2
        assert indexes[0] != indexes[1]
        assert [index.read_bytes() for index in indexes] == before
        subprocess.run(execution, shell=True, cwd=first, check=True)
    assert all(not index.exists() for index in indexes)
    assert [(root / '.git' / 'index').read_bytes() for root in checkouts] == before


@pytest.mark.parametrize('command', ['python3 -c "print(1)"', 'git add file',
                                     'git log --oneline -1'])
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
    monkeypatch.setattr(preparation, 'copy_index_snapshot', failed_copy)
    root = checkouts[0]
    command = 'printf x >> marker && git status --short'
    with pytest.raises(commands.CommandAdmissionRefusal,
                       match=r'\[git-inspection-preparation\].*Command was not started'):
        commands.tool_run_command(str(root), command)
    assert not (root / 'marker').exists()


@pytest.mark.parametrize('error', [preparation.GitInspectionPreparationError, ValueError])
def test_body_error_is_not_reclassified_as_not_started(checkouts, monkeypatch, error):
    from lib.project_mod import run_command as commands
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: True)
    def body(*args, **kwargs):
        raise error('injected body failure')
    monkeypatch.setattr(commands, '_run_command_simple', body)
    with pytest.raises(error, match='body failure') as raised:
        commands.tool_run_command(str(checkouts[0]), 'git status --short')
    assert not isinstance(raised.value, commands.CommandAdmissionRefusal)


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


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('cd_succeeds', [False, True])
@pytest.mark.parametrize('directory_args', ['', '-C nested -C ..'])
def test_conditional_cwd_read_uses_actual_checkout_and_preserves_indexes(
        checkouts, monkeypatch, legacy, cd_succeeds, directory_args):
    first, second = checkouts
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: not legacy)
    for root in checkouts:
        (root / 'nested').mkdir()
        (root / 'file').write_text(root.name + ' edited\n')
    indexes = [root / '.git/index' for root in checkouts]
    before = [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in indexes]
    destination = second if cd_succeeds else second / 'missing'
    command = harden_git_inspections(
        'cd ' + shlex.quote(str(destination)) + ' && git show :file | cat; '
        + 'git ' + directory_args + ' diff -- file | head -40')
    allocated = []
    allocate = preparation._private_index_directory

    def capture_directory(stack):
        directory = allocate(stack)
        allocated.append(Path(directory))
        return directory

    monkeypatch.setattr(preparation, '_private_index_directory', capture_directory)
    with preparation.prepare_git_inspections(command, str(first), environment=os.environ) as execution:
        result = subprocess.run(execution, shell=True, cwd=first, capture_output=True, text=True)
        expected = second if cd_succeeds else first
        other = first if cd_succeeds else second
        assert result.returncode == 0, result.stderr
        assert '-' + expected.name + '\n' in result.stdout
        assert '+' + expected.name + ' edited\n' in result.stdout
        assert '+' + other.name + ' edited\n' not in result.stdout
    assert allocated and all(not directory.exists() for directory in allocated)
    assert [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in indexes] == before
    assert all(not (root / '.git/index.lock').exists() for root in checkouts)


@pytest.mark.parametrize('cd_succeeds', [False, True])
def test_runtime_index_resolution_preserves_ordered_directory_steps(checkouts, cd_succeeds):
    first, second = checkouts
    indexes = []
    for root in checkouts:
        target = root / 'nested folder' / 'diff'
        subprocess.run(['git', 'init', '-q', str(target)], check=True)
        (target / 'file').write_text(root.name + ' staged\n')
        subprocess.run(['git', '-C', str(target), 'add', 'file'], check=True)
        (target / 'file').write_text(root.name + ' edited\n')
        indexes.extend([root / '.git/index', target / '.git/index'])
    before = [path.read_bytes() for path in indexes]
    destination = second if cd_succeeds else second / 'missing'
    command = harden_git_inspections(
        'cd ' + shlex.quote(str(destination)) + "; git -C 'nested folder' -C diff -C '' diff -- file")
    with preparation.prepare_git_inspections(command, str(first), environment=os.environ) as execution:
        result = subprocess.run(execution, shell=True, cwd=first, capture_output=True, text=True)
    expected = second if cd_succeeds else first
    assert result.returncode == 0, result.stderr
    assert '-' + expected.name + ' staged\n' in result.stdout
    assert '+' + expected.name + ' edited\n' in result.stdout
    assert [path.read_bytes() for path in indexes] == before


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('cd_succeeds', [False, True])
def test_conditional_query_never_refreshes_writer_index(checkouts, monkeypatch, legacy, cd_succeeds):
    first, second = checkouts
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: not legacy)
    for root in checkouts:
        tracked = root / 'file'
        metadata = tracked.stat()
        os.utime(tracked, ns=(metadata.st_atime_ns, metadata.st_mtime_ns - 2_000_000_000))
    indexes = [root / '.git/index' for root in checkouts]
    before = [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in indexes]
    destination = second if cd_succeeds else second / 'missing'
    command = harden_git_inspections('cd ' + shlex.quote(str(destination)) + '; git diff --exit-code')
    with preparation.prepare_git_inspections(command, str(first), environment=os.environ) as execution:
        result = subprocess.run(execution, shell=True, cwd=first, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    assert [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in indexes] == before


@pytest.mark.parametrize('control', [';', '||', '| cat;'])
@pytest.mark.parametrize('cd_succeeds', [False, True])
def test_conditional_read_preserves_shell_control(checkouts, control, cd_succeeds):
    first, second = checkouts
    destination = second if cd_succeeds else second / 'missing'
    command = harden_git_inspections(
        'cd ' + shlex.quote(str(destination)) + ' ' + control + ' git diff --cached -- file')
    with preparation.prepare_git_inspections(command, str(first), environment=os.environ) as execution:
        result = subprocess.run(execution, shell=True, cwd=first, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    if control == '||' and cd_succeeds:
        assert result.stdout == ''
    else:
        expected = second if cd_succeeds and control == ';' else first
        assert '+' + expected.name + '\n' in result.stdout


@pytest.mark.parametrize('cd_succeeds', [False, True])
def test_conditional_cwd_query_sees_prior_write_once(checkouts, cd_succeeds):
    first, second = checkouts
    destination = second if cd_succeeds else second / 'missing'
    command = harden_git_inspections(
        'cd ' + shlex.quote(str(destination)) + ' && git show :file | cat; '
        "printf x >> marker; printf 'new staged content\n' > file; git add file; "
        'git diff --cached -- file')
    with preparation.prepare_git_inspections(command, str(first), environment=os.environ) as execution:
        result = subprocess.run(execution, shell=True, cwd=first, capture_output=True, text=True)
    expected = second if cd_succeeds else first
    other = first if cd_succeeds else second
    assert result.returncode == 0, result.stderr
    assert '+new staged content\n' in result.stdout
    assert (expected / 'marker').read_text() == 'x'
    assert not (other / 'marker').exists()
    assert subprocess.check_output(['git', 'show', ':file'], cwd=expected) == b'new staged content\n'


def _on_disk_snapshot_helper():
    return Path(preparation.__file__).with_name('git_index_snapshot_exec.py')


def test_snapshot_helper_reports_caller_protocol_skew_instead_of_crashing(checkouts, tmp_path):
    root = checkouts[0]
    # A caller loaded before 49053ced2 passed its execution cwd, not JSON -C steps.
    result = subprocess.run(
        [sys.executable, '-I', '-S', str(_on_disk_snapshot_helper()), str(root),
         str(tmp_path / 'index'), '--', 'git', 'diff', '--cached'],
        cwd=root, capture_output=True, text=True)
    assert result.returncode == 126
    assert 'Traceback' not in result.stderr
    assert 'restart the Tofu server' in result.stderr
    assert not (tmp_path / 'index').exists()


def test_on_disk_snapshot_helper_still_serves_callers_that_spawn_it_by_path(checkouts, tmp_path):
    # Servers started before helper pinning execute the checkout file with this argv.
    root = checkouts[0]
    before = (root / '.git/index').read_bytes()
    result = subprocess.run(
        [sys.executable, '-I', '-S', str(_on_disk_snapshot_helper()), '[]',
         str(tmp_path / 'index'), '--', 'git', 'diff', '--cached', '--name-only'],
        cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'file\n'
    assert (root / '.git/index').read_bytes() == before


_CHECKOUT_UPDATED_HELPERS = {
    # Pre-49053ced2 contract: the first argument named the execution cwd.
    'older': 'execution_cwd, private_index = sys.argv[1:3]\n',
    # A later contract that no longer understands today's argument list.
    'newer': 'raise SystemExit("unsupported Git snapshot helper arguments")\n',
}


@pytest.mark.parametrize('update', sorted(_CHECKOUT_UPDATED_HELPERS))
def test_loaded_caller_never_executes_helper_changed_on_disk(checkouts, tmp_path, monkeypatch, update):
    first, second = checkouts
    (second / 'file').write_text('second edited\n')
    loaded = tmp_path / 'loaded'
    loaded.mkdir()
    for name in ('git_inspection_execution.py', 'git_index_snapshot_exec.py'):
        shutil.copyfile(Path(preparation.__file__).with_name(name), loaded / name)
    # A server imports the caller once, then the checkout changes beneath it.
    module_name = 'lib.project_mod._caller_loaded_before_checkout_update'
    spec = importlib.util.spec_from_file_location(module_name, loaded / 'git_inspection_execution.py')
    caller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, caller)
    spec.loader.exec_module(caller)
    executed = tmp_path / 'updated-helper-executed'
    (loaded / 'git_index_snapshot_exec.py').write_text(
        f'import pathlib, sys\npathlib.Path({str(executed)!r}).write_text("x")\n'
        + _CHECKOUT_UPDATED_HELPERS[update])
    command = harden_git_inspections(
        'cd ' + shlex.quote(str(second)) + ' && git show :file | cat; git diff -- file')
    with caller.prepare_git_inspections(command, str(first), environment=os.environ) as execution:
        result = subprocess.run(execution, shell=True, cwd=first, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '+second edited\n' in result.stdout
    assert not executed.exists()


def test_command_runner_executes_compound_git_read_after_cd(checkouts):
    from lib.project_mod import run_command as commands
    first, second = checkouts
    command = ('cd ' + shlex.quote(str(second)) + ' && git show :file | cat; '
               'git diff --cached -- file | head -40')
    result = commands.tool_run_command(str(first), command)
    assert '[exit code: 0]' in result
    assert '+second' in result
    assert '[git-inspection-preparation]' not in result


@pytest.mark.parametrize('legacy', [False, True])
def test_read_after_a_run_time_cd_uses_the_index_of_the_checkout_entered(
        checkouts, monkeypatch, legacy):
    """`cd "$D" && git diff` reads D's index, not a copy of the starting checkout's.

    The cwd model ignored `cd "$D"`, so the read was prepared with a private
    copy of the first checkout's index and `git show :file` printed `first`.
    """
    first, second = checkouts
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: not legacy)
    for root in checkouts:
        (root / 'file').write_text(root.name + ' edited\n')
    command = harden_git_inspections('cd "$D" && git show :file | cat; git diff -- file | head -40')
    with preparation.prepare_git_inspections(command, str(first), environment=os.environ) as execution:
        result = subprocess.run(execution, shell=True, cwd=first, capture_output=True, text=True,
                                env={**os.environ, 'D': str(second)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('second\n')
    assert '-second\n' in result.stdout and '+second edited\n' in result.stdout
    assert 'first' not in result.stdout
