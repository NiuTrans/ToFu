"""Git coordination follows concrete checkout/index effects and shell control."""
import os
import shlex

import pytest

from lib.project_mod.git_command_scope import git_command_scopes

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
    scopes = git_command_scopes(command, str(parent), environment={})
    assert scopes[-1].presence_roots() == (str(child),)


@pytest.mark.parametrize('separator', [';', '||', '|'])
def test_failed_cd_or_pipeline_cannot_remove_original_checkout(roots, separator):
    parent, child = roots
    missing = child.parent / 'removed'
    for target in (child, missing):
        command = 'cd ' + shlex.quote(str(target)) + ' ' + separator + ' git reset --hard'
        expected = (str(parent),) if separator == '|' else tuple(sorted(map(str, roots)))
        scopes = git_command_scopes(command, str(parent), environment={})
        # Without the leading-cd proof both candidates remain.
        assert str(parent) in scopes[-1].presence_roots()
        if target == child:
            assert scopes[-1].presence_roots() == expected


@pytest.mark.parametrize('separator', [';', '||', '&&', '\n'])
@pytest.mark.parametrize('form', ['cd {child}', 'cd -P {child}', 'cd -- {child}',
                                  'command cd {child}', 'cd {child} 2>/dev/null'])
def test_certain_leading_cd_leaves_no_original_checkout(roots, separator, form):
    """A first-command cd into an existing searchable directory cannot fail.

    So no later segment can run where the command started, whatever separator
    follows: `cd wt; git reset --hard` resets only `wt`.
    """
    parent, child = roots
    command = form.format(child=shlex.quote(str(child))) + ' ' + separator + ' git reset --hard'
    scope = git_command_scopes(command, str(parent), environment={}, certain_leading_cd=True)[-1]
    assert scope.presence_roots() == (str(child),)


def test_reported_linked_worktree_rm_attributes_only_that_worktree(roots):
    """Refused live 2026-10-31 because the shared parent stayed a fallback cwd."""
    parent, child = roots
    command = ('cd ' + shlex.quote(str(child)) + ' && grep -n "export function disclosureChevron"'
               ' -A3 frontend/src/icons.ts; git rm -q frontend/src/tool-panel-header.ts;'
               ' git status --short | grep -v "^M " | head -20')
    scopes = git_command_scopes(command, str(parent), environment={}, certain_leading_cd=True)
    assert [(scope.subcommand, scope.presence_roots()) for scope in scopes] == [
        ('rm', (str(child),)), ('status', (str(child),))]


@pytest.mark.parametrize('shape', [
    'true; cd {child}; git reset --hard',
    'rm -rf {child}; cd {child}; git reset --hard',
    '( cd {child}; git reset --hard )',
    'cd {child} | true; git reset --hard',
    'cd {missing}; git reset --hard',
    'cd {plain_file}; git reset --hard',
    'cd {child}/../linked; git reset --hard',
    'builtin cd {child}; git reset --hard',
    'pushd {child}; git reset --hard',
    'cd -e {child}; git reset --hard',
    'cd -P -e {child}; git reset --hard',
    'cd -@ {child}; git reset --hard',
])
def test_cd_that_may_fail_keeps_original_checkout_under_certainty(roots, shape):
    """Only a leading literal cd that cannot fail drops the original cwd.

    Something ran before it, it runs in a subshell or a pipeline, its target is
    missing or not a directory, `..` resolves logically in the shell, or POSIX
    sh has no `builtin`/`pushd` and dash rejects bash's `cd -e`/`-@`: the reset
    may still run in the parent.
    """
    parent, child = roots
    plain_file = parent / 'plain-file'
    plain_file.write_text('not a directory\n')
    command = shape.format(child=shlex.quote(str(child)),
                           missing=shlex.quote(str(child.parent / 'removed')),
                           plain_file=shlex.quote(str(plain_file)))
    scope = git_command_scopes(command, str(parent), environment={}, certain_leading_cd=True)[-1]
    assert str(parent) in scope.presence_roots()


def test_unsearchable_leading_cd_target_is_not_entered_surely(roots):
    from lib.project_mod.git_command_scope import leading_cd_enters
    if os.geteuid() == 0:
        pytest.skip('root may search any directory')
    _parent, child = roots
    command = 'cd ' + shlex.quote(str(child)) + '; git reset --hard'
    assert leading_cd_enters(command, 0, str(child), {str(child)})
    child.chmod(0o600)
    try:
        assert not leading_cd_enters(command, 0, str(child), {str(child)})
    finally:
        child.chmod(0o755)


@pytest.mark.parametrize('shape', ['cd {child} & git reset --hard', 'cd {child} &\ngit reset --hard'])
def test_background_leading_cd_stays_unresolved_under_certainty(roots, shape):
    """`cd wt &` moves only a background subshell; the reset runs in the parent."""
    parent, child = roots
    with pytest.raises(ValueError, match='unresolved shell control'):
        git_command_scopes(shape.format(child=shlex.quote(str(child))), str(parent),
                           environment={}, certain_leading_cd=True)


@pytest.mark.parametrize('shape', [
    'echo start\ncd {child} && true\ngit reset --hard',
    'true || cd {child} && git reset --hard',
    '(cd {child} && true) && git reset --hard',
    '(cd {child} && true); git reset --hard',
    'echo $(cd {child} && pwd); git reset --hard',
    'cd {child} | (git reset --hard)',
])
def test_destructive_git_after_newline_subshell_or_skipped_cd_keeps_original_checkout(roots, shape):
    """Approval presence and writer admission both see the checkout the reset may run in."""
    parent, child = roots
    command = shape.format(child=shlex.quote(str(child)))
    assert str(parent) in git_command_scopes(command, str(parent), environment={})[-1].presence_roots()



@pytest.mark.parametrize('substitution', [
    'PRE=$(git rev-parse HEAD)', 'echo $(git rev-parse HEAD)', 'X=$(cd {child} && pwd)',
])
def test_command_substitution_resumes_the_parent_cwd(roots, substitution):
    """A `$(…)` body runs in a child shell; the parent continues where it was.

    Reported 2026-11-01: after `cd <linked> && git commit … && cd <shared>`, a
    `PRE=$(git rev-parse HEAD)` brought the linked cwd back, so the relative
    `git worktree remove` that followed was refused before anything ran.
    """
    parent, child = roots
    command = ('cd ' + shlex.quote(str(child)) + ' && git log -1 && cd ' + shlex.quote(str(parent))
               + ' && ' + substitution.format(child=shlex.quote(str(child)))
               + ' && git merge topic && git worktree remove .worktrees/linked')
    for options in ({}, {'certain_leading_cd': True}, {'strict': False}):
        removal = git_command_scopes(command, str(parent), environment={}, **options)[-1]
        assert removal.subcommand == 'worktree'
        assert removal.execution_cwds == (str(parent),)
        if options.get('strict', True):
            # Presence attribution is approval's; execution derives no tree effects.
            assert removal.presence_roots() == (str(child),)


@pytest.mark.parametrize('shape', [
    'cd {child} && X=$(git rev-parse HEAD) ; git reset --hard',
    'cd {child} && X=$(git rev-parse HEAD) || git reset --hard',
    'true && ( cd {child} && X=$(git rev-parse HEAD) ) && git reset --hard',
])
def test_substitution_resume_keeps_other_cwd_paths(roots, shape):
    """Resuming after `$(…)` keeps the parent's own failure paths and enclosing groups.

    A failed `cd` skips the substitution and `;`/`||` continue where the command
    started; an enclosing `( … )` is not a delimited substitution and still
    widens to every earlier cwd.
    """
    parent, child = roots
    command = shape.format(child=shlex.quote(str(child)))
    assert str(parent) in git_command_scopes(command, str(parent), environment={})[-1].presence_roots()

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


def test_literal_successful_new_checkout_does_not_claim_parent(roots):
    parent, _ = roots
    future = parent / '.worktrees' / 'new'
    command = ('git worktree add --detach ' + shlex.quote(str(future))
               + ' HEAD && git -C ' + shlex.quote(str(future)) + ' cherry-pick abc123')
    scope = git_command_scopes(command, str(parent), environment={})[-1]
    assert scope.presence_roots() == (str(future),)


def test_unresolved_later_target_does_not_erase_known_prior_writer(roots):
    parent, child = roots
    command = 'git -C ' + shlex.quote(str(child)) + ' add . && git -C "$UNKNOWN" add .'
    with pytest.raises(ValueError, match='expansion'):
        git_command_scopes(command, str(parent), environment={})


@pytest.mark.parametrize('setup', ['export GIT_DIR={metadata};', 'GIT_DIR={metadata};'])
def test_git_environment_setup_cannot_erase_index_owner(roots, setup):
    parent, child = roots
    command = setup.format(metadata=shlex.quote(str(parent / '.git'))) + ' git -C '
    command += shlex.quote(str(child)) + ' reset --hard'
    scope = git_command_scopes(command, str(parent), environment={})[-1]
    assert scope.presence_roots() == tuple(sorted(map(str, roots)))


@pytest.mark.parametrize('command', [
    'git reset --soft HEAD', 'git reset --soft',
    'git restore --staged -- one.py', 'git restore -S --source=HEAD -- one.py',
    'git restore -S -s HEAD -- one.py',
])
def test_proven_metadata_only_modes_keep_destructive_risk(roots, command):
    from lib.project_mod.git_guard import classify
    parent, child = roots
    scope = git_command_scopes(command, str(child), environment={})[0]
    assert scope.presence_roots() == (str(child),)
    assert classify(command) == 'tree_destroying'


@pytest.mark.parametrize('setup', ['GIT_COMMON_DIR={common} git', 'env GIT_COMMON_DIR={common} git',
                                   'export GIT_COMMON_DIR={common}; git'])
def test_common_dir_redirect_keeps_actual_metadata_owner(roots, setup):
    parent, child = roots
    command = setup.format(common=shlex.quote(str(parent / '.git'))) + ' -C ' + shlex.quote(str(child)) + ' branch -D topic'
    scope = git_command_scopes(command, str(child), environment={})[-1]
    assert scope.presence_roots() == tuple(sorted(map(str, roots)))


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
    _parent, child = roots
    hardened = _lease_free_git_command(command)
    assert hardened and '--no-pager' in hardened and 'core.fsmonitor=false' in hardened


_MESSAGE_BODY = ('subject\n\n'
                 'are rebuildable (git worktree remove without --force deletes ignored files,\n'
                 'rm -rf build && git reset --hard\n')


@pytest.mark.parametrize('header', [
    "git commit -q --allow-empty -F - <<'EOF'",
    'git commit -q --allow-empty --file=- <<"EOF"',
    "git tag -a v1 -F - <<'EOF'",
])
def test_quoted_heredoc_body_read_by_git_is_data_not_segments(roots, header):
    """Git reads a quoted heredoc as stdin bytes; its lines are never commands.

    Reported 2026-10-23: a commit-message line starting with `(git worktree
    remove` aborted the whole run_command with "Unresolved Git worktree
    lifecycle operands" before anything ran.
    """
    parent, _child = roots
    command = f'{header}\n{_MESSAGE_BODY}EOF'
    scopes = git_command_scopes(command, str(parent), environment={})
    assert [scope.subcommand for scope in scopes] == [header.split()[1]]


@pytest.mark.parametrize('command', [
    'git grep -n value *.py',
    'git grep -n $PATTERN -- lib',
    'git log -- $FILE',
    'git log -- "$(rm -rf build)"',
    'git -C "$OTHER" log -- one.py',
])
def test_approval_resolution_refuses_target_expansions(roots, command):
    _parent, child = roots
    with pytest.raises(ValueError, match='expansion'):
        git_command_scopes(command, str(child), environment={})
    assert git_command_scopes(command, str(child), environment={}, strict=False)


@pytest.mark.parametrize('command', [
    'git worktree remove -- {parent}/.worktrees/*',
    'WT={parent}/.worktrees/linked; git worktree remove --force "$WT"',
    'cd .worktrees || true; git worktree remove linked',
])
def test_worktree_lifecycle_unresolved_operands_refuse_approval_resolution_only(roots, command):
    # Approval keeps its existing gate; execution has no lease to resolve.
    parent, _child = roots
    command = command.format(parent=shlex.quote(str(parent)))
    with pytest.raises(ValueError, match='lifecycle|expansion'):
        git_command_scopes(command, str(parent), environment={})
    scopes = git_command_scopes(command, str(parent), environment={}, strict=False)
    assert [scope.presence_paths for scope in scopes] == [None]


@pytest.mark.parametrize('shape', [
    'if true; then cd {child}; fi; git reset --hard',
    '{{ cd {child}; }}; git reset --hard',
    'while true; do cd {child}; break; done; git reset --hard',
    'if cd {child}; then git reset --hard; fi',
    'true && {{ cd {child} && git reset --hard; }}',
])
def test_keyword_introduced_cd_and_git_reach_presence_and_writer_scopes(roots, shape):
    """A reserved word in front of `cd` or `git` does not hide the checkout it moves to.

    `if true; then cd wt; fi; git reset --hard` resets `wt` when the branch is
    taken, yet presence and writer admission only saw the invoking checkout.
    """
    parent, child = roots
    command = shape.format(child=shlex.quote(str(child)))
    scopes = git_command_scopes(command, str(parent), environment={})
    assert str(child) in scopes[-1].presence_roots()


@pytest.mark.parametrize('form', [
    'cd -P {child}', 'cd -L -- {child}', 'command cd {child}', 'builtin cd {child}',
    'pushd {child}', 'cd {child} 2>/dev/null', 'pushd {child} >/dev/null 2>&1',
])
def test_every_cd_form_moves_git_scope(roots, form):
    parent, child = roots
    command = form.format(child=shlex.quote(str(child))) + ' && git reset --hard'
    scopes = git_command_scopes(command, str(parent), environment={})
    assert scopes[-1].presence_roots() == (str(child),)


@pytest.mark.parametrize('shape', [
    'cd "$X" && git reset --hard',
    'cd ~ && git reset --hard',
    'cd - && git reset --hard',
    'popd; git reset --hard',
    'pushd "$X"; git reset --hard',
    'for i in 1 2; do cd nested; done; git reset --hard',
])
def test_unresolvable_cwd_change_before_destructive_git_fails_closed(roots, shape):
    """Approval falls back to the attended gate; writer admission refuses."""

    parent, _child = roots
    with pytest.raises(ValueError):
        git_command_scopes(shape, str(parent), environment={})
