"""Unit tests for the shared-checkout Git workspace guard classification.

Contract: docs/runbooks/GIT_WORKSPACE_GUARD.md.
``_git_guard_classify`` is a PURE function in
``lib/project_mod/command_analysis.py`` returning 'readonly' / 'safe_write' /
'tree_destroying' / None (→ caller keeps the existing attended gate).

The matrix pins three product decisions:

1. Safe writes (add/commit/branch/tag/fetch/push, branch creation) bypass
   attended confirmation with an automatic receipt.
2. Tree-destroying forms (checkout/switch/reset/restore/clean/stash
   mutations/rebase/integration recovery/pull/am/rm/mv, partial/conflicting
   apply modes) engage the
   presence-aware guard.
3. Unknown sub-commands, unknown global flags, and opaque non-Git segments
   fail CONSERVATIVE (None → human gate), never silent-allow.
"""

from __future__ import annotations

import shlex

import pytest

from lib.project_mod.command_analysis import (
    _git_guard_classify, git_integration_option_evidence, git_invocation_parts,
)

pytestmark = pytest.mark.unit


# ── No git → guard never engages ────────────────────────────────────

@pytest.mark.parametrize('command', [
    '',
    'ls -la',
    'rm -rf build/',
    'python -m pytest',
    'echo "git reset --hard"',          # git only inside a quoted string
    "grep -rn 'git checkout' lib/",
])
def test_no_git_segment_returns_none(command):
    assert _git_guard_classify(command) is None


# ── Read-only ───────────────────────────────────────────────────────

@pytest.mark.parametrize('command', [
    'git status',
    'git status --short',
    'git log --oneline -5',
    'git diff HEAD~1',
    'git show HEAD:lib/app.py',
    'git blame lib/app.py',
    'git rev-parse --show-toplevel',
    'git stash list',
    'git stash show -p stash@{0}',
    'git config --get user.name',
    'git status && git diff',                      # chains stay readonly
    'git log --oneline | head -5',                 # pipelines with readonly filters
    'git branch --show-current',
    'git branch --list feature-*',
    'git tag --list v*',
    '/usr/bin/git status',                         # absolute-path git seen through
    'git -C /repo status',                         # global value opt skipped
    'git -c color.ui=false status',
    'git --git-dir=/repo/.git log -1',
    'git --no-pager diff',
    'sudo git status',
    'git',                                         # bare git prints help
])
def test_readonly(command):
    assert _git_guard_classify(command) == 'readonly'


# ── Safe writes (receipted, no attended confirmation) ───────────────

@pytest.mark.parametrize('command', [
    'git add -A',
    'git add lib/app.py',
    'git commit -m "wip"',
    'git add -A && git commit -m "wip: preserve workspace"',
    'git branch feature-x',
    'git tag v1.2.3',
    'git fetch origin',
    'git push origin main',

    'git push -vu origin main',
    'git push -oforce origin main',                 # -o value is not -f
    'git fetch -otopic origin',                    # server option value
    'git fetch -j4 origin',
    'git branch -d old-feature',                   # self-guarding: Git
    'git branch --delete old-feature',             # refuses unmerged and
    'git branch -d a b c',                         # worktree-checked-out
    'git -C /repo branch -d x',                    # branches by itself
    'git checkout -b new-branch',
    'git switch -c new-branch',
    'git switch --create=new-branch',
    'git add x && git commit -m y && git push',    # all-safe chain
    'TOFU_GIT_ACK=abc123 git commit -m x',         # env prefix stripped
    'git -C /repo add -A',                         # global opt then safe write
])
def test_safe_write(command):
    assert _git_guard_classify(command) == 'safe_write'


# ── Tree-destroying (presence-aware guard engages) ──────────────────

@pytest.mark.parametrize('command', [

    'git checkout -B new-branch',
    'git checkout --orphan fresh',
    'git checkout --orphan=fresh',
    'git switch -C new-branch',
    'git branch -D old-branch',
    'git branch -df old-branch',                   # -D is exactly -df
    'git branch -fd old-branch',
    'git branch -vd old-branch',                   # bundled force flags
    'git branch -d -f old-branch',                 # separate force token
    'git branch --delete --force old-branch',
    'git branch --delete=old-branch',              # name-attached flag
    'git branch -d --list x',                      # mixed delete + listing
    'git branch -d -d x',                          # duplicated delete flag
    'git branch -d --delete x',
    'git branch -dm old new',                      # delete + rename mixed
    'git branch -d',                               # no branch named
    'git branch -M main',
    'git branch -m old new',
    'git tag -d v0.9',
    'git tag -df v0.9',                            # tag force unchanged
    'git commit --amend --no-edit',
    'git push --force-with-lease origin feature',

    'git push -vf origin feature',
    'git push -vd origin feature',
    'git push -uf origin feature',
    'git fetch -pf origin',
    'git fetch -p origin',
    'git fetch -P origin',
    'git fetch -u origin',
    'git fetch -vqu origin',
    'git reflog expire --expire=now --all',
    'git update-ref refs/heads/main HEAD~1',
    'git checkout main',
    'git checkout HEAD~2',
    'git checkout -- lib/app.py',                  # path checkout rewrites files
    'git checkout .',
    'git checkout -b new main~2',                  # start-point rewrites tree
    'git switch main',
    'git switch -c new main~2',                    # create + start-point
    'git switch --detach HEAD',
    'git reset --hard HEAD~1',
    'git reset --soft HEAD~1',                     # HEAD moves under siblings
    'git reset',
    'git restore .',
    'git restore --staged lib/app.py',
    'git clean -fd',
    'git rm -r --cached build/',
    'git mv old.py new.py',
    'git stash',                                   # bare stash == push
    'git stash push -m "wip"',
    'git stash pop',
    'git stash apply stash@{0}',
    'git stash drop',
    'git stash clear',
    'git stash branch rescue',
    'git rebase main',
    'git rebase --abort',                          # abort also rewrites the tree
    'git merge --abort',
    'git apply --3way patch.diff',
    'git apply --reject patch.diff',
    'git apply --unsafe-paths patch.diff',
    'git am < series.mbox',
    'git pull',
    'git pull --rebase',
    'git checkout main && git pull',               # chain with one destroyer
    'git stash show | git apply --reject',         # partial consumer
    'sudo git reset --hard',
    'git -C /repo checkout main',                  # presence uses resolved checkout
])
def test_tree_destroying(command):
    assert _git_guard_classify(command) == 'tree_destroying'


# ── Conservative fallbacks (None → existing attended gate) ──────────

@pytest.mark.parametrize('command', [
    'git bisect start',                            # unknown sub-command
    'git worktree add ../wt',                      # deliberately unclassified
    'git gc --aggressive',
    'git clone https://x/y.git',
    'git init',
    'git --unknown-global-flag status',            # unknown global flag shape
    'git add x && rm -rf build/',                  # opaque non-git segment
    'git commit -m x && python deploy.py',         # opaque interpreter segment
    'xargs git checkout',                          # wrapped invocation, unseen
])
def test_conservative_none(command):
    assert _git_guard_classify(command) is None


# ── Quoting cannot smuggle or falsify a verdict ─────────────────────

def test_quoted_literals_do_not_change_classification():
    # The destructive-looking text sits INSIDE the commit message: safe_write.
    assert _git_guard_classify(
        'git commit -m "document why we avoid reset --hard"') == 'safe_write'
    # Pipe inside quotes does not split the command.
    assert _git_guard_classify(
        'git log --format="%h | %s"') == 'readonly'
    # stash sub-command inside a quoted message is not parsed as a verb.
    assert _git_guard_classify(
        'git commit -m "mention stash pop"') == 'safe_write'


def test_env_assignment_prefix_does_not_hide_git():
    assert _git_guard_classify(
        'GIT_EDITOR=true git reset --hard') == 'tree_destroying'
    assert _git_guard_classify(
        'FOO=bar git add -A') == 'safe_write'


@pytest.mark.parametrize('command', [
    'git checkout -B existing', 'git checkout --orphan fresh',
    'git switch -C existing', 'git switch --force-create=existing',
    'git branch -D main', 'git branch -M main', 'git branch --list -D main',
    'git tag -f v1 HEAD', 'git tag -d v1', 'git commit --amend --no-edit',
    'git stash apply', 'git stash push', 'git stash drop',
    'git update-ref refs/heads/main HEAD~1',
    'git symbolic-ref HEAD refs/heads/other',
    'git config user.name writer', 'git config --global --add alias.x status',
    'git remote add origin /tmp/repo',
    'git reflog expire --expire=now --all',
    'git diff --output=report.txt', 'git stash show --output=report.txt',
    'git status > report.txt',

    'env git stash apply',
])
def test_writes_are_never_readonly_in_any_classifier(command):
    from lib.project_mod.command_analysis import (
        _extract_write_targets, _is_destructive_command,
    )
    assert _git_guard_classify(command) != 'readonly'
    assert _is_destructive_command(command)
    assert _extract_write_targets(command) != set()


def test_readonly_git_does_not_hide_another_segment_write():
    assert _git_guard_classify('git status && echo overwritten > file') is None


@pytest.mark.parametrize('arguments', [
    '--check', '--stat', '--numstat', '--summary',
    '--check --cached', '--check --index', '--stat --summary',
    '--check --exclude=frontend/generated.json',
    '--check --include "--reject"', '--check --directory src',
    '--check -p1 -C2 --whitespace=error', '--check -- --apply',
])
def test_apply_inspection_is_readonly(arguments):
    from lib.project_mod.command_analysis import _is_destructive_command
    command = f'/usr/bin/git apply {arguments} patch.diff'
    assert _git_guard_classify(command) == 'readonly'
    assert not _is_destructive_command(command)


@pytest.mark.parametrize('arguments', [
    '', '--cached', '--index', '--intent-to-add', '--reverse',
    '--exclude=a --exclude=b', '--include src --directory=src',
    '--whitespace=fix -p 1 -C 2', '--stat --apply',
    '--apply --check', '--numstat --apply', '--summary --apply',
    '-- --check', '--exclude --check',
])
def test_atomic_apply_is_ordinary_write(arguments):
    from lib.project_mod.command_analysis import (
        _extract_write_targets, _is_destructive_command,
    )
    command = f'git apply {arguments} patch.diff'
    assert _git_guard_classify(command) == 'safe_write'
    assert _is_destructive_command(command)
    assert _extract_write_targets(command) != set()


@pytest.mark.parametrize('arguments', [
    '--3way', '-3', '--reject', '--unsafe-paths', '--allow-overlap',
    '--check --reject', '--cached --3way', '--check --unsafe-paths',
])
def test_partial_or_conflicting_apply_keeps_presence_policy(arguments):
    assert _git_guard_classify(f'git apply {arguments} patch.diff') == 'tree_destroying'


@pytest.mark.parametrize('arguments', [
    '--future-option', '--chec', '--app', '--3', '-v3',
    '--build-fake-ancestor=index', '--check --build-fake-ancestor index',
    '--check --whitespace=unknown', '--check -p x', '--check -C',
])
def test_unknown_apply_options_cannot_borrow_readonly_or_write_receipt(arguments):
    assert _git_guard_classify(f'git apply {arguments}') is None


@pytest.mark.parametrize('opaque', [
    'npm run generate:i18n', 'python3 -m pytest', 'rm -rf build/',
    'git bisect start',
])
@pytest.mark.parametrize('git_command, verdict', [
    ('git reset --hard', 'tree_destroying'),
    ('git commit --amend --no-edit', 'tree_destroying'),
    ('git apply --reject patch.diff', 'tree_destroying'),
    ('git apply patch.diff', None),
    ('git commit -m append', None),
    ('git apply --check patch.diff', None),
])
def test_chain_policy_is_independent_of_opaque_segment_order(opaque, git_command, verdict):
    assert _git_guard_classify(f'{opaque} && {git_command}') == verdict
    assert _git_guard_classify(f'{git_command} && {opaque}') == verdict


def test_owned_patch_chain_uses_existing_gate_not_presence_veto():
    command = (
        '/usr/bin/git apply --exclude=frontend/src/i18n/contract.generated.ts '
        '--exclude=frontend/src/i18n/generated/en.generated.json '
        '--exclude=frontend/src/i18n/generated/zh.generated.json '
        'data/project-coordination-p2-owned.patch && '
        '/usr/bin/git apply --cached data/project-coordination-p2-owned.patch && '
        '/usr/bin/git diff --cached --check && '
        '/usr/bin/git commit -m "feat(swarm): integrate bounded child messaging and input receipts" && '
        'npm run generate:i18n && '
        'python3 scripts/gen_project_coordination_contract.py --check && '
        'PATH="$PWD/.native/git-bin:$PATH" python3 -m pytest -q tests/swarm/test_run_messages.py'
    )
    assert _git_guard_classify(command) is None


@pytest.mark.parametrize('command', [
    'git apply --stat "$(touch marker)"',
    'git apply --check `touch marker`',
    'git apply --check "$PATCH"',
    'git status "$(touch marker)"',
])
def test_dynamic_git_arguments_are_not_readonly_authority(command):
    from lib.project_mod.command_analysis import (
        _extract_write_targets, _is_destructive_command,
    )
    assert _git_guard_classify(command) is None
    assert _is_destructive_command(command)
    assert _extract_write_targets(command) != set()


@pytest.mark.parametrize('command', [
    'python deploy.py && git reset --hard',
    'git reset --hard && python deploy.py',
    'git bisect start && git reset --hard',
    'git reset --hard "$(touch marker)"',
])
def test_presence_risk_is_not_receipt_authority_for_opaque_segments(command):
    assert _git_guard_classify(command) == 'tree_destroying'
    assert _git_guard_classify(command, require_known_segments=True) is None


@pytest.mark.parametrize('command', [
    'git commit --amen --no-edit', 'git commit --amend',
    'git commit --am --no-edit',
])
def test_amend_abbreviations_cannot_borrow_append_only_receipt(command):
    assert _git_guard_classify(command) == 'tree_destroying'


@pytest.mark.parametrize('command', [
    'git commit -m --amend', 'git commit --message --amend',
    'git commit --message=--amend', 'git commit -- --amend',
    'git commit -F --amend', 'git commit -m "ordinary append"',
])
def test_commit_option_values_and_pathspecs_are_not_amend(command):
    assert _git_guard_classify(command) == 'safe_write'


@pytest.mark.parametrize('command', [
    'git apply --check patch.diff && echo "$(touch marker)"',
    'echo `touch marker` && git apply --check patch.diff',
    'git add file && echo "$(touch marker)"',
])
def test_dynamic_filter_segments_cannot_borrow_git_authority(command):
    from lib.project_mod.command_analysis import _is_destructive_command
    assert _git_guard_classify(command) is None
    assert _is_destructive_command(command)


@pytest.mark.parametrize('command', [
    'git apply --3way --build-fake-ancestor=outside.index --stat patch.diff',
    'git apply --reject --future-option patch.diff',
    'git apply --future-option --reject patch.diff',
    'git apply --unsafe-paths --output=outside.index patch.diff',
])
def test_unknown_apply_options_keep_risk_without_granting_receipt(command):
    assert _git_guard_classify(command) == 'tree_destroying'
    assert _git_guard_classify(command, require_known_segments=True) is None


@pytest.mark.parametrize('args, expected', [
    ('list', 'readonly'),
    ('list --porcelain', 'readonly'),
    ('add -b feature/new .worktrees/new HEAD', 'safe_write'),
    ('add --detach .worktrees/new HEAD', 'safe_write'),
    ('add -B existing .worktrees/new HEAD', None),
    ('add --force -b new .worktrees/new HEAD', None),
    ('add -b new .worktrees/new HEAD --force', None),
    # remove/move on a literal absolute path: Git itself refuses the main
    # worktree and a dirty/locked target, so this is tree_destroying (solo
    # admissible) rather than unknown. Relative/~ operands stay unknown — the
    # classifier cannot see the invoking checkout to resolve them, and the
    # lease layer enforces the same absolute-path requirement.
    ('remove /tmp/wt-a', 'tree_destroying'),
    ('remove --force /tmp/wt-a', 'tree_destroying'),
    ('remove /tmp/wt-a --force', 'tree_destroying'),
    ('remove -f /tmp/wt-a', 'tree_destroying'),
    ('move /tmp/wt-a /tmp/wt-b', 'tree_destroying'),
    ('move --force /tmp/wt-a /tmp/wt-b', 'tree_destroying'),
    ('remove --force .worktrees/new', None),   # relative source: unresolved root
    ('remove ../wt-rel', None),                 # relative source
    ('remove ~/wt', None),                      # tilde operand
    ('remove --orphan /tmp/wt-a', None),        # unknown option
    ('remove --reason x /tmp/wt-a', None),      # unknown option
    ('remove /tmp/wt-a /tmp/wt-b', None),       # remove takes one operand
    ('move /tmp/wt-a', None),                   # move takes two operands
    ('prune', None),
])
def test_worktree_parameter_sensitive_classification(args, expected):
    assert _git_guard_classify('/usr/bin/git worktree ' + args) == expected


def test_worktree_remove_chain_no_longer_unknown():
    # The repo's mandated isolated-worktree cleanup shape: remove the temp
    # worktree, force-delete its branch, list survivors. Previously
    # ``worktree remove`` was unclassified (None -> unknown), which forced the
    # whole chain to the human approval gate even though every other segment
    # is known. Now every git segment is classified, so the chain is a clean
    # tree_destroying verdict: solo-admissible, and eligible for the
    # merged-branch downgrade in git_guard.force_delete_targets_merged.
    command = ('cd /repo && git worktree remove --force /tmp/wt-x'
               ' && git branch -D fix/x && git worktree list | grep wt'
               ' || echo GONE')
    assert _git_guard_classify(command) == 'tree_destroying'
    # The compound with shell cd is not auto-receipt-eligible (cd is not a
    # provably read-only utility), but that is a receipt concern, not the
    # unknown-segment refusal that previously blocked it.
    assert _git_guard_classify(command, require_known_segments=True) is None


def test_worktree_remove_force_has_known_risk_but_still_requires_review():
    # Syntax classification does not grant forced cleanup authority. The
    # guard must retain review even when the entire chain is understood.
    command = ('git worktree remove --force /tmp/wt-x'
               ' && git branch -D fix/x && git worktree list | grep wt'
               ' || echo GONE')
    assert _git_guard_classify(command, require_known_segments=True) == 'tree_destroying'
    from lib.project_mod.git_guard import automatic_receipt_eligible
    assert automatic_receipt_eligible(command) is False


def test_worktree_add_and_cherry_pick_uses_ordinary_write_policy():
    command = ('git worktree add -b new .worktrees/new HEAD'
               ' && git -C .worktrees/new cherry-pick abc123')
    assert _git_guard_classify(command, require_known_segments=True) == 'safe_write'


@pytest.mark.parametrize('command', ['git merge feature-x', 'git cherry-pick abc123',
                                     'git revert HEAD', 'git cherry-pick --continue'])
def test_ordinary_integration_is_not_discard_policy(command):
    assert _git_guard_classify(command, require_known_segments=True) == 'safe_write'


@pytest.mark.parametrize('command', ['git merge --abort', 'git merge --autostash feature',
                                     'git cherry-pick --abort', 'git revert --abort',
                                     'git cherry-pick -X ours HEAD', 'git merge --strategy=ours feature'])
def test_forced_or_recovery_integration_keeps_discard_policy(command):
    assert _git_guard_classify(command) == 'tree_destroying'


@pytest.mark.parametrize('command', [
    'git merge --no-ff fix/workspace-message-receipts-20261010 -m "merge: concise agent-message surfaces and verified checkout contention docs"',
    'git merge --no-ff topic --message "ordinary merge"',
    'git merge topic --message=--abort',
    'git merge topic -m --autostash',
    'git merge topic -m"--strategy=ours"',
    'git merge --no-autostash --log=3 --stat topic -m message',
    'git merge topic -m first -m second',
    'git merge -- --abort',
    'git cherry-pick -s -x HEAD',
    'git cherry-pick -m 1 HEAD',
    'git cherry-pick -m2 HEAD',
    'git cherry-pick --mainline=1 HEAD',
    'git revert -m 1 HEAD',
    'git revert -s --no-edit HEAD',
])
def test_integration_option_values_and_verb_specific_flags(command):
    assert _git_guard_classify(command, require_known_segments=True) == 'safe_write'


@pytest.mark.parametrize('command', [
    'git merge -s ours topic',
    'git merge -sours topic',
    'git merge -m message --abort',
    'git merge --message=message --autostash topic',
    'git cherry-pick -m 1 --abort',
])
def test_integration_strategy_or_real_recovery_flags_require_presence_policy(command):
    assert _git_guard_classify(command) == 'tree_destroying'


@pytest.mark.parametrize('command', [
    'git merge --message',
    'git merge topic -m',
    'git merge --future-option topic',
    'git cherry-pick -m 0 HEAD',
    'git cherry-pick -m --abort HEAD',
    'git cherry-pick --mainline',
    'git cherry-pick --mainline=invalid HEAD',
    'git revert -m -1 HEAD',
    'git merge --log=unknown topic',
])
def test_integration_unknown_or_invalid_options_use_attended_fallback(command):
    # Intentional policy change: uncertainty is approval, not tree discard.
    assert _git_guard_classify(command) is None
    assert _git_guard_classify(command, require_known_segments=True) is None


def test_dynamic_merge_message_cannot_mint_ordinary_write_receipt():
    assert _git_guard_classify(
        'git merge topic -m "$(touch marker)"', require_known_segments=True) is None


@pytest.mark.parametrize('args, expected', [
    ([], ('', [])),
    (['-C/repo', '-ccolor.ui=false', 'merge', '--no-ff', 'topic'],
     ('merge', ['--no-ff', 'topic'])),
    (['-C', '', '-c', 'core.bare', 'status'], ('status', [])),
    (['--git-dir=/repo/.git', '--work-tree=/repo', '--namespace=ns',
      '--config-env=core.editor=EDITOR', 'status', '--short'], ('status', ['--short'])),
    (['--exec-path=/bin', '-P', 'status'], ('status', [])),
    (['--exec-path'], ('', [])),
    (['-C', '--abort', 'merge', '-m', '--abort', 'topic'],
     ('merge', ['-m', '--abort', 'topic'])),
])
def test_git_invocation_parts_consumes_only_validated_globals(args, expected):
    original = list(args)
    assert git_invocation_parts(args) == expected
    assert args == original
    # Syntax acceptance alone cannot authorize configured helpers.
    if expected[0] and any(x.startswith(('--config-env', '--exec-path'))
                           or x == 'core.bare' for x in args):
        assert _git_guard_classify('git ' + shlex.join(args)) is None


@pytest.mark.parametrize('args', [
    ['-C'], ['-c'], ['--git-dir'], ['--work-tree='], ['--namespace='],
    ['--unknown=anything', 'status'], ['--unknown', 'status'],
    ['-cnot-a-key', 'status'], ['-c', '=value', 'status'],
    ['--config-env=core.editor=', 'status'], ['--config-env=core.editor', 'status'],
    ['-C', '\x00', 'status'], [''],
])
def test_invalid_global_syntax_is_not_inferred_readonly(args):
    assert git_invocation_parts(args) is None


@pytest.mark.parametrize('command', [
    'git -C', 'git -c', 'git --git-dir', 'git --unknown=value status',
    'git --unknown=value merge topic', 'git --work-tree= status',
    'git -cnot-a-key status',
])
def test_invalid_globals_keep_attended_fallback(command):
    assert _git_guard_classify(command) is None
    assert _git_guard_classify(command, require_known_segments=True) is None


@pytest.mark.parametrize('verb', ['merge', 'cherry-pick', 'revert'])
@pytest.mark.parametrize('option', [
    '--cleanup strip', '--cleanup=whitespace', '--cleanup=verbatim',
    '--cleanup scissors', '--cleanup=default', '--rerere-autoupdate',
    '--no-rerere-autoupdate', '--commit', '--no-commit', '--no-gpg-sign',
])
def test_common_known_integration_option_modes(verb, option):
    assert _git_guard_classify(
        f'git {verb} {option} HEAD', require_known_segments=True) == 'safe_write'


@pytest.mark.parametrize('option', [
    '--ff', '--no-ff', '--empty=drop', '--empty=keep', '--empty=stop',
    '--empty drop', '--empty keep', '--empty stop', '--keep-redundant-commits',
])
def test_cherry_pick_known_empty_and_fast_forward_modes(option):
    assert _git_guard_classify(
        f'git cherry-pick {option} HEAD', require_known_segments=True) == 'safe_write'


@pytest.mark.parametrize('verb', ['merge', 'cherry-pick', 'revert'])
@pytest.mark.parametrize('option', [
    '--future-option', '--cleanup', '--cleanup=unknown', '--cleanup --abort',
    '--cleanup=', '--strategy', '--strategy=', '--strategy --abort',
    '--strategy-option', '--strategy-option=', '--rerere-autoupdate=true',
    '--no-gpg-sign=key', '--ff=true',
])
def test_unknown_missing_or_invalid_integration_option_is_approval(verb, option):
    assert _git_guard_classify(f'git {verb} HEAD {option}') is None


@pytest.mark.parametrize('command', [
    'git merge --empty=keep topic', 'git revert --empty=drop HEAD',
    'git revert --ff HEAD', 'git revert -x HEAD',
    'git cherry-pick --message=hello HEAD', 'git cherry-pick --empty=',
    'git cherry-pick --empty=unknown HEAD', 'git cherry-pick --empty --abort HEAD',
])
def test_integration_options_do_not_leak_across_verbs(command):
    assert _git_guard_classify(command) is None


@pytest.mark.parametrize('verb', ['merge', 'cherry-pick', 'revert'])
@pytest.mark.parametrize('option', ['--abort', '--quit', '--skip', '--autostash',
                                   '--strategy=ours', '--strategy-option=ours', '-Xours'])
@pytest.mark.parametrize('unknown', ['--future-option', '--cleanup=invalid', '--mainline=0'])
def test_real_integration_danger_outranks_unknown_but_cannot_mint_receipt(verb, option, unknown):
    for arguments in (f'{unknown} {option}', f'{option} {unknown}'):
        command = f'git {verb} {arguments} HEAD'
        assert _git_guard_classify(command) == 'tree_destroying'
        assert _git_guard_classify(command, require_known_segments=True) is None


@pytest.mark.parametrize('verb', ['merge', 'cherry-pick', 'revert'])
@pytest.mark.parametrize('option', ['-S', '-Skey', '--gpg-sign', '--gpg-sign=key'])
def test_signing_is_external_approval_not_tree_discard(verb, option):
    command = f'git {verb} {option} HEAD'
    assert _git_guard_classify(command) is None
    assert _git_guard_classify(command, require_known_segments=True) is None
    assert _git_guard_classify(command + ' --abort') == 'tree_destroying'
    assert _git_guard_classify(command + ' --abort', require_known_segments=True) is None


def test_integration_evidence_omits_consumed_values_and_separates_reasons():
    evidence = git_integration_option_evidence(
        'merge', ['-m', '--abort', '--cleanup=bad', '--future', '--strategy=ours', '-Ssecret'])
    assert evidence.risky_options == ('--strategy',)
    assert evidence.unknown_options == ('--cleanup', '--future')
    assert evidence.external_options == ('-S',)
    assert git_integration_option_evidence('merge', ['--', '--abort']).risky_options == ()


@pytest.mark.parametrize('arguments', [
    '-n', '--dry-run', '-nd', '-ndx', '-nff', '-ffn', '-qndX',
    '-n -e --abort', '-n --exclude=--interactive', '-ne--abort',
    '-n -- --interactive', '--dry-run --no-force',
    '--no-dry-run --dry-run', '-n --quiet -- path',
])
def test_clean_proven_preview_is_readonly(arguments):
    from lib.project_mod.command_analysis import _is_destructive_command
    command = f'git clean {arguments}'
    assert _git_guard_classify(command, require_known_segments=True) == 'readonly'
    assert not _is_destructive_command(command)


@pytest.mark.parametrize('arguments', [
    '-n -i', '-ni', '--interactive -n', '-n --no-interactive',
    '-n --future', '-nz', '-n -e', '-n --exclude', '-n --dry-run=true',
    '-n --force=yes', '-n --exclud=pattern',
])
def test_clean_interactive_unknown_or_missing_value_is_approval(arguments):
    assert _git_guard_classify(f'git clean {arguments}') is None
    assert _git_guard_classify(f'git clean {arguments}', require_known_segments=True) is None


@pytest.mark.parametrize('arguments', [
    '', '-fd', '-ffdx', '-e --dry-run', '-e--dry-run', '-- --dry-run',
    '--exclude=--dry-run', '-n --no-dry-run',
])
def test_actual_clean_and_option_looking_values_are_not_preview(arguments):
    assert _git_guard_classify(f'git clean {arguments}') == 'tree_destroying'


@pytest.mark.parametrize('command', [
    'git -C "$ROOT" merge topic', 'git merge --cleanup="$MODE" topic',
    'git cherry-pick --empty="$(touch marker)" HEAD', 'git clean -n "$PATHSPEC"',
    'git clean -n && echo "$(touch marker)"',
    'echo `touch marker` && git merge --cleanup=strip topic',
    'git clean -n > tracked-file',
])
def test_expanded_safe_syntax_never_authorizes_dynamic_or_output_writes(command):
    assert _git_guard_classify(command) is None
    assert _git_guard_classify(command, require_known_segments=True) is None


@pytest.mark.parametrize('prefix', [
    'git clean -n "$PATHSPEC"', 'git cherry-pick --empty="$MODE" HEAD',
    'git merge --cleanup=invalid topic',
])
def test_new_unknown_forms_preserve_chain_risk_and_strict_receipt(prefix):
    for command in (prefix + ' && git merge --abort', 'git merge --abort && ' + prefix):
        assert _git_guard_classify(command) == 'tree_destroying'
        assert _git_guard_classify(command, require_known_segments=True) is None
