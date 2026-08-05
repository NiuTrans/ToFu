#!/usr/bin/env python3
# Incident anchor: born in commit 4795d5ff — fix(export): force-push only on non-fast-forward rejection, not any e...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Guard: export.py force-pushes ONLY on a non-fast-forward REJECTION.

Background — the data-loss class:

  ``_git_push`` mirrors an export tree to a git remote. A freshly re-``git
  init``'d dest shares no ancestry with the mirror, so the remote legitimately
  REJECTS the push as non-fast-forward; a ``--force`` there is the intended,
  warned one-way-mirror behavior. The bug: the fallback forced on ANY
  ``RuntimeError`` — including auth failures, DNS/connection errors, permission
  denials and missing repos — where a force-push is useless at best and (if the
  transient error later clears) a remote-history clobber at worst.

  Fix: :func:`_is_nonff_push_rejection` classifies the git stderr; the fallback
  force-pushes only when it returns True and re-raises otherwise. This test
  pins that classifier — NOT the (owner-gated) question of whether divergence
  should force at all.

No git, no network, no DB — pure string classification.

Run:  pytest tests/test_export_push_force_guard.py -m unit
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit


# Real git stderr fragments for a non-fast-forward rejection.
_NONFF_CASES = [
    "! [rejected]        main -> main (non-fast-forward)\n"
    "error: failed to push some refs to 'github.com:o/r.git'\n"
    "hint: Updates were rejected because the tip of your current branch is behind",
    "! [rejected] main -> main (fetch first)\n"
    "error: failed to push some refs",
    "Updates were rejected because a pushed branch tip is behind its remote",
]

# Failures where forcing is useless-or-dangerous → must NOT force (re-raise).
_HARD_FAILURE_CASES = [
    "fatal: Authentication failed for 'https://github.com/o/r.git/'",
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "fatal: could not resolve host: github.com",
    "fatal: unable to access 'https://...': Connection refused",
    "remote: Permission to o/r.git denied to user.\nfatal: unable to access",
    "remote: Repository not found.\nfatal: repository 'https://...' not found",
    "",           # empty stderr
    "some totally unrelated error text",
]


def test_nonff_rejections_are_forceable():
    from export import _is_nonff_push_rejection
    for txt in _NONFF_CASES:
        assert _is_nonff_push_rejection(txt) is True, txt


def test_hard_failures_are_not_forceable():
    from export import _is_nonff_push_rejection
    for txt in _HARD_FAILURE_CASES:
        assert _is_nonff_push_rejection(txt) is False, txt


def test_auth_failure_wins_even_if_it_mentions_rejected():
    """A combined message that carries BOTH an auth failure and a reject
    marker must be treated as a hard failure (do not force)."""
    from export import _is_nonff_push_rejection
    mixed = ("! [rejected] main -> main (non-fast-forward)\n"
             "fatal: Authentication failed for 'https://github.com/o/r.git/'")
    assert _is_nonff_push_rejection(mixed) is False


def test_case_insensitive():
    from export import _is_nonff_push_rejection
    assert _is_nonff_push_rejection("NON-FAST-FORWARD") is True
    assert _is_nonff_push_rejection("AUTHENTICATION FAILED") is False


# ═══════════════════════════════════════════════════════════════════════
#  Owner-ratified policy (2026-07-25, pt_6598ae21 — "most long-term robust"):
#  divergence default = PRESERVE remote history (ours-merge → fast-forward),
#  force only via explicit --force; published tags never move silently.
# ═══════════════════════════════════════════════════════════════════════


def test_tag_push_action_pure():
    from export import _tag_push_action
    # Not on remote → plain push.
    assert _tag_push_action('', 'aaa111', force=False) == 'push'
    assert _tag_push_action('   \n', 'aaa111', force=True) == 'push'
    # Same commit on remote → nothing to do.
    assert _tag_push_action('aaa111\trefs/tags/v1.2.3', 'aaa111', force=False) == 'skip-same'
    # Different commit: force only with --force, otherwise keep the published tag.
    assert _tag_push_action('bbb222\trefs/tags/v1.2.3', 'aaa111', force=False) == 'keep'
    assert _tag_push_action('bbb222\trefs/tags/v1.2.3', 'aaa111', force=True) == 'force'


# ── Real-git end-to-end (skipped when git is unavailable) ─────────────

import shutil
import subprocess

_GIT = shutil.which('git')


def _git_run(cwd):
    def run(cmd):
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or 'git failed')
        return result.stdout.strip()
    return run


def _init_repo(path, branch='main'):
    path.mkdir(parents=True, exist_ok=True)
    r = _git_run(path)
    r(['git', 'init'])
    r(['git', 'checkout', '-b', branch])
    r(['git', 'config', 'user.email', 'test@example.com'])
    r(['git', 'config', 'user.name', 'test'])
    return r


def _commit_file(run, path, name, content, msg):
    (path / name).write_text(content, encoding='utf-8')
    run(['git', 'add', '-A'])
    run(['git', 'commit', '-m', msg])
    return run(['git', 'rev-parse', 'HEAD'])


def _remote_log(remote_git_dir, branch='main'):
    result = subprocess.run(
        ['git', '--git-dir', str(remote_git_dir), 'log', '--format=%H', branch],
        capture_output=True, text=True, timeout=60)
    return result.stdout.split()


def _remote_file(remote_git_dir, branch, name):
    result = subprocess.run(
        ['git', '--git-dir', str(remote_git_dir), 'show', f'{branch}:{name}'],
        capture_output=True, text=True, timeout=60)
    return result.stdout if result.returncode == 0 else None


def _make_diverged_remote(tmp_path):
    """Bare remote holding commit R1 (file remote.txt); an unrelated fresh
    export tree holding commit E1 (file export.txt). Returns (remote, exp, r1)."""
    remote = tmp_path / 'remote.git'
    remote.mkdir()
    subprocess.run(['git', 'init', '--bare', str(remote)],
                   capture_output=True, check=True, timeout=60)
    seed = _init_repo(tmp_path / 'seed')
    r1 = _commit_file(seed, tmp_path / 'seed', 'remote.txt', 'v1\n', 'R1')
    seed(['git', 'remote', 'add', 'origin', str(remote)])
    seed(['git', 'push', 'origin', 'main'])
    # Fresh export tree — UNRELATED history (the re-git-init'd mirror case).
    exp_dir = tmp_path / 'export'
    exp = _init_repo(exp_dir)
    _commit_file(exp, exp_dir, 'export.txt', 'snapshot\n', 'E1')
    exp(['git', 'remote', 'add', 'origin', str(remote)])
    return remote, exp_dir, r1


@pytest.mark.skipif(_GIT is None, reason='git not installed')
def test_push_branch_preserves_remote_history(tmp_path):
    """Default (no --force): a non-ff divergence must NOT clobber the remote.

    The export snapshot becomes the tip content, and the remote's own commit
    R1 stays reachable in the DAG (ours-merge → fast-forward). If the
    implementation ever regresses to force-push-by-default, R1 disappears and
    this test goes red."""
    from export import _push_branch
    remote, exp_dir, r1 = _make_diverged_remote(tmp_path)
    _push_branch(_git_run(exp_dir), 'origin', 'main', force=False)
    assert _remote_file(remote, 'main', 'export.txt') == 'snapshot\n'
    assert r1 in _remote_log(remote), 'remote history R1 lost — force happened?'


@pytest.mark.skipif(_GIT is None, reason='git not installed')
def test_push_branch_force_overwrites_only_when_explicit(tmp_path):
    """Explicit --force: the overwrite IS available for a deliberate reset —
    remote-only commits become unreachable from the branch tip."""
    from export import _push_branch
    remote, exp_dir, r1 = _make_diverged_remote(tmp_path)
    _push_branch(_git_run(exp_dir), 'origin', 'main', force=True)
    assert _remote_file(remote, 'main', 'export.txt') == 'snapshot\n'
    assert r1 not in _remote_log(remote), '--force should drop remote-only history'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
