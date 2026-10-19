"""Git coordination follows concrete checkout/index effects and shell control."""
import shlex

import pytest

from lib.project_mod.git_command_scope import git_command_scopes
from lib.project_mod.run_command import command_write_lease_paths

pytestmark = pytest.mark.unit


@pytest.fixture
def roots(tmp_path):
    parent = tmp_path / 'shared'
    (parent / '.git').mkdir(parents=True)
    child = parent / '.worktrees' / 'linked'
    child.mkdir(parents=True)
    metadata = parent / '.git' / 'worktrees' / 'linked'
    metadata.mkdir(parents=True)
    (metadata / 'commondir').write_text('../..')
    (child / '.git').write_text('gitdir: ' + str(metadata))
    (child / 'nested').mkdir()
    return parent, child


@pytest.mark.parametrize('prefix', ['git -C {child}', 'git -C .worktrees -C linked',
                                    'git -C{child}', 'cd {child} && git',
                                    'cd {child}/nested && git -C ..'])
def test_isolated_writer_has_no_invocation_root_claim(roots, prefix):
    parent, child = roots
    command = prefix.format(child=shlex.quote(str(child))) + ' reset --hard HEAD'
    assert command_write_lease_paths(str(parent), command) == (str(child),)
    scopes = git_command_scopes(command, str(parent), environment={})
    assert scopes[-1].presence_roots() == (str(child),)


@pytest.mark.parametrize('separator', [';', '||', '|'])
def test_failed_cd_or_pipeline_cannot_remove_original_checkout(roots, separator):
    parent, child = roots
    command = 'cd ' + shlex.quote(str(child)) + ' ' + separator + ' git reset --hard'
    expected = (str(parent),) if separator == '|' else tuple(sorted(map(str, roots)))
    scopes = git_command_scopes(command, str(parent), environment={})
    assert scopes[-1].presence_roots() == expected
    assert command_write_lease_paths(str(parent), command) == expected


@pytest.mark.parametrize('argument', ['-C another', '--work-tree=another', '--git-dir=another'])
def test_commit_message_and_pathspec_do_not_select_checkout(roots, argument):
    parent, child = roots
    command = ('git -C ' + shlex.quote(str(child)) + ' commit --amend -m '
               + shlex.quote(argument) + ' -- ' + shlex.quote(argument))
    assert command_write_lease_paths(str(parent), command) == (str(child),)


@pytest.mark.parametrize('override', ['--git-dir', '--git-dir=', 'GIT_DIR=', 'GIT_INDEX_FILE=', 'env GIT_DIR='])
def test_metadata_override_claims_actual_index_owner(roots, override):
    parent, child = roots
    index_path = parent / '.git' / 'index'
    target = index_path if override == 'GIT_INDEX_FILE=' else parent / '.git'
    if override.startswith(('GIT_', 'env GIT_')):
        command = override + shlex.quote(str(target)) + ' git -C ' + shlex.quote(str(child)) + ' reset --hard'
    else:
        sep = ' ' if override == '--git-dir' else ''
        command = 'git -C ' + shlex.quote(str(child)) + ' ' + override + sep + shlex.quote(str(target)) + ' reset --hard'
    scopes = git_command_scopes(command, str(parent), environment={})
    assert scopes[-1].presence_roots() == tuple(sorted(map(str, roots)))
    paths = command_write_lease_paths(str(parent), command)
    assert str(target) in paths and str(child) in paths


def test_inherited_index_environment_is_explicit_scope_input(roots):
    parent, child = roots
    scope = git_command_scopes('git reset --hard', str(child),
                               environment={'GIT_INDEX_FILE': str(parent / '.git' / 'index')})[0]
    assert scope.presence_roots() == tuple(sorted(map(str, roots)))


@pytest.mark.parametrize('middle', [';', '||', '|', '&& python3 -c "print(1)" &&'])
def test_future_checkout_requires_proven_success_and_surviving_identity(roots, middle):
    parent, _ = roots
    future = parent / '.worktrees' / 'new'
    command = ('git worktree add -b new ' + shlex.quote(str(future)) + ' HEAD '
               + middle + ' git -C ' + shlex.quote(str(future)) + ' reset --hard')
    scope = git_command_scopes(command, str(parent), environment={})[-1]
    assert scope.presence_roots() == (str(parent),)
    assert scope.writer_paths() == (str(future),)


def test_literal_successful_new_checkout_does_not_claim_parent(roots):
    parent, _ = roots
    future = parent / '.worktrees' / 'new'
    command = ('git worktree add --detach ' + shlex.quote(str(future))
               + ' HEAD && git -C ' + shlex.quote(str(future)) + ' cherry-pick abc123')
    scope = git_command_scopes(command, str(parent), environment={})[-1]
    assert scope.presence_roots() == (str(future),)
    assert scope.writer_paths() == ()
    assert set(command_write_lease_paths(str(parent), command)) == {str(parent), str(future)}
    from lib.project_mod.run_command import command_write_lease_claims
    assert set(command_write_lease_claims(str(parent), command)) == {
        ('metadata', str(parent)), ('tree', str(future))}


def test_unresolved_later_target_does_not_erase_known_prior_writer(roots):
    parent, child = roots
    command = 'git -C ' + shlex.quote(str(child)) + ' add . && git -C "$UNKNOWN" add .'
    assert set(command_write_lease_paths(str(parent), command)) == set(map(str, roots))
    with pytest.raises(ValueError, match='expansion'):
        git_command_scopes(command, str(parent), environment={})


@pytest.mark.parametrize('setup', ['export GIT_DIR={metadata};', 'GIT_DIR={metadata};'])
def test_git_environment_setup_cannot_erase_index_owner(roots, setup):
    parent, child = roots
    command = setup.format(metadata=shlex.quote(str(parent / '.git'))) + ' git -C '
    command += shlex.quote(str(child)) + ' reset --hard'
    scope = git_command_scopes(command, str(parent), environment={})[-1]
    assert scope.presence_roots() == tuple(sorted(map(str, roots)))
    assert str(parent / '.git') in command_write_lease_paths(str(parent), command)


@pytest.mark.parametrize('command', [
    'git reset --soft HEAD', 'git reset --soft',
    'git restore --staged -- one.py', 'git restore -S --source=HEAD -- one.py',
    'git restore -S -s HEAD -- one.py',
])
def test_proven_metadata_only_modes_keep_destructive_risk(roots, command):
    from lib.project_mod.git_guard import classify
    parent, child = roots
    scope = git_command_scopes(command, str(child), environment={})[0]
    assert scope.writer_claims() == (('metadata', str(child)),)
    assert scope.presence_roots() == (str(child),)
    assert classify(command) == 'tree_destroying'


@pytest.mark.parametrize('command', [
    'git reset --hard HEAD', 'git reset --soft --hard HEAD',
    'git reset --soft HEAD -- one.py', 'git reset --soft --future HEAD',
    'git restore -SW -- one.py', 'git restore --staged --worktree -- one.py',
    'git restore --staged --patch -- one.py', 'git restore --staged --source -- one.py',
    'git restore --staged --future -- one.py',
])
def test_unknown_or_worktree_modes_never_shed_checkout_effects(roots, command):
    _parent, child = roots
    claims = git_command_scopes(command, str(child), environment={})[0].writer_claims()
    assert ('root', str(child)) in claims or ('tree', str(child)) in claims


@pytest.mark.parametrize('command', ['git reset --soft HEAD', 'git restore --staged -- one.py'])
def test_metadata_modes_with_executable_helpers_keep_tree_claims(roots, command):
    parent, child = roots
    hooks = parent / '.git' / 'hooks'
    hooks.mkdir()
    hook = hooks / 'reference-transaction'
    hook.write_text('#!/bin/sh\nexit 0\n')
    hook.chmod(0o700)
    claims = git_command_scopes(command, str(child), environment={})[0].writer_claims()
    assert ('tree', str(child)) in claims


@pytest.mark.parametrize('setup', ['GIT_COMMON_DIR={common} git', 'env GIT_COMMON_DIR={common} git',
                                   'export GIT_COMMON_DIR={common}; git'])
def test_common_dir_redirect_keeps_actual_metadata_owner(roots, setup):
    parent, child = roots
    command = setup.format(common=shlex.quote(str(parent / '.git'))) + ' -C ' + shlex.quote(str(child)) + ' branch -D topic'
    scope = git_command_scopes(command, str(child), environment={})[-1]
    assert scope.presence_roots() == tuple(sorted(map(str, roots)))
    assert ('tree', str(parent / '.git')) in scope.writer_claims()


@pytest.mark.parametrize('prefix', ['git', 'git -C nested -C ..'])
def test_relative_common_dir_resolves_against_effective_cwd(roots, prefix):
    parent, child = roots
    scope = git_command_scopes(prefix + ' branch -D topic', str(child),
                              environment={'GIT_COMMON_DIR': '../../.git'})[0]
    assert scope.presence_roots() == tuple(sorted(map(str, roots)))
    assert str(parent / '.git') in scope.paths
    assert str(parent / '.git' / '.git') not in scope.paths


@pytest.mark.parametrize('revision', ['HEAD~1', 'HEAD~2', 'topic~3'])
def test_git_revision_tilde_is_not_shell_path_expansion(roots, revision):
    _parent, child = roots
    scope = git_command_scopes('git reset --hard ' + revision, str(child), environment={})[0]
    assert scope.presence_roots() == (str(child),)


@pytest.mark.parametrize('source', ['git -C ~/repo reset --hard', 'GIT_DIR=~/git git reset --hard',
                                   'git -C "$OTHER" reset --hard'])
def test_real_expanded_targets_still_refuse_literal_resolution(roots, source):
    parent, _child = roots
    with pytest.raises(ValueError, match='expansion'):
        git_command_scopes(source, str(parent), environment={})


@pytest.mark.parametrize('command', ['git clean -ndx', 'git clean --dry-run -ff',
                                      'git clean -n -e --abort'])
def test_clean_preview_is_hardened_and_has_no_writer_claims(roots, command):
    from lib.project_mod.command_analysis import _lease_free_git_command
    from lib.project_mod.run_command import command_write_lease_claims
    _parent, child = roots
    hardened = _lease_free_git_command(command)
    assert hardened and '--no-pager' in hardened and 'core.fsmonitor=false' in hardened
    assert command_write_lease_claims(str(child), hardened) == ()


def test_clean_preview_does_not_hide_followup_writer_or_output(roots):
    from lib.project_mod.run_command import command_write_lease_claims
    _parent, child = roots
    assert ('root', str(child)) in command_write_lease_claims(str(child), 'git clean -nd && git reset --hard')
    claims = command_write_lease_claims(str(child), 'git clean -nd > preview.txt')
    assert ('file', str(child / 'preview.txt')) in claims


@pytest.mark.parametrize('option', ['--config-env core.hooksPath=HOOKDIR',
                                    '--config-env=core.hooksPath=HOOKDIR'])
@pytest.mark.parametrize('operation', ['reset --soft HEAD', 'restore --staged -- one.py',
                                       'branch -D topic', 'write-tree'])
def test_dynamic_helper_configuration_never_narrows_effects(roots, option, operation):
    _parent, child = roots
    command = 'git -c core.hooksPath=/dev/null ' + option + ' ' + operation
    scope = git_command_scopes(command, str(child), environment={'HOOKDIR': '/hooks'})[0]
    assert ('tree', str(child)) in scope.writer_claims()


@pytest.mark.parametrize('config', [
    '[core] hooksPath = /hooks\n',
    '[CORE] HOOKSPATH = /hooks\n',
    '[include] path = other.config\n',
    '[includeIf "gitdir:/other/"] path = other.config\n',
    '[filter "custom"] clean = helper\n',
    '[core]\n hooksPath\n',
    '[core]\n hooksPath = /ho\\\noks\n',
    '[core]\n' + '# padding\n' * 7000,
])
@pytest.mark.parametrize('operation', ['reset --soft HEAD', 'branch -D topic', 'write-tree'])
def test_bounded_configuration_detects_applicable_helpers(roots, config, operation):
    parent, child = roots
    (parent / '.git' / 'config').write_text(config)
    scope = git_command_scopes('git ' + operation, str(child), environment={})[0]
    if operation == 'branch -D topic' and config.startswith('[filter '):
        # Ref-only branch mutation does not invoke a content filter.
        assert scope.writer_claims() == (('metadata', str(child)),)
    else:
        assert ('tree', str(child)) in scope.writer_claims()


def test_ordinary_inline_configuration_does_not_force_tree_admission(roots):
    parent, child = roots
    (parent / '.git' / 'config').write_text(
        '[core] repositoryFormatVersion = 0\n bare = false\n'
        '[remote "origin"] url = /native/repo\n fetch = +refs/heads/*:refs/remotes/origin/*\n')
    scope = git_command_scopes('git reset --soft HEAD', str(child), environment={})[0]
    assert scope.writer_claims() == (('metadata', str(child)),)
