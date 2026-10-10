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

import pytest

from lib.project_mod.command_analysis import _git_guard_classify

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
    ('remove --force .worktrees/new', None),
    ('prune', None),
])
def test_worktree_parameter_sensitive_classification(args, expected):
    assert _git_guard_classify('/usr/bin/git worktree ' + args) == expected


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
