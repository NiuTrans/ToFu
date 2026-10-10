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
    assert command_write_lease_paths(str(parent), command) == ()


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
