"""Model tool calls take no workspace lease and never wait for admission.

Owner decision 2026-10-28, recorded in docs/runbooks/GIT_WORKSPACE_GUARD.md
("Tool calls take no workspace leases"). Commands and file writes start at
once whatever another task or Tofu's own whole-workspace operation holds; the
only workspace protection left for tool calls is the Git guard for
tree-destroying Git, and one file's read-modify-write stays atomic through the
per-file edit mutex. Access control (read-only roots) is unchanged. Only
private temporary Git checkouts are used.
"""
from contextlib import contextmanager
import subprocess
import threading
import time

import pytest

from lib.project_mod import git_guard, tools, workspace_lease
from lib.project_mod.file_edit_mutex import _entries, file_edit_mutex
from lib.project_mod.tool_admission import WORKSPACE_ADMISSION_KEY
from tests.meta.test_git_guard_pipeline import (
    _clean_guard_state as _clean_guard_state, _invoke, _peer, harness as harness,
)

pytestmark = pytest.mark.unit

_GIT = ['git', '-c', 'user.email=t@example.invalid', '-c', 'user.name=t']


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    # Were a tool call still admitted through a lease, it would fail fast
    # instead of waiting for the holder below.
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '1')
    root = tmp_path / 'checkout'
    (root / 'lib').mkdir(parents=True)
    (root / 'lib' / 'x1.py').write_text('value = 1\n')
    (root / 'notes.txt').write_text('alpha\nbeta\n')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run([*_GIT, '-C', str(root), 'add', '-A'], check=True)
    subprocess.run([*_GIT, '-C', str(root), 'commit', '-q', '-m', 'base'], check=True)
    return root


def _task(root, name, **config):
    return {'id': f'task-{name}', 'convId': f'conv-{name}',
            'config': {'projectPath': str(root), **config}}


_INTERNAL_WRITER = 'internal-whole-workspace-writer'


@pytest.fixture
def tool_thread_leases(monkeypatch):
    """Lease acquisitions made by any tool-call thread; must stay empty."""
    entries = []
    original = workspace_lease._lease

    def recording(path, *args, **kwargs):
        if threading.current_thread().name != _INTERNAL_WRITER:
            entries.append((threading.current_thread().name, str(path)))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(workspace_lease, '_lease', recording)
    return entries


@contextmanager
def _internal_whole_workspace_writer(root):
    """Hold the exclusive checkout lease Tofu's own delivery/checkpoints use."""
    ready, release, failures = threading.Event(), threading.Event(), []

    def hold():
        try:
            with workspace_lease.workspace_write_lease(str(root)):
                ready.set()
                release.wait(30)
        except BaseException as error:
            failures.append(error)
            ready.set()

    worker = threading.Thread(target=hold, name=_INTERNAL_WRITER)
    worker.start()
    try:
        assert ready.wait(10) and not failures
        yield
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not failures


def _run(root, command, task):
    return tools.execute_tool('run_command', {'command': command}, str(root), task=task)


def test_writing_command_starts_while_another_task_runs_a_long_command(repo, tool_thread_leases):
    # `> "$PWD/..."` was an unattributed tree fence on the checkout: under
    # admission every later writer in that checkout queued behind it.
    long_task = _task(repo, 'long')
    results = {}

    def long_command():
        results['long'] = _run(repo, 'touch started; sleep 60 > "$PWD/long.out"', long_task)

    holder = threading.Thread(target=long_command, name='long-command')
    holder.start()
    try:
        deadline = time.monotonic() + 30
        while not (repo / 'started').exists():
            assert time.monotonic() < deadline, 'the long command never started'
            time.sleep(0.05)
        began = time.monotonic()
        result = _run(repo, 'echo fresh > out.txt && git add out.txt', _task(repo, 'short'))
        elapsed = time.monotonic() - began
        assert '[exit code: 0]' in result, result
        assert (repo / 'out.txt').read_text() == 'fresh\n'
        assert holder.is_alive(), 'the other task finished first; nothing was proven'
        assert elapsed < 10, elapsed
    finally:
        long_task['aborted'] = True
        holder.join(30)
    assert not holder.is_alive()
    assert tool_thread_leases == []


@pytest.mark.parametrize('command', [
    'git log -E --grep=x --oneline',
    'git ls-files lib/x*',
    'for f in $(git ls-files lib); do wc -l "$f"; done',
])
def test_formerly_fenced_read_only_shapes_start_during_internal_writer(
        repo, tool_thread_leases, command):
    task = _task(repo, 'reader')
    with _internal_whole_workspace_writer(repo):
        began = time.monotonic()
        result = _run(repo, command, task)
        elapsed = time.monotonic() - began
    assert '[exit code: 0]' in result, result
    assert '[workspace-busy]' not in result
    assert elapsed < 10, elapsed
    assert tool_thread_leases == []
    assert WORKSPACE_ADMISSION_KEY not in task


@pytest.mark.parametrize('command', [
    'WT={tree}; git -C "$WT" status --short; git worktree remove --force "$WT"',
    'R={root}; git -C $R worktree remove $R/.wt/tree',
    'git worktree add -q --detach .wt/fresh HEAD && cd .wt/fresh && cd {root} '
    '&& git worktree remove .wt/fresh && git worktree remove .wt/tree',
    'git worktree remove -- {root}/.wt/tre*',
])
def test_worktree_lifecycle_with_shell_resolved_operands_starts(repo, tool_thread_leases, command):
    # Formerly refused before start as `workspace-target-unresolved`: execution
    # preparation resolved lease effects for Git segments it never rewrites.
    tree = repo / '.wt' / 'tree'
    subprocess.run([*_GIT, '-C', str(repo), 'worktree', 'add', '-q', '--detach', str(tree)],
                   check=True)
    result = _run(repo, command.format(root=repo, tree=tree), _task(repo, 'cleanup'))
    assert '[exit code: 0]' in result, result
    assert not tree.exists()
    assert not (repo / '.wt' / 'fresh').exists()
    assert tool_thread_leases == []


def test_file_writes_and_edits_take_no_lease_during_internal_writer(repo, tool_thread_leases):
    task = _task(repo, 'writer')
    with _internal_whole_workspace_writer(repo):
        written = tools.execute_tool('write_file', {'path': 'new.txt', 'content': 'x\n'},
                                     str(repo), task=task)
        tools.execute_tool('read_files', {'path': 'notes.txt'}, str(repo), task=task)
        edited = tools.execute_tool('edit_file', {'path': 'notes.txt', 'edits': [
            {'operation': 'replace', 'anchor': 'alpha', 'content': 'ALPHA'}]},
            str(repo), task=task)
    assert written.startswith('File created'), written
    assert edited.startswith('Applied 1/1'), edited
    assert (repo / 'notes.txt').read_text() == 'ALPHA\nbeta\n'
    assert tool_thread_leases == []
    assert WORKSPACE_ADMISSION_KEY not in task


def test_two_concurrent_edit_file_calls_on_one_file_both_apply(repo, monkeypatch):
    from lib.project_mod.write_tools import _ops
    task = _task(repo, 'parallel-round')
    tools.execute_tool('read_files', {'path': 'notes.txt'}, str(repo), task=task)
    original_read = _ops._read_original_text
    overlapping = threading.Barrier(2, timeout=10)
    readers = []

    def slow_read(target):
        # Hold the first preimage so the second call is truly concurrent.
        readers.append(threading.get_ident())
        if len(readers) == 1:
            time.sleep(0.3)
        return original_read(target)

    monkeypatch.setattr(_ops, '_read_original_text', slow_read)
    results = {}

    def edit(anchor, content):
        overlapping.wait()
        results[anchor] = tools.execute_tool('edit_file', {'path': 'notes.txt', 'edits': [
            {'operation': 'replace', 'anchor': anchor, 'content': content}]},
            str(repo), task=task)

    workers = [threading.Thread(target=edit, args=args)
               for args in (('alpha', 'ALPHA'), ('beta', 'BETA'))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    assert all(not worker.is_alive() for worker in workers)
    assert results['alpha'].startswith('Applied 1/1'), results
    assert results['beta'].startswith('Applied 1/1'), results
    assert (repo / 'notes.txt').read_text() == 'ALPHA\nBETA\n'
    assert _entries == {}


def test_file_edit_mutex_serializes_one_path_and_never_another(tmp_path):
    first, second = str(tmp_path / 'a'), str(tmp_path / 'b')
    inside, release, order = threading.Event(), threading.Event(), []

    def holder():
        with file_edit_mutex([first]):
            inside.set()
            release.wait(10)
            order.append('holder')

    def contender():
        with file_edit_mutex([first]):
            order.append('same path')

    workers = [threading.Thread(target=holder), threading.Thread(target=contender)]
    workers[0].start()
    assert inside.wait(10)
    with file_edit_mutex([second]):
        order.append('other path')  # never waits for an unrelated file
    workers[1].start()
    time.sleep(0.2)
    assert order == ['other path']  # the same file waits for its holder
    release.set()
    for worker in workers:
        worker.join(10)
    assert order == ['other path', 'holder', 'same path']
    with file_edit_mutex([first, second, first]):
        with file_edit_mutex([second]):  # reentrant on its owning thread
            pass
    assert _entries == {}


def test_read_only_roots_still_refuse_writes_and_destructive_commands(repo):
    task = _task(repo, 'readonly', readOnlyPaths=[str(repo)])
    written = tools.execute_tool('write_file', {'path': 'new.txt', 'content': 'x\n'},
                                 str(repo), task=task)
    command = _run(repo, 'echo x > forbidden.txt', task)
    assert written.startswith('Write failed'), written
    assert 'READ-ONLY' in command, command
    assert not (repo / 'new.txt').exists()
    assert not (repo / 'forbidden.txt').exists()


@pytest.mark.parametrize('command', [
    'git stash', 'git checkout main', 'git switch main', 'git reset --hard', 'git clean -fd',
])
def test_whole_workspace_git_keeps_the_git_guard(harness, monkeypatch, command):
    assert git_guard.classify(command) == 'tree_destroying'
    monkeypatch.setattr(git_guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [_peer()])
    stopped, tool_results = _invoke(command=command)
    assert stopped is True
    assert tool_results['call-1'][1] is False
    assert harness['rejections'][0]['descriptor']['kind'] == 'git_guard'
    monkeypatch.setattr(git_guard, 'active_peers',
                        lambda root, user_id, exclude_conv='': [])
    stopped, _tool_results = _invoke(command=command)
    assert stopped is False
    assert harness['receipts'][-1]['authority'] == 'agent_git_guard_solo'
