"""Command admission claims actual effects without replaying shell source."""
import shlex

import pytest

from lib.project_mod.run_command import command_write_lease_claims
from lib.project_mod.git_command_scope import git_command_scopes

pytestmark = pytest.mark.unit


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'repo'
    (root / '.git').mkdir(parents=True)
    for name in ('a', 'b'):
        (root / name).write_text(name)
    return root


@pytest.mark.parametrize('command', ['pytest -q', 'npm test', 'make build', 'python3 -c "print(1)"', 'mkdir -p outputs'])
def test_low_risk_commands_remain_free(checkout, command):
    assert command_write_lease_claims(str(checkout), command) == ()


@pytest.mark.parametrize('command', ['git branch topic', 'git branch -D topic', 'git tag topic', 'git update-ref refs/heads/topic HEAD'])
def test_metadata_risk_is_not_checkout_scope(checkout, command):
    assert command_write_lease_claims(str(checkout), command) == (('metadata', str(checkout)),)


def test_commit_helpers_require_conservative_scope(checkout):
    assert command_write_lease_claims(str(checkout), 'git commit -m message') == (('metadata', str(checkout)),)
    hook = checkout / '.git' / 'hooks' / 'pre-commit'
    hook.parent.mkdir()
    hook.write_text('#!/bin/sh\nexit 0\n')
    hook.chmod(0o700)
    assert set(command_write_lease_claims(str(checkout), 'git commit -m message')) == {
        ('tree', str(checkout)), ('metadata', str(checkout))}
    safe = 'git -c core.hooksPath=/dev/null -c commit.gpgSign=false -c core.fsmonitor=false commit -m message'
    assert command_write_lease_claims(str(checkout), safe) == (('metadata', str(checkout)),)


@pytest.mark.parametrize('operation', ['remove --force {source}', 'move {source} {target}'])
def test_external_worktree_lifecycle_targets(checkout, tmp_path, operation):
    source, target = tmp_path / 'external', tmp_path / 'moved'
    source.mkdir()
    command = 'git worktree ' + operation.format(source=shlex.quote(str(source)), target=shlex.quote(str(target)))
    scope = git_command_scopes(command, str(checkout), environment={})[0]
    expected = {('metadata', str(checkout)), ('tree', str(source))}
    if operation.startswith('move'):
        expected.add(('tree', str(target)))
    assert set(scope.writer_claims()) == expected
    assert set(command_write_lease_claims(str(checkout), command)) == expected


@pytest.mark.parametrize('command, names', [('rm a b', ['a', 'b']), ('mv a c', ['a', 'c']), ('cp a c', ['c']), ('touch a b', ['a', 'b'])])
def test_regular_file_sets_are_precise(checkout, command, names):
    assert set(command_write_lease_claims(str(checkout), command)) == {('file', str(checkout / name)) for name in names}


def test_directory_delete_is_actual_tree_not_cwd(checkout):
    (checkout / 'outputs').mkdir()
    assert command_write_lease_claims(str(checkout), 'rm -rf outputs') == (('tree', str(checkout / 'outputs')),)


def test_dynamic_significant_write_fails_closed(checkout):
    assert ('tree', str(checkout)) in command_write_lease_claims(str(checkout), 'rm -rf "$TARGET"')


def test_unknown_worktree_option_refused(checkout):
    with pytest.raises(ValueError, match='Git worktree lifecycle'):
        command_write_lease_claims(str(checkout), 'git worktree move --unknown a b')


@pytest.mark.parametrize('command', ['git branch topic', 'git tag topic', 'pytest -q', 'touch b'])
def test_nonoverlapping_commands_coexist_with_edit(checkout, monkeypatch, command):
    import threading
    from lib.project_mod.run_command import _with_command_writer
    from lib.project_mod.workspace_lease import workspace_file_write_lease

    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with workspace_file_write_lease(checkout / 'a'):
                ready.set()
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(5) and not errors
        runner = _with_command_writer(lambda base, source: 'ran')
        assert runner(str(checkout), command) == 'ran'
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive() and not errors


@pytest.mark.parametrize('operation', ['remove {source}', 'move {source} {target}'])
def test_external_lifecycle_blocks_active_edit(checkout, tmp_path, monkeypatch, operation):
    import threading
    from lib.project_mod.run_command import _with_command_writer
    from lib.project_mod.workspace_lease import workspace_file_write_lease

    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    source = tmp_path / 'external'
    source.mkdir()
    (source / '.git').mkdir()
    leaf = source / 'editing'
    leaf.write_text('original')
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with workspace_file_write_lease(leaf):
                ready.set()
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(5) and not errors
        runner = _with_command_writer(lambda base, command: pytest.fail('busy lifecycle executed'))
        command = 'git worktree ' + operation.format(source=shlex.quote(str(source)), target=shlex.quote(str(tmp_path / 'moved')))
        assert '[workspace-busy]' in runner(str(checkout), command)
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive() and not errors


def test_worktree_operand_resolution_ignores_cwd_named_worktree(checkout, tmp_path):
    nested = checkout / 'worktree'
    (nested / '.git').mkdir(parents=True)
    source = tmp_path / 'external'
    source.mkdir()
    command = f'git -C worktree worktree remove {shlex.quote(str(source))}'
    claims = set(command_write_lease_claims(str(checkout), command))
    assert claims == {('metadata', str(nested)), ('tree', str(source))}


def test_subcommand_copy_option_is_not_global_helper_disabling_config(checkout):
    hook = checkout / '.git' / 'hooks' / 'reference-transaction'
    hook.parent.mkdir()
    hook.write_text('#!/bin/sh\nexit 0\n')
    hook.chmod(0o700)
    command = 'git branch -c core.hooksPath=/dev/null'
    assert command_write_lease_claims(str(checkout), command) == (('tree', str(checkout)), ('metadata', str(checkout)))


def test_signing_is_not_disabled_by_disabling_hooks(checkout):
    command = 'git -c core.hooksPath=/dev/null tag -a topic -m message'
    assert not any(scope == 'metadata' for scope, _ in command_write_lease_claims(str(checkout), command))


def test_redirected_external_index_is_an_actual_effect(checkout, tmp_path):
    index = tmp_path / 'external-index'
    index.write_text('index')
    command = f'GIT_INDEX_FILE={shlex.quote(str(index))} git reset --hard HEAD'
    claims = set(command_write_lease_claims(str(checkout), command))
    assert ('tree', str(index)) in claims
    assert ('tree', str(checkout)) in claims


def test_unknown_git_shell_alias_has_descendant_protection(checkout):
    assert command_write_lease_claims(str(checkout), 'git custom-cleanup') == (('tree', str(checkout)), ('metadata', str(checkout)))


def test_configured_git_filter_cannot_bypass_active_nested_checkout(checkout):
    (checkout / '.git' / 'config').write_text('[filter "custom"]\n  smudge = helper\n')
    assert command_write_lease_claims(str(checkout), 'git checkout other') == (('tree', str(checkout)), ('metadata', str(checkout)))


@pytest.mark.parametrize('config', [
    '-c include.path={path}', '-cinclude.path={path}',
    '-c includeIf.gitdir:*.path={path}',
    '-c core.hooksPath=/dev/null -c include.path={path}',
])
def test_inline_include_cannot_borrow_metadata_only_admission(checkout, tmp_path, config):
    include = tmp_path / 'extra.conf'
    include.write_text('[core]\n  hooksPath = ' + str(tmp_path / 'hooks') + '\n')
    command = 'git ' + config.format(path=shlex.quote(str(include))) + ' branch topic'
    assert command_write_lease_claims(str(checkout), command) == (('tree', str(checkout)), ('metadata', str(checkout)))


def test_attached_hook_config_is_an_executable_tree_effect(checkout, tmp_path):
    command = f'git -ccore.hooksPath={shlex.quote(str(tmp_path / "hooks"))} branch topic'
    assert command_write_lease_claims(str(checkout), command) == (('tree', str(checkout)), ('metadata', str(checkout)))


@pytest.mark.parametrize('redirect, names', [
    ('> out', ['out']), ('>> out', ['out']), ('>out 2>errors', ['out', 'errors']),
    ('> out 2>&1', ['out']), ("> '&output'", ['&output']),
    ("> '$OUTPUT'", ['$OUTPUT']), ('<> out', ['out']),
])
def test_git_native_and_output_effects_are_unioned(checkout, redirect, names):
    claims = set(command_write_lease_claims(str(checkout), 'git branch topic ' + redirect))
    assert claims == {('metadata', str(checkout))} | {('file', str(checkout / name)) for name in names}


def test_git_output_uses_shell_cwd_not_git_cwd(checkout, tmp_path):
    peer = tmp_path / 'peer'
    (peer / '.git').mkdir(parents=True)
    command = f'git -C {shlex.quote(str(peer))} branch topic > output'
    assert set(command_write_lease_claims(str(checkout), command)) == {
        ('metadata', str(peer)), ('file', str(checkout / 'output'))}


@pytest.mark.parametrize('command', [
    'git branch topic > "$OUTPUT"', 'git branch topic >&4', 'git branch topic >&output',
    'git branch topic >', "git commit -m '$OUTPUT' > $OUTPUT",
])
def test_unresolved_git_output_is_refused_before_execution(checkout, command):
    with pytest.raises(ValueError, match='Git shell output target'):
        command_write_lease_claims(str(checkout), command)


def test_quoted_redirect_operator_is_a_commit_message_not_output(checkout):
    command = 'git -c core.hooksPath=/dev/null -c commit.gpgSign=false -c core.fsmonitor=false commit -m ">"'
    assert command_write_lease_claims(str(checkout), command) == (('metadata', str(checkout)),)


@pytest.mark.parametrize('command', ['git branch topic > a', 'git -c include.path=extra.conf branch topic'])
def test_git_omitted_effects_cannot_overlap_real_edit(checkout, monkeypatch, command):
    from lib.project_mod.run_command import _with_command_writer
    from lib.project_mod.workspace_lease import workspace_file_write_lease
    import threading

    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with workspace_file_write_lease(checkout / 'a'):
                ready.set()
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)
            ready.set()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5) and not errors
        runner = _with_command_writer(lambda *args: pytest.fail('conflicting Git command started'))
        assert '[workspace-busy]' in runner(str(checkout), command)
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive() and not errors


def test_prospective_worktree_has_metadata_and_destination_claims(checkout, tmp_path):
    future = tmp_path / 'future'
    command = f'git worktree add --detach {shlex.quote(str(future))} HEAD && git -C {shlex.quote(str(future))} cherry-pick revision'
    assert set(command_write_lease_claims(str(checkout), command)) == {
        ('metadata', str(checkout)), ('tree', str(future))}


def test_future_checkout_edits_cannot_enter_mid_compound_creation(checkout, tmp_path, monkeypatch):
    import threading
    from lib.project_mod.run_command import _with_command_writer
    from lib.project_mod.workspace_lease import workspace_file_write_lease, WorkspaceBusyError

    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    future = tmp_path / 'future'
    errors, observations = [], []

    def create_and_probe(*args):
        (future / '.git').mkdir(parents=True)
        (future / 'a').write_text('before')

        def edit():
            try:
                with pytest.raises(WorkspaceBusyError):
                    with workspace_file_write_lease(future / 'a'):
                        pytest.fail('prospective checkout lost its live tree fence')
                observations.append('excluded')
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=edit)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and not errors
        return 'created'

    command = f'git worktree add --detach {shlex.quote(str(future))} HEAD && git -C {shlex.quote(str(future))} cherry-pick revision'
    assert _with_command_writer(create_and_probe)(str(checkout), command) == 'created'
    assert observations == ['excluded']


def test_irrelevant_credential_and_pager_settings_do_not_widen_native_checkout(checkout):
    (checkout / '.git' / 'config').write_text('[credential]\n  helper = store\n[core]\n  pager = less\n')
    assert command_write_lease_claims(str(checkout), 'git cherry-pick revision') == (('root', str(checkout)),)


def test_explicit_git_pager_remains_an_executable_effect(checkout):
    assert command_write_lease_claims(str(checkout), 'git --paginate branch topic') == (('tree', str(checkout)), ('metadata', str(checkout)))


def test_git_helper_from_subdirectory_fences_the_actual_checkout(checkout):
    nested = checkout / 'nested'
    nested.mkdir()
    command = 'git -c include.path=extra.conf branch topic'
    assert set(command_write_lease_claims(str(nested), command)) == {
        ('tree', str(checkout)), ('metadata', str(checkout))}


def test_linked_git_tree_effect_cannot_bypass_common_metadata(checkout, monkeypatch):
    import threading
    from lib.project_mod.run_command import _with_command_writer
    from lib.project_mod.workspace_lease import workspace_git_metadata_lease

    child = checkout / '.worktrees' / 'child'
    child.mkdir(parents=True)
    admin = checkout / '.git' / 'worktrees' / 'child'
    admin.mkdir(parents=True)
    (admin / 'commondir').write_text('../..')
    (child / '.git').write_text('gitdir: ' + str(admin))
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with workspace_git_metadata_lease(checkout):
                ready.set()
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)
            ready.set()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5) and not errors
        runner = _with_command_writer(lambda *args: pytest.fail('common Git metadata effects overlapped'))
        assert '[workspace-busy]' in runner(str(child), 'git custom-command')
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive() and not errors


@pytest.mark.parametrize('hook, command', [
    ('post-rewrite', 'git commit --amend --no-edit'),
    ('post-index-change', 'git -c core.fsmonitor=false add --refresh'),
    ('post-index-change', 'git write-tree'),
])
def test_index_and_rewrite_hooks_are_not_metadata_only(checkout, hook, command):
    path = checkout / '.git' / 'hooks' / hook
    path.parent.mkdir()
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o700)
    assert set(command_write_lease_claims(str(checkout), command)) == {
        ('tree', str(checkout)), ('metadata', str(checkout))}


@pytest.mark.parametrize('args', ['-a -m message', '-m message -- a', '--interactive -m message'])
def test_staging_commits_do_not_borrow_metadata_exemption(checkout, args):
    assert command_write_lease_claims(str(checkout), 'git commit ' + args) == (('root', str(checkout)),)
