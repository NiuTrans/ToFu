"""Classification and Git read hardening found on live traffic (2026-10-23).

Model tool calls take no workspace lease since 2026-10-28
(docs/runbooks/GIT_WORKSPACE_GUARD.md); what remains of these specs pins the
substitution scanner, the hardening of Git reads inside substitutions, and the
additive classification of ``git worktree add -q/--lock`` that the Git guard
still uses.
"""

import pytest

from lib.project_mod.command_analysis import (
    _classify_git_invocation, command_substitution_bodies, harden_git_inspections,
)

pytestmark = pytest.mark.unit


def test_substitution_scanner_reports_untracked_constructs():
    assert command_substitution_bodies('echo "$(date)"', quoted_only=True) == [(6, 13, 'date')]
    assert command_substitution_bodies('echo $(date)', quoted_only=True) == []
    assert command_substitution_bodies('echo "$((1+2))"') == []
    assert command_substitution_bodies("echo '$(literal)'") == []
    for untracked in ('echo "$(case x in a) echo;; esac)"', 'echo "$(echo x # c)"',
                      'echo "$(unterminated"'):
        assert command_substitution_bodies(untracked) is None


def test_git_reads_inside_substitutions_are_hardened_for_execution():
    hardened = harden_git_inspections('echo "HEAD $(git rev-parse --short HEAD)"')
    assert '--no-optional-locks' in hardened and 'core.fsmonitor=false' in hardened
    heredoc = "cat <<'E'\n$(git log)\nE"
    assert harden_git_inspections(heredoc) == heredoc  # exact bytes kept


@pytest.mark.parametrize('args', [
    ['worktree', 'add', '-q', '-b', 'x', 'path', 'HEAD'],
    ['worktree', 'add', '-b', 'x', '-q', 'path'],
    ['worktree', 'add', '--detach', '--lock', 'path', 'HEAD'],
    # Git's option parser permutes: options may follow the path.
    ['worktree', 'add', '-b', 'x', 'path', '-q'],
    ['worktree', 'add', 'path', '-b', 'x', 'HEAD'],
])
def test_worktree_add_with_quiet_or_lock_flags_is_additive(args):
    assert _classify_git_invocation(args) == 'safe_write'


@pytest.mark.parametrize('args', [
    ['worktree', 'add', '-B', 'x', 'path'],            # resets an existing branch
    ['worktree', 'add', '-f', '-b', 'x', 'path'],      # force
    ['worktree', 'add', '-b', 'x', '--detach', 'path'],
    ['worktree', 'add', 'path'],                       # implicit branch
    ['worktree', 'add', 'path', '-b', 'x', '-f'],      # force after the path
])
def test_other_worktree_add_shapes_keep_the_human_gate(args):
    assert _classify_git_invocation(args) is None
