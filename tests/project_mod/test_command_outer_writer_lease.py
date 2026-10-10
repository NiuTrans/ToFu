"""Project command exclusion spans preimage, fast-copy/subprocess, and journal.

Only private temporary Git checkouts are used; contenders synchronize with
bounded joins/events rather than sleeps or the live workspace's lock.
"""
from contextlib import contextmanager
import os
import shlex
import subprocess
import threading
from types import SimpleNamespace

import pytest

from lib.project_mod import tools
from lib.project_mod.workspace_lease import (
    WorkspaceBusyError, workspace_lease_fds, workspace_write_lease,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path, monkeypatch):

    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    root = tmp_path / 'checkout'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'source').write_text('new contents\n')
    (root / 'target').write_text('old\n')
    monkeypatch.setenv('TOFU_RUN_CP_FASTPATH', '1')
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    return root


@contextmanager
def _other_writer(root):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with workspace_write_lease(root):
                ready.set()
                if not release.wait(10):
                    raise AssertionError('writer release was not signalled')
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(5)
        assert not errors
        yield
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors


def _contender_result(root):
    outcomes = []

    def contend():
        try:
            with workspace_write_lease(root):
                outcomes.append('acquired')
        except WorkspaceBusyError:
            outcomes.append('busy')

    worker = threading.Thread(target=contend)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert len(outcomes) == 1
    return outcomes[0]


@pytest.mark.parametrize('route', ['cwd', 'working_dir', 'destination', 'alias'])
def test_fast_copy_busy_before_snapshot_or_mutation(repo, tmp_path, monkeypatch, route):
    base = repo
    args = {'command': 'cp source target'}
    if route == 'working_dir':
        base = tmp_path
        args['working_dir'] = str(repo)
    elif route == 'destination':
        base = tmp_path
        (base / 'source').write_text('outside source\n')
        args['command'] = f'cp source {shlex.quote(str(repo / "target"))}'
    elif route == 'alias':
        base = tmp_path / 'alias'
        base.symlink_to(repo, target_is_directory=True)

    def forbidden(*_args, **_kwargs):
        pytest.fail('busy command reached snapshot, execution, or history')

    for name in ('_snapshot_targets', '_snapshot_project_files',
                 '_run_cp_in_process', 'tool_run_command',
                 '_record_run_command_changes'):
        monkeypatch.setattr(tools, name, forbidden)
    with _other_writer(repo):
        result = tools.execute_tool('run_command', args, str(base))
    assert '[workspace-busy]' in result
    assert 'Command was not started' in result
    assert (repo / 'target').read_text() == 'old\n'


@pytest.mark.parametrize('fast_copy', [True, False])
def test_same_lease_covers_preimage_execution_postimage_and_journal(
        repo, monkeypatch, fast_copy):
    monkeypatch.setenv('TOFU_RUN_CP_FASTPATH', '1' if fast_copy else '0')
    observed = []
    snapshots = tools._snapshot_targets
    record_changes = tools._record_run_command_changes
    execute = tools._run_cp_in_process if fast_copy else tools.tool_run_command
    initial_fds = []

    def assert_held(phase):
        fds = workspace_lease_fds()
        assert len(fds) == 2  # shared checkout barrier + exclusive target
        if not initial_fds:
            initial_fds.extend(fds)
        assert fds == tuple(initial_fds)
        assert _contender_result(repo) == 'busy'
        observed.append(phase)

    def snapshot(*args, **kwargs):
        assert_held('snapshot')
        return snapshots(*args, **kwargs)

    def execute_with_probe(*args, **kwargs):
        assert_held('execute')
        if not fast_copy:
            kwargs['on_spawn'] = lambda *_: assert_held('spawn')
        result = execute(*args, **kwargs)
        assert_held('execution-returned')
        return result

    def journal(cwd, changes, **kwargs):
        assert_held('journal')
        assert cwd == str(repo)
        change = next(ch for ch in changes if ch['rel_path'] == 'target')
        assert change['original_content'] == 'old\n'
        assert (repo / 'target').read_text() == 'new contents\n'
        recorded = record_changes(cwd, changes, **kwargs)
        assert len(recorded) == 1
        assert _contender_result(repo) == 'busy'
        return recorded

    monkeypatch.setattr(tools, '_snapshot_targets', snapshot)
    monkeypatch.setattr(tools, '_run_cp_in_process' if fast_copy else 'tool_run_command',
                        execute_with_probe)
    monkeypatch.setattr(tools, '_record_run_command_changes', journal)
    result = tools.execute_tool('run_command', {'command': 'cp source target'}, str(repo))
    assert '[exit code: 0]' in result
    assert observed == (['snapshot', 'execute'] + ([] if fast_copy else ['spawn'])
                        + ['execution-returned', 'snapshot', 'journal'])
    assert not workspace_lease_fds()
    assert _contender_result(repo) == 'acquired'


@pytest.mark.parametrize('phase', ['snapshot', 'copy', 'journal'])
def test_outer_lease_released_on_exception(repo, monkeypatch, phase):
    def fail(*_args, **_kwargs):
        assert _contender_result(repo) == 'busy'
        raise RuntimeError(f'{phase} fault')

    seam = {'snapshot': '_snapshot_targets', 'copy': '_run_cp_in_process',
            'journal': '_record_run_command_changes'}[phase]
    monkeypatch.setattr(tools, seam, fail)
    with pytest.raises(RuntimeError, match=f'{phase} fault'):
        tools.execute_tool('run_command', {'command': 'cp source target'}, str(repo))
    assert not workspace_lease_fds()
    assert _contender_result(repo) == 'acquired'


def test_readonly_command_remains_available_during_other_writer(repo):
    with _other_writer(repo):
        result = tools.execute_tool('run_command', {'command': 'git status --short'}, str(repo))
    assert '[exit code: 0]' in result
    assert '[workspace-busy]' not in result


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_shell_prefix_cannot_borrow_git_readonly_exemption(repo, monkeypatch, route):
    import lib.project_mod.run_command as commands

    monkeypatch.setattr(commands, 'SHELL_PREFIX', 'touch copied.txt;')
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), 'git status --short')
        else:
            result = tools.execute_tool('run_command', {'command': 'git status --short'}, str(repo))
    assert '[workspace-busy]' in result
    assert not (repo / 'copied.txt').exists()


@pytest.mark.parametrize('command', [None, '', ' \t '])
def test_empty_command_validation_does_not_require_a_lease(repo, command):
    import lib.project_mod.run_command as commands

    with _other_writer(repo):
        assert commands.tool_run_command(str(repo), command) == 'Error: Empty command.'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    'sort -o tracked.txt tracked.txt',
    'npm run build > copied.txt',
    'python3 -c "$(touch copied.txt)"',
    "python3 -c 'print(1)' > copied.txt",
    'sort --output=tracked.txt tracked.txt',
    'find . -name tracked.txt -delete',
    "sed '1w copied.txt' tracked.txt",
    "awk 'BEGIN {system(\"touch copied.txt\")}'",
    'uniq tracked.txt copied.txt',
    'git log --output=copied.txt',
    'git -c core.fsmonitor=./hook status --short',
    'git --paginate log -1',
    'git log --ext-diff -p',
    'git log --textconv -p',
    'git log --show-signature',
    'git log --format=%G?',
    'git log --unknown-option',
    'git status --short > copied.txt',
    'git status --short | sort -o copied.txt',
    'GIT_PAGER=./hook git log -1',
    'env git status --short',
    "sh -c 'git status --short'",
    'git status --short\ntouch copied.txt',
    'git status --short # hidden shell tail',
    'git status "unterminated',
    'git status --short; sort -o tracked.txt tracked.txt',
    'git status "$(touch copied.txt)"',
    'git status `touch copied.txt`',
])
def test_shell_write_forms_refuse_competing_writer_before_spawn(
        repo, monkeypatch, route, command):
    import lib.project_mod.run_command as commands

    (repo / 'tracked.txt').write_text('z\na\n')

    def forbidden(*_args, **_kwargs):
        pytest.fail('busy shell command reached snapshot, spawn, or history')

    monkeypatch.setattr(commands.subprocess, 'Popen', forbidden)
    for name in ('_snapshot_targets', '_snapshot_project_files',
                 '_record_run_command_changes'):
        monkeypatch.setattr(tools, name, forbidden)
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[workspace-busy]' in result
    assert 'Command was not started' in result
    assert (repo / 'tracked.txt').read_text() == 'z\na\n'
    assert not (repo / 'copied.txt').exists()
    assert _contender_result(repo) == 'acquired'


@pytest.mark.parametrize('command', [
    'git status --short &',
    'sort tracked.txt &',
    'git status --short; disown',
])
def test_readonly_looking_unmanaged_background_is_also_refused(repo, command):
    import lib.project_mod.run_command as commands

    result = commands.tool_run_command(str(repo), command)
    assert '[workspace-lease-background-unsupported]' in result
    assert _contender_result(repo) == 'acquired'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    'git status --short',
    'git status --porcelain=v2 -uno',
    "git -C '.' --no-pager status -b --short",
    'git log --oneline -1',
    'git log -1',
    'git --no-pager log --graph --max-count 2 --all',
])
def test_proven_git_inspection_stays_parallel_without_index_or_helper_writes(
        repo, route, command):
    import os
    import lib.project_mod.run_command as commands

    subprocess.run(['git', '-C', str(repo), 'add', '--', 'source'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Lease Test',
                    '-c', 'user.email=lease@example.invalid', 'commit',
                    '-qm', 'private fixture'], check=True)
    hook = repo / 'fsmonitor'
    marker = repo / 'helper-ran'
    hook.write_text(f'#!/bin/sh\nprintf called > {shlex.quote(str(marker))}\n')
    hook.chmod(0o700)
    subprocess.run(['git', '-C', str(repo), 'config', 'core.fsmonitor', str(hook)], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'format.pretty', 'unsafe-default'], check=True)
    # Make an optional index refresh meaningful without changing file bytes.
    stamp = (repo / 'source').stat().st_mtime + 10
    os.utime(repo / 'source', (stamp, stamp))
    index = repo / '.git' / 'index'
    before = index.read_bytes()
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
        assert '[exit code: 0]' in result
        assert '[workspace-busy]' not in result
        assert index.read_bytes() == before
        assert not marker.exists()
    assert _contender_result(repo) == 'acquired'


@pytest.mark.parametrize('command', [
    'sort -o tracked.txt tracked.txt',
    'find . -name tracked.txt -delete',
    "sed '1w copied.txt' tracked.txt",
])
def test_snapshot_readonly_shell_still_holds_lease_through_execution(
        repo, monkeypatch, command):
    (repo / 'tracked.txt').write_text('z\na\n')
    execute = tools.tool_run_command
    observations = []

    def checked_execute(*args, **kwargs):
        assert workspace_lease_fds()
        observations.append(_contender_result(repo))
        kwargs['on_spawn'] = lambda *_: observations.append(_contender_result(repo))
        result = execute(*args, **kwargs)
        observations.append(_contender_result(repo))
        return result

    monkeypatch.setattr(tools, 'tool_run_command', checked_execute)
    result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[exit code: 0]' in result
    assert observations == ['busy', 'busy', 'busy']
    if command.startswith('sort'):
        assert (repo / 'tracked.txt').read_text() == 'a\nz\n'
    elif command.startswith('find'):
        assert not (repo / 'tracked.txt').exists()
    else:
        assert (repo / 'copied.txt').read_text() == 'z\n'
    assert not workspace_lease_fds()
    assert _contender_result(repo) == 'acquired'


def test_readonly_root_refusal_precedes_lease(repo, monkeypatch):
    monkeypatch.setattr('lib.project_mod.config.is_readonly_path', lambda *a, **kw: True)
    result = tools.execute_tool('run_command', {'command': 'cp source target'}, str(repo))
    assert 'READ-ONLY' in result
    assert (repo / 'target').read_text() == 'old\n'
    assert not (repo / '.git' / 'tofu-workspace-leases').exists()


def test_detached_write_refused_before_snapshot(repo, monkeypatch):
    monkeypatch.setattr(tools, '_snapshot_targets',
                        lambda *a: pytest.fail('detached write reached preimage'))
    result = tools.execute_tool('run_command', {'command': 'cp source target &'}, str(repo))
    assert '[workspace-lease-background-unsupported]' in result
    assert (repo / 'target').read_text() == 'old\n'
    assert _contender_result(repo) == 'acquired'


# Opaque commands deliberately have no lifetime read claim. Explicit readers
# still use workspace_read_lease when their caller requires a stable snapshot.

@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    'pytest -q tests/', 'npm run build', 'python3 -m pytest tests/',
    'make -j4', "python3 -c 'print(1)'", 'sleep 30',
    'python3 -c "\nprint(2 > 1)\n" 2>&1 | head -60',
    "python3 -c 'print(2 > 1)'",
    'python3 -c "print(2 > 1)"',
    "python3 -c 'print(1)' 1>&2",
    "python3 -c 'print(1)' 3<&0",
    "python3 -c 'print(1)' 2>&1",
])
def test_opaque_commands_overlap_writers_without_inherited_lease(
        repo, monkeypatch, route, command):
    import lib.project_mod.run_command as commands
    from lib.project_mod.write_tools import _ops

    entered, release = threading.Event(), threading.Event()
    errors, outcomes = [], []
    (repo / 'file.txt').write_text('before\n')

    def supervised_process(*_args, **_kwargs):
        # This is the process boundary, not a replacement admission wrapper.
        assert workspace_lease_fds() == ()
        entered.set()
        assert release.wait(10)
        return '$ opaque\n[exit code: 0]'

    def run_opaque():
        try:
            if route == 'runner':
                result = commands.tool_run_command(str(repo), command)
            else:
                result = tools.execute_tool('run_command', {'command': command}, str(repo))
            outcomes.append(result)
        except BaseException as error:
            errors.append(error)
            entered.set()

    monkeypatch.setattr(commands, '_run_command_simple', supervised_process)
    with _other_writer(repo):
        worker = threading.Thread(target=run_opaque)
        worker.start()
        try:
            assert entered.wait(5)
            assert not errors
        except BaseException:
            release.set()
            worker.join(5)
            raise
    try:
        assert worker.is_alive()  # Still waiting for the explicit release event.
        result = _ops.tool_write_file(str(repo), 'file.txt', 'after\n')
        assert result['ok'] is True
        assert (repo / 'file.txt').read_text() == 'after\n'
        assert _contender_result(repo) == 'acquired'
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert len(outcomes) == 1 and '[exit code: 0]' in outcomes[0]
    assert workspace_lease_fds() == ()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    'cp source target', 'sort -o target target', 'sed -i s/old/new/ target',
    'git add -A', 'git commit -m wip', 'git checkout -B feature',
    'git stash pop', 'echo hi > target',
])
def test_identified_mutation_excludes_other_writers_at_process_boundary(
        repo, monkeypatch, route, command):
    import lib.project_mod.run_command as commands
    observed = []

    def process_boundary(*_args, **_kwargs):
        assert workspace_lease_fds()
        observed.append(_contender_result(repo))
        return '$ mutation\n[exit code: 0]'

    monkeypatch.setenv('TOFU_RUN_CP_FASTPATH', '0')
    monkeypatch.setattr(commands, '_run_command_simple', process_boundary)
    if route == 'runner':
        result = commands.tool_run_command(str(repo), command)
    else:
        result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[exit code: 0]' in result
    assert observed == ['busy']
    assert workspace_lease_fds() == ()
    assert _contender_result(repo) == 'acquired'


# Plain runtime commands do not participate in checkout coordination.

def test_opaque_test_commands_run_concurrently(repo):
    """Two opaque test/build commands overlap instead of serializing."""
    import time
    import lib.project_mod.run_command as commands
    started, outcomes = [], []

    def worker():
        started.append(time.monotonic())
        outcomes.append(commands.tool_run_command(str(repo), 'sleep 0.3'))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
        assert not t.is_alive()
    assert all('[exit code: 0]' in r for r in outcomes)
    # Both sleeps overlapped in wall-clock → they were not serialized.
    assert len(started) == 2
    assert abs(started[1] - started[0]) < 0.3


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', ['sleep 0', 'sleep 0 && echo ready',
                                     "python3 -c 'print(1)'"])
def test_writer_does_not_block_plain_runtime_command(repo, route, command):
    import lib.project_mod.run_command as commands

    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[exit code: 0]' in result
    assert '[workspace-busy]' not in result


@pytest.mark.parametrize('command', [
    'pytest -q tests/', 'npm run build', 'python3 -m pytest tests/',
    'make -j4', 'cargo test', 'CI=1 npm run build',
])
def test_build_and_test_admission_does_not_hold_checkout_lease(repo, command):
    import lib.project_mod.run_command as commands

    @commands._with_command_writer
    def admitted(base, command):
        assert not workspace_lease_fds()
        return 'started'

    with _other_writer(repo):
        assert admitted(str(repo), command) == 'started'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_runtime_command_does_not_block_edit_or_git_write(repo, route):
    import sys
    import lib.project_mod.run_command as commands
    from lib.project_mod.write_tools import _ops

    (repo / 'file.txt').write_text('before\n')
    ready = threading.Event()
    release = repo / 'release-runtime'
    program = ("import pathlib,time; marker=pathlib.Path('release-runtime'); "
               "deadline=time.monotonic()+10; "
               "exec('while not marker.exists() and time.monotonic()<deadline: time.sleep(.01)')")
    command = shlex.join([sys.executable, '-c', program])
    outcomes = []

    def execute():
        if route == 'runner':
            outcomes.append(commands.tool_run_command(
                str(repo), command, on_spawn=lambda *_: ready.set()))
        else:
            outcomes.append(tools.execute_tool(
                'run_command', {'command': command}, str(repo),
                on_spawn=lambda *_: ready.set()))

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert ready.wait(5)
        assert _contender_result(repo) == 'acquired'
        result = _ops.tool_write_file(str(repo), 'file.txt', 'after\n')
        assert result['ok'] is True
        assert (repo / 'file.txt').read_text() == 'after\n'
        result = commands.tool_run_command(str(repo), 'git add -- file.txt')
        assert '[exit code: 0]' in result
    finally:
        release.touch()
        worker.join(5)
    assert not worker.is_alive()
    assert len(outcomes) == 1 and '[exit code: 0]' in outcomes[0]
    assert not workspace_lease_fds()


@pytest.fixture
def sibling(repo, tmp_path):
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Lease Test',
                    '-c', 'user.email=lease@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    worktree = tmp_path / 'isolated worktree'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', 'peer', str(worktree)], check=True)
    return worktree


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_literal_leading_cd_writer_leases_only_actual_worktree(
        repo, sibling, monkeypatch, route):
    import lib.project_mod.run_command as commands
    observed = []

    def process_boundary(*_args, **_kwargs):
        assert len(workspace_lease_fds()) == 2  # FILE in the actual worktree
        observed.append((_contender_result(repo), _contender_result(sibling)))
        return '$ worktree\n[exit code: 0]'

    monkeypatch.setattr(commands, '_run_command_simple', process_boundary)
    command = f'cd {shlex.quote(str(sibling))} && cp source target'
    if route == 'runner':
        result = commands.tool_run_command(str(repo), command)
    else:
        result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[exit code: 0]' in result
    assert observed == [('acquired', 'busy')]
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[exit code: 0]' in result
    assert workspace_lease_fds() == ()
    assert _contender_result(sibling) == 'acquired'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('suffix', ['', ' && cp source target'])
def test_mutation_before_cd_never_drops_canonical_admission(
        repo, sibling, monkeypatch, route, suffix):
    import lib.project_mod.run_command as commands

    def forbidden(*_args, **_kwargs):
        pytest.fail('canonical mutation before cd passed the admission boundary')

    monkeypatch.setattr(commands, '_run_command_simple', forbidden)
    monkeypatch.setattr(tools, '_snapshot_targets', forbidden)
    monkeypatch.setattr(tools, '_snapshot_project_files', forbidden)
    command = f'cp source target && cd {shlex.quote(str(sibling))}{suffix}'
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[workspace-busy]' in result
    assert (repo / 'target').read_text() == 'old\n'
    assert workspace_lease_fds() == ()
    assert _contender_result(sibling) == 'acquired'


@pytest.mark.parametrize('shape', [
    'cd {target}; cp source target',
    'cd {target} || cp source target',
    'cd {target} | cp source target',
    'cd {target} && echo done; cp source target',
    'cd {target} && echo done || cp source target',
])
def test_cd_failure_and_pipeline_shapes_retain_canonical_fallback(repo, sibling, shape):
    import lib.project_mod.run_command as commands

    command = shape.format(target=shlex.quote(str(sibling)))
    with _other_writer(repo):
        result = commands.tool_run_command(str(repo), command)
    assert '[workspace-busy]' in result
    assert (repo / 'target').read_text() == 'old\n'
    assert _contender_result(sibling) == 'acquired'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    'cp source target &', 'nohup cp source target', '/usr/bin/nohup cp source target',
    "'setsid' cp source target", 'git status --short; disown',
])
def test_unmanaged_detach_refused_before_outer_preimage(repo, monkeypatch, route, command):
    import lib.project_mod.run_command as commands

    def forbidden(*_args, **_kwargs):
        pytest.fail('unmanaged detach reached preimage or process')

    monkeypatch.setattr(commands, '_run_command_simple', forbidden)
    monkeypatch.setattr(tools, '_snapshot_targets', forbidden)
    monkeypatch.setattr(tools, '_snapshot_project_files', forbidden)
    if route == 'runner':
        result = commands.tool_run_command(str(repo), command)
    else:
        result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[workspace-lease-background-unsupported]' in result
    assert workspace_lease_fds() == ()
    assert _contender_result(repo) == 'acquired'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('cancel_source', [None, 'task', 'runtime'])
def test_writer_wait_acquires_or_cancels_before_snapshot_and_spawn(
        repo, monkeypatch, route, cancel_source):
    import lib.project_mod.run_command as commands
    from lib.project_mod import tool_admission
    task = {'aborted': False}
    runtime = SimpleNamespace(cancellation_requested=False)
    waiting = threading.Event()
    outcomes, errors, phases = [], [], []
    snapshots = tools._snapshot_targets
    original_is_set = tool_admission.Cancellation.is_set
    probes = {}

    def observed_cancel(cancel):
        thread_id = threading.get_ident()
        probes[thread_id] = probes.get(thread_id, 0) + 1
        if probes[thread_id] >= 3:
            waiting.set()
        return original_is_set(cancel)

    def snapshot(*args, **kwargs):
        phases.append('snapshot')
        assert workspace_lease_fds()
        return snapshots(*args, **kwargs)

    def process_boundary(*_args, **_kwargs):
        phases.append('process')
        assert workspace_lease_fds()
        assert _contender_result(repo) == 'busy'
        return '$ writer\n[exit code: 0]'

    def writer():
        try:
            if route == 'runner':
                # The task's positional slot must work too, not kwargs alone.
                result = commands.tool_run_command(
                    str(repo), 'cp source target', None, None, task, runtime_context=runtime)
            else:
                result = tools.execute_tool(
                    'run_command', {'command': 'cp source target'}, str(repo),
                    task=task, runtime_context=runtime)
            outcomes.append(result)
        except BaseException as error:
            errors.append(error)
            waiting.set()

    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '5')
    monkeypatch.setenv('TOFU_RUN_CP_FASTPATH', '0')
    monkeypatch.setattr(tool_admission.Cancellation, 'is_set', observed_cancel)
    monkeypatch.setattr(commands, '_run_command_simple', process_boundary)
    monkeypatch.setattr(tools, '_snapshot_targets', snapshot)
    with _other_writer(repo):
        worker = threading.Thread(target=writer)
        worker.start()
        try:
            assert waiting.wait(5)
            assert not errors
            assert phases == []
        finally:
            if cancel_source == 'task':
                task['aborted'] = True
            elif cancel_source == 'runtime':
                runtime.cancellation_requested = True
            if cancel_source:
                worker.join(5)
                assert not worker.is_alive()
    worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert len(outcomes) == 1
    if cancel_source:
        assert '[workspace-lease-cancelled]' in outcomes[0]
        assert 'Command was not started' in outcomes[0]
        assert phases == []
    else:
        assert '[exit code: 0]' in outcomes[0]
        assert 'process' in phases
        assert phases == (['process'] if route == 'runner' else ['snapshot', 'process', 'snapshot'])
    assert workspace_lease_fds() == ()
    assert _contender_result(repo) == 'acquired'


def test_tool_admission_timeout_default_and_bounds(monkeypatch):
    from lib.project_mod.tool_admission import admission_timeout

    monkeypatch.delenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', raising=False)
    assert admission_timeout() == 5
    for value in ['0', '600']:
        monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', value)
        assert admission_timeout() == float(value)
    for value in ['-1', '601', 'inf', 'nan', 'invalid']:
        monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', value)
        with pytest.raises(ValueError, match=r'\[0, 600\]'):
            admission_timeout()


@pytest.mark.parametrize('interactive', [False, True])
def test_popen_inherits_writer_descriptor_until_child_exit(repo, monkeypatch, interactive):
    import lib.project_mod.run_command as commands

    popen = subprocess.Popen
    inherited = []

    def checked_popen(*args, **kwargs):
        fds = workspace_lease_fds()
        assert fds and kwargs['pass_fds'] == fds
        for fd in fds:
            os.fstat(fd)
        inherited.extend(fds)
        return popen(*args, **kwargs)

    monkeypatch.setattr(commands.subprocess, 'Popen', checked_popen)
    result = commands.tool_run_command(
        str(repo), 'cp source target',
        stdin_callback=(lambda _prompt: '') if interactive else None)
    assert '[exit code: 0]' in result
    assert (repo / 'target').read_text() == 'new contents\n'
    assert inherited
    assert workspace_lease_fds() == ()
    for fd in inherited:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert _contender_result(repo) == 'acquired'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    'python3 -c "\nprint(2 > 1)\n" > target 2>&1',
    "python3 -c 'print(1)' 2> 'target'",
    "python3 -c 'print(1)' >target 2>&1 | head -60",
    'python3 -c "print(\\"$(touch target)\\")"',
    'python3 -c "print(\\"`touch target`\\")"',
    'sort "-o" "target" source',
    "'unlink' 'target'",
])
def test_shell_writes_remain_blocked_with_quoted_arguments(repo, monkeypatch, route, command):
    import lib.project_mod.run_command as commands
    monkeypatch.setattr(commands, '_run_command_simple',
                        lambda *a, **kw: pytest.fail('write reached process boundary'))
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[workspace-busy]' in result
    assert (repo / 'target').read_text() == 'old\n'


def test_multiline_python_query_pipeline_executes_during_other_writer(repo):
    import sys
    from lib.project_mod.run_command import tool_run_command
    source = "\nimport sqlite3\ncon = sqlite3.connect(':memory:')\nprint(con.execute('SELECT 26 WHERE 26 > 1').fetchone()[0])\n"
    command = f'{shlex.quote(sys.executable)} -c {shlex.quote(source)} 2>&1 | head -60'
    with _other_writer(repo):
        result = tool_run_command(str(repo), command)
    assert '[exit code: 0]' in result
    assert '26' in result
    assert '[workspace-busy]' not in result


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    "python3 - <<'PY'\nprint(3 & 1)\n# $() > target; rm target\nPY\n",
    'npm run test:unit -- --run example.test.ts && python3 - <<"PY"\n'
    'print(2 > 1)\nPY\n',
    "node - <<'JS'\nconsole.log(`literal ${1}`);\nJS\n",
    "python3 - <<'PY-stdin'\n# unmatched quote: '\nprint(1)\nPY-stdin",
    "python3 - <<'PY literal'\n# PY literal suffix\nprint(1)\nPY literal\n",
    "python3 - 0<<'PY'\nprint(1)\nPY\necho done",
    "python3 - <<'PY' 2>&1 | head -n 1\nprint(3 & 1)\nPY\n",
    "python3 - <<'PY' 3<&0\nprint(1)\nPY\n",
    "python3 - <<'PY'\nprint(1)\nPY\n"
    "node - <<'JS'\nconsole.log(1);\nJS\n",
    # Runtime is an opaque scheduling exemption, NOT a readonly proof.
    "python3 - <<'PY'\nfrom pathlib import Path\n"
    "Path('target').write_text('opaque runtime can write')\nPY\n",
])
def test_literal_runtime_heredoc_keeps_original_at_process_boundary(
        repo, monkeypatch, route, command):
    import lib.project_mod.run_command as commands
    observed = []

    def process_boundary(execution_source, *_args, **_kwargs):
        assert workspace_lease_fds() == ()
        assert command.strip() in execution_source
        observed.append(execution_source)
        return '$ runtime heredoc\n[exit code: 0]'

    monkeypatch.setattr(commands, '_run_command_simple', process_boundary)
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[exit code: 0]' in result
    assert len(observed) == 1
    assert not workspace_lease_fds()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_r54_unit_test_chain_and_python_stdin_read_runs_under_root_writer(
        repo, tmp_path, monkeypatch, route):
    import lib.project_mod.run_command as commands

    # Exercise the real shell/Python path without depending on npm packages.
    binary_dir = tmp_path / 'bin'
    binary_dir.mkdir()
    npm = binary_dir / 'npm'
    npm.write_text('#!/bin/sh\nprintf "unit runner: %s\\n" "$*"\n')
    npm.chmod(0o700)
    monkeypatch.setenv('PATH', f'{binary_dir}:{os.environ["PATH"]}')
    command = ("npm run test:unit -- --run src/example.test.ts && python3 - <<'PY'\n"
               "from pathlib import Path\n"
               "print('stdin read:', Path('target').read_text().strip(), 3 & 1)\n"
               "PY\n")
    spawned = []
    popen = commands.subprocess.Popen

    def process_boundary(*args, **kwargs):
        assert workspace_lease_fds() == ()
        assert not kwargs.get('pass_fds')
        argv = args[0]
        source = argv if isinstance(argv, str) else (
            argv[2] if len(argv) >= 3 and argv[1] == '-c' else None)
        if source is not None:
            assert command.strip() in source
            spawned.append(source)
        return popen(*args, **kwargs)

    monkeypatch.setattr(commands.subprocess, 'Popen', process_boundary)
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[exit code: 0]' in result
    assert 'unit runner: run test:unit -- --run src/example.test.ts' in result
    assert 'stdin read: old 1' in result
    assert len(spawned) == 1
    assert (repo / 'target').read_text() == 'old\n'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    "python3 - <<'PY'\nprint(1)\n",  # incomplete
    'python3 - <<PY\nprint("$HOME")\nPY\n',  # expansion-enabled
    "python3 - <<'PY' > target\nprint(1)\nPY\n",
    "python3 - <<'PY' 3> target\nprint(1)\nPY\n",
    "python3 - <<-'PY'\n\tprint(1)\n\tPY\n",
    "python3 - 3<<'PY'\nprint(1)\nPY\n",
    "python3 - <<'PY' <<'SECOND'\nprint(1)\nPY\nprint(2)\nSECOND\n",
    "sh <<'SH'\ntouch target\nSH\n",
    "echo $(python3 - <<'PY'\nprint(1)\nPY\n)",
    "python3 - <<'PY'\nprint(1)\nPY\ncp source target",
    "python3 - <<'PY' && cp source target\nprint(1)\nPY\n",
    "cp source target && python3 - <<'PY'\nprint(1)\nPY\n",
    "python3 - <<'PY'\nprint(1)\nPY\ngit add -A",
])
def test_heredoc_ambiguity_and_real_shell_writes_still_exclude_root_writer(
        repo, monkeypatch, route, command):
    import lib.project_mod.run_command as commands

    def forbidden(*_args, **_kwargs):
        pytest.fail('uncertain heredoc or real shell write reached execution')

    monkeypatch.setattr(commands, '_run_command_simple', forbidden)
    for name in ('_snapshot_targets', '_snapshot_project_files', '_run_cp_in_process'):
        monkeypatch.setattr(tools, name, forbidden)
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[workspace-busy]' in result
    assert (repo / 'target').read_text() == 'old\n'


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_mutating_prefix_does_not_borrow_literal_runtime_heredoc_exemption(
        repo, monkeypatch, route):
    import lib.project_mod.run_command as commands

    monkeypatch.setattr(commands, 'SHELL_PREFIX', 'touch prefix-target;')
    command = "python3 - <<'PY'\nprint(1)\nPY\n"
    with _other_writer(repo):
        if route == 'runner':
            result = commands.tool_run_command(str(repo), command)
        else:
            result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[workspace-busy]' in result
    assert not (repo / 'prefix-target').exists()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    "python3 - <<'PY'\nprint(1)\nPY\ncp source target &",
    "python3 - <<'PY' &\nprint(1)\nPY\n",
])
def test_true_background_tail_after_runtime_heredoc_is_not_hidden(repo, route, command):
    import lib.project_mod.run_command as commands
    if route == 'runner':
        result = commands.tool_run_command(str(repo), command)
    else:
        result = tools.execute_tool('run_command', {'command': command}, str(repo))
    assert '[workspace-lease-background-unsupported]' in result
    assert (repo / 'target').read_text() == 'old\n'


def test_literal_runtime_projection_does_not_replace_original_or_frozen_arguments(repo):
    import lib.project_mod.run_command as commands
    from lib.project_mod.command_heredoc import command_admission_view

    command = ("npm run test:unit && python3 - <<'PY' && echo header-tail\n"
               "# PY suffix is data, not a delimiter\n"
               "print(3 & 1)\nPY\necho real-tail")
    frozen_paths = {'candidate_digest': 'target'}
    view = command_admission_view(command)
    assert len(view) == len(command)
    assert 'print(3 & 1)' not in view and '<<' not in view
    assert 'npm run test:unit' in view
    assert 'echo header-tail' in view and 'echo real-tail' in view

    @commands._with_command_writer
    def admitted(base, received, *, frozen_input_paths):
        assert received is command
        assert frozen_input_paths is frozen_paths
        assert not workspace_lease_fds()
        return 'original source retained'

    with _other_writer(repo):
        assert admitted(str(repo), command, frozen_input_paths=frozen_paths) == 'original source retained'


@pytest.mark.parametrize('command', [
    "python3 - <<'PY'\nprint(1)\nPY \n",  # not an exact delimiter
    "python3 - <<'PY'\nprint(1)\nPY\ncp source target\nPY\n",  # first delimiter wins
    "python3 - <<P'Y'\nprint(1)\nPY\n",  # quote concatenation
    "python3 - <<'PY'2>&1\nprint(1)\nPY2\ncp source target\nPY\n",
    "python3 - <<\\PY\nprint(1)\nPY\n",  # escaped delimiter
])
def test_nonstandard_or_early_delimiter_never_hides_shell_writer(repo, command):
    import lib.project_mod.run_command as commands

    @commands._with_command_writer
    def forbidden(base, command):
        pytest.fail('nonstandard/early heredoc delimiter hid a shell writer')

    with _other_writer(repo):
        assert '[workspace-busy]' in forbidden(str(repo), command)


INSPECTION_CHAIN = (
    "/usr/bin/git branch --show-current; /usr/bin/git worktree list --porcelain; "
    "/usr/bin/git remote -v; /usr/bin/git for-each-ref "
    "--format='%(refname:short)|%(objectname:short)|%(committerdate:iso8601)|%(upstream:short)' "
    "refs/heads refs/remotes; /usr/bin/git status --porcelain | python3 -c "
    "'import sys; from collections import Counter; "
    'print("Main status counts:",dict(Counter(x[:2] for x in sys.stdin)))' + "'"
)


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_absolute_git_inspection_chain_runs_during_writer(repo, route):
    import lib.project_mod.run_command as commands
    index = repo / '.git' / 'index'
    subprocess.run(['git', '-C', str(repo), 'add', 'source'], check=True)
    before = index.read_bytes()
    with _other_writer(repo):
        result = (commands.tool_run_command(str(repo), INSPECTION_CHAIN) if route == 'runner'
                  else tools.execute_tool('run_command', {'command': INSPECTION_CHAIN}, str(repo)))
    assert '[exit code: 0]' in result
    assert 'Main status counts:' in result
    assert '[workspace-busy]' not in result
    assert index.read_bytes() == before


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_git_c_isolated_mutation_does_not_lease_parent(repo, tmp_path, route):
    import lib.project_mod.run_command as commands
    subprocess.run(['git', '-C', str(repo), 'add', 'source'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'initial'], check=True)
    donor = tmp_path / 'donor'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', 'donor', str(donor)], check=True)
    (donor / 'picked').write_text('isolated change\n')
    subprocess.run(['git', '-C', str(donor), 'add', 'picked'], check=True)
    subprocess.run(['git', '-C', str(donor), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'donor change'], check=True)
    revision = subprocess.check_output(['git', '-C', str(donor), 'rev-parse', 'HEAD']).decode().strip()
    child = repo / '.worktrees' / 'isolated'
    command = ('/usr/bin/git worktree add -b isolated ' + shlex.quote(str(child))
               + ' HEAD && /usr/bin/git -C ' + shlex.quote(str(child))
               + ' -c user.name=Test -c user.email=test@example.invalid cherry-pick ' + revision)
    before = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'])
    with _other_writer(repo):
        result = (commands.tool_run_command(str(repo), command) if route == 'runner'
                  else tools.execute_tool('run_command', {'command': command}, str(repo)))
    assert '[exit code: 0]' in result
    assert subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']) == before
    assert subprocess.check_output(['git', '-C', str(child), 'rev-parse', 'HEAD']) != before
    assert (child / 'picked').read_text() == 'isolated change\n'
    assert not (repo / 'picked').exists()
    # Existing isolated checkouts also target only their own lease.
    with _other_writer(repo):
        result = commands.tool_run_command(str(repo), '/usr/bin/git -C '
                                           + shlex.quote(str(child)) + ' reset --hard HEAD')
    assert '[exit code: 0]' in result
    with _other_writer(child):
        result = commands.tool_run_command(str(repo), '/usr/bin/git -C '
                                           + shlex.quote(str(child)) + ' reset --hard HEAD')
    assert '[workspace-busy]' in result


@pytest.mark.parametrize('suffix', [
    '; touch copied.txt', '; git reset --hard', '| sort -o copied.txt',
    '; git branch -f main HEAD', '; git worktree remove peer',
    "; git for-each-ref --format='%(signature:grade)'",
])
def test_read_chain_cannot_hide_mutation(repo, suffix):
    from lib.project_mod.run_command import command_write_lease_claims
    assert command_write_lease_claims(str(repo), INSPECTION_CHAIN + suffix)


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    'git -C \"\" status --short',
    'git diff --stat HEAD', 'git diff --cached --name-only', 'git show --stat HEAD',
    'git log --format="%h | %s" --max-count=2 HEAD', 'git ls-files --stage',
    'git ls-tree -r HEAD', 'git rev-parse --verify HEAD', 'git merge-base HEAD HEAD',
    'git rev-list --count HEAD', 'git cat-file -p HEAD',
    'git reflog show -1', 'git stash list', 'git config --get log.showSignature',
    'git describe --always HEAD', 'git name-rev --name-only HEAD',
    'git blame --line-porcelain source', 'git shortlog -s HEAD',
    'git branch --format=\"%(refname:short)\"',
    'git diff --stat HEAD | python3 -c "import sys; print(sys.stdin.read())"',
])
def test_local_git_query_catalog_is_parallel_and_helper_free(repo, route, command):
    import lib.project_mod.run_command as commands
    subprocess.run(['git', '-C', str(repo), 'add', 'source'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'initial'], check=True)
    helper = repo / 'helper'
    marker = repo / 'helper-ran'
    helper.write_text('#!/bin/sh\nprintf called > ' + shlex.quote(str(marker)) + '\n')
    helper.chmod(0o700)
    for key in ('core.fsmonitor', 'diff.external', 'core.pager', 'gpg.program'):
        subprocess.run(['git', '-C', str(repo), 'config', key, str(helper)], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'log.showSignature', 'true'], check=True)
    expected_code = 0
    if command.startswith('git branch --format='):
        # Git 2.11 lacks this read-only option; preserve its native error.
        expected_code = subprocess.run(['git', '-C', str(repo), 'branch',
                                        '--format=%(refname:short)'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    before = (repo / '.git' / 'index').read_bytes()
    with _other_writer(repo):
        result = (commands.tool_run_command(str(repo), command) if route == 'runner'
                  else tools.execute_tool('run_command', {'command': command}, str(repo)))
    assert f'[exit code: {expected_code}]' in result
    assert (repo / '.git' / 'index').read_bytes() == before
    assert not marker.exists()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_query_does_not_upgrade_disjoint_file_write_to_checkout_lock(repo, monkeypatch, route):
    import lib.project_mod.run_command as commands
    from lib.project_mod.workspace_lease import workspace_file_write_lease
    ready, release = threading.Event(), threading.Event()
    errors = []
    def hold():
        try:
            with workspace_file_write_lease(repo / 'source'):
                ready.set()
                assert release.wait(10)
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(5)
        command = 'git status --short && python3 -c "print(1)" && cp target copied'
        result = (commands.tool_run_command(str(repo), command) if route == 'runner'
                  else tools.execute_tool('run_command', {'command': command}, str(repo)))
        assert '[exit code: 0]' in result
        assert (repo / 'copied').read_text() == 'old\n'
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors



def test_git_alias_changed_during_admission_refuses_before_body(repo, tmp_path, monkeypatch):
    import lib.project_mod.run_command as commands
    import lib.project_mod.workspace_lease as leases
    other = tmp_path / 'other'
    subprocess.run(['git', 'init', '-q', str(other)], check=True)
    alias = tmp_path / 'checkout-alias'
    alias.symlink_to(repo, target_is_directory=True)
    original = leases.workspace_write_lease
    @contextmanager
    def changed(path, **kwargs):
        with original(path, **kwargs):
            alias.unlink()
            alias.symlink_to(other, target_is_directory=True)
            yield
    monkeypatch.setattr(leases, 'workspace_write_lease', changed)
    monkeypatch.setattr(commands, '_run_command_simple',
                        lambda *a, **kw: pytest.fail('stale Git target reached execution'))
    run = commands._with_command_writer(commands.tool_run_command.__wrapped__)
    result = run(str(repo), 'git -C ' + shlex.quote(str(alias)) + ' add .')
    assert '[workspace-target-changed]' in result
    assert 'command was not started' in result.lower()
    assert not (other / '.git' / 'index').exists()
