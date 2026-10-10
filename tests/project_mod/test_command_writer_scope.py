"""Typed command writer admission: only verified single files narrow ROOT.

The parser's opaque-runtime scheduling exemption is not a readonly guarantee.
Concurrency uses private Git repositories and bounded holder lifetimes.
"""
from contextlib import contextmanager
import os
import subprocess
import threading

import pytest

from lib.project_mod import run_command as commands, tools, workspace_lease

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setenv('TOFU_RUN_CP_FASTPATH', '1')
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    root = tmp_path / 'checkout'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'source').write_text('new\n')
    (root / 'target').write_text('old\n')
    (root / 'other').write_text('other\n')
    (root / 'outdir').mkdir()
    (root / 'alias').symlink_to(root / 'target')
    (root / 'dir-alias').symlink_to(root / 'outdir', target_is_directory=True)
    os.link(root / 'other', root / 'hardlink')
    return root


@contextmanager
def _other_file_writer(path):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with workspace_lease.workspace_file_write_lease(path):
                ready.set()
                assert release.wait(10), 'file writer release was not signalled'
        except BaseException as error:
            errors.append(error)
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


def _run(repo, route, command):
    if route == 'runner':
        return commands.tool_run_command(str(repo), command)
    return tools.execute_tool('run_command', {'command': command}, str(repo))


@pytest.mark.parametrize('command,target', [
    ('cp source target', 'target'),
    ('cp -- source target', 'target'),
    ('echo changed > target', 'target'),
    ('printf changed 2>> target', 'target'),
    ('echo changed > "space target"', 'space target'),
    ('touch target', 'target'),
    ('tee -a target', 'target'),
    ('truncate -s 0 target', 'target'),
    ('sort -o target source', 'target'),
])
def test_regular_file_claim_does_not_implicitly_claim_cwd(repo, command, target):
    claims = commands.command_write_lease_claims(str(repo), command)
    assert claims == (('file', str(repo / target)),)
    assert commands.command_write_lease_paths(str(repo), command) == (str(repo / target),)


@pytest.mark.parametrize('command', [
    'cp source outdir', 'cp -r outdir newdir', 'cp -b source target',
    'cp --parents source outdir', 'cp -t outdir source',
    'cp source target outdir', 'mv source target', 'rm -rf outdir',
    'cp source missing-parent/target', 'cp source alias',
    'cp source dir-alias/created', 'echo changed > hardlink',
    'echo changed > dir-alias/../target', 'cp source outdir/../target',
    'echo changed > .git/../target', 'cp alias target',
    'cp source .git/config', 'echo changed > .git/config',
    'echo changed > outdir', 'echo changed > target > other',
    'echo changed 3> target', 'echo changed > "$OUTPUT"',
    'echo changed > target 2>&1', 'cp source target 2>&1',
    'cp source target # uncertain tail',
    'touch target -c', 'tee target -a', 'POSIXLY_CORRECT=1 touch target -c',
    'touch target --', 'truncate target -s 0',
])
def test_directory_alias_metadata_and_uncertain_sets_remain_root(repo, command):
    claims = commands.command_write_lease_claims(str(repo), command)
    assert ('root', str(repo)) in claims
    assert commands.command_write_lease_paths(str(repo), command) == tuple(
        sorted({path for _scope, path in claims}))


def test_literal_file_target_outside_cwd_does_not_claim_starting_checkout(repo, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    subprocess.run(['git', 'init', '-q', str(outside)], check=True)
    target = outside / 'file'
    command = f'cp source {target}'
    assert commands.command_write_lease_claims(str(repo), command) == (('file', str(target)),)


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('fast_copy', [True, False])
def test_file_a_holder_does_not_block_file_b_mutator(repo, monkeypatch, route, fast_copy):
    monkeypatch.setenv('TOFU_RUN_CP_FASTPATH', '1' if fast_copy else '0')
    with _other_file_writer(repo / 'target'):
        result = _run(repo, route, 'cp source created')
    assert '[exit code: 0]' in result
    assert (repo / 'created').read_text() == 'new\n'
    assert (repo / 'target').read_text() == 'old\n'
    assert not workspace_lease.workspace_lease_fds()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', ['cp source target', 'echo changed > target', 'git add -A'])
def test_same_file_and_git_root_writer_still_block_before_snapshot_or_spawn(
        repo, monkeypatch, route, command):
    def forbidden(*_args, **_kwargs):
        pytest.fail('conflicting writer reached snapshot, copy, process, or journal')

    monkeypatch.setattr(commands.subprocess, 'Popen', forbidden)
    for name in ('_snapshot_targets', '_snapshot_project_files',
                 '_run_cp_in_process', '_record_run_command_changes'):
        monkeypatch.setattr(tools, name, forbidden)
    with _other_file_writer(repo / 'target'):
        result = _run(repo, route, command)
    assert '[workspace-busy]' in result
    assert 'Command was not started' in result
    assert (repo / 'target').read_text() == 'old\n'
    assert not workspace_lease.workspace_lease_fds()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_multiple_literal_segments_keep_all_file_claims(repo, monkeypatch, route):
    command = 'cp source target && echo changed > created'
    assert commands.command_write_lease_claims(str(repo), command) == (
        ('file', str(repo / 'created')), ('file', str(repo / 'target')))
    with _other_file_writer(repo / 'target'):
        assert '[workspace-busy]' in _run(repo, route, command)
    assert not (repo / 'created').exists()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_descriptor_duplication_without_file_output_does_not_claim_writer(repo, route):
    with _other_file_writer(repo / 'target'):
        result = _run(repo, route, "python3 -c 'print(1)' 2>&1")
    assert '[exit code: 0]' in result
    assert not workspace_lease.workspace_lease_fds()


@pytest.mark.parametrize('command', ['echo hi > target &', 'cp source target &'])
def test_scoped_file_writer_cannot_escape_supervision(repo, command):
    result = commands.tool_run_command(str(repo), command)
    assert '[workspace-lease-background-unsupported]' in result
    assert (repo / 'target').read_text() == 'old\n'
    assert not workspace_lease.workspace_lease_fds()


def test_external_git_metadata_remains_a_root_action(tmp_path):
    root, metadata = tmp_path / 'separate-checkout', tmp_path / 'git-storage'
    subprocess.run(['git', 'init', '-q', '--separate-git-dir', str(metadata), str(root)], check=True)
    command = f'echo changed > {metadata / "config"}'
    claims = commands.command_write_lease_claims(str(root), command)
    assert ('root', str(root)) in claims
    assert not any(scope == 'file' for scope, _path in claims)


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_actual_file_mutator_a_overlaps_actual_file_mutator_b(repo, monkeypatch, route):
    monkeypatch.setenv('TOFU_RUN_CP_FASTPATH', '0')
    entered, release = threading.Event(), threading.Event()
    outcomes, errors = [], []
    process = commands._run_command_simple

    def pause_a(execution_source, *args, **kwargs):
        if 'cp source target' in execution_source:
            assert len(workspace_lease.workspace_lease_fds()) == 2
            entered.set()
            assert release.wait(10)
        return process(execution_source, *args, **kwargs)

    def execute_a():
        try:
            outcomes.append(_run(repo, route, 'cp source target'))
        except BaseException as error:
            errors.append(error)
            entered.set()

    monkeypatch.setattr(commands, '_run_command_simple', pause_a)
    worker = threading.Thread(target=execute_a)
    worker.start()
    try:
        assert entered.wait(5)
        assert not errors
        result = _run(repo, route, 'echo changed > created')
        assert '[exit code: 0]' in result
        assert (repo / 'created').read_text() == 'changed\n'
        assert (repo / 'target').read_text() == 'old\n'
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert len(outcomes) == 1 and '[exit code: 0]' in outcomes[0]
    assert (repo / 'target').read_text() == 'new\n'
    assert not workspace_lease.workspace_lease_fds()


def test_mixed_root_and_file_claims_order_by_checkout_not_scope(repo, tmp_path):
    outside = tmp_path / 'aaa-other-checkout'
    outside.mkdir()
    subprocess.run(['git', 'init', '-q', str(outside)], check=True)
    target = outside / 'file'
    command = f'cp source {target} && git add -A'
    assert commands.command_write_lease_claims(str(repo), command) == (
        ('file', str(target)), ('root', str(repo)))


def test_mixed_claims_order_by_checkout_even_when_leaf_path_sorts_later(repo):
    nested = repo / 'outdir'
    subprocess.run(['git', 'init', '-q', str(nested)], check=True)
    command = 'cp source zz-parent-file && cd outdir && git add -A'
    assert commands.command_write_lease_claims(str(repo), command) == (
        ('file', str(repo / 'zz-parent-file')), ('root', str(nested)))
