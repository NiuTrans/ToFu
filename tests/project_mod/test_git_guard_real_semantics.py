"""Real Git checks for expanded guard syntax, only in pytest-owned repositories."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

import pytest

from lib.project_mod.command_analysis import _git_guard_classify, git_invocation_parts

pytestmark = pytest.mark.unit


@pytest.fixture
def git_repo(tmp_path):
    binary = shutil.which('git')
    if binary is None:
        pytest.skip('Git executable unavailable')
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repo'
    repo.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_')}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), GIT_CONFIG_NOSYSTEM='1',
               GIT_TERMINAL_PROMPT='0', GIT_EDITOR='true', GIT_MERGE_AUTOEDIT='no',
               GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args, check=True):
        result = subprocess.run(
            [binary, *args], cwd=repo, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10, check=False)
        if check:
            assert result.returncode == 0, result.stderr
        return result

    git('init', '-b', 'main')
    git('config', 'user.name', 'Git guard test')
    git('config', 'user.email', 'git-guard@example.invalid')
    git('config', 'commit.gpgSign', 'false')
    git('config', 'core.hooksPath', str(home / 'no-hooks'))
    (repo / 'tracked').write_text('base\n')
    (repo / 'unrelated').write_text('base\n')
    git('add', '--', 'tracked', 'unrelated')
    git('commit', '-m', 'base')
    git('switch', '-c', 'topic')
    (repo / 'new-file').write_text('topic\n')
    git('add', '--', 'new-file')
    git('commit', '-m', 'topic')
    git('switch', 'main')
    return repo, git


@pytest.mark.parametrize('arguments', [
    ['-n'], ['--dry-run'], ['-nd'], ['-ndx'], ['-nff'], ['-ffn'],
    ['-qndX'], ['-n', '-e', '--abort'], ['-ne--abort'],
    ['-n', '--exclude=--interactive'], ['-n', '--', '--interactive'],
    ['--no-dry-run', '--dry-run'],
])
def test_real_clean_preview_preserves_untracked_files_and_git_state(git_repo, arguments):
    repo, git = git_repo
    (repo / 'untracked').write_bytes(b'keep untracked\n')
    (repo / '--interactive').write_bytes(b'keep option-looking path\n')
    (repo / 'scratch').mkdir()
    (repo / 'scratch' / 'data').write_bytes(b'keep directory\n')
    (repo / 'ignored').write_bytes(b'keep ignored\n')
    (repo / '.gitignore').write_text('ignored\n')
    before = {path.relative_to(repo): path.read_bytes()
              for path in repo.rglob('*') if path.is_file() and '.git' not in path.parts}
    head = git('rev-parse', 'HEAD').stdout
    index = (repo / '.git' / 'index').read_bytes()
    command = shlex.join(['git', 'clean', *arguments])
    assert _git_guard_classify(command, require_known_segments=True) == 'readonly'
    git('clean', *arguments)
    after = {path.relative_to(repo): path.read_bytes()
             for path in repo.rglob('*') if path.is_file() and '.git' not in path.parts}
    assert after == before
    assert git('rev-parse', 'HEAD').stdout == head
    assert (repo / '.git' / 'index').read_bytes() == index


@pytest.mark.parametrize('verb, arguments', [
    ('merge', ['--no-ff', '-m', '--abort', '--cleanup=verbatim']),
    ('merge', ['--no-ff', '--message=--abort', '--cleanup', 'strip']),
    ('merge', ['--rerere-autoupdate', '--no-autostash', '--log=2']),
    ('cherry-pick', ['--ff']),
    ('cherry-pick', ['--empty=drop', '--cleanup=strip']),
    ('cherry-pick', ['--empty=keep', '--no-rerere-autoupdate']),
    ('cherry-pick', ['--empty', 'stop', '--cleanup=scissors']),
    ('revert', ['--no-edit', '--cleanup=whitespace', '--rerere-autoupdate']),
    ('revert', ['--no-edit', '--cleanup=default', '-s']),
])
def test_real_ordinary_integration_preserves_unrelated_dirty_and_untracked(git_repo, verb, arguments):
    repo, git = git_repo
    if verb == 'revert':
        git('cherry-pick', 'topic')
    (repo / 'unrelated').write_bytes(b'local dirty work\n')
    (repo / 'untracked').write_bytes(b'untracked work\n')
    command = shlex.join(['git', verb, *arguments, 'topic'])
    assert _git_guard_classify(command, require_known_segments=True) == 'safe_write'
    git(verb, *arguments, 'topic')
    assert (repo / 'unrelated').read_bytes() == b'local dirty work\n'
    assert (repo / 'untracked').read_bytes() == b'untracked work\n'
    if verb == 'revert':
        assert not (repo / 'new-file').exists()
    else:
        assert (repo / 'new-file').read_bytes() == b'topic\n'


def test_real_attached_global_options_reach_same_invocation(git_repo):
    repo, git = git_repo
    args = [f'-C{repo}', '-ccolor.ui=false', '--git-dir=' + str(repo / '.git'),
            '--work-tree=' + str(repo), 'clean', '-ndx']
    assert git_invocation_parts(args) == ('clean', ['-ndx'])
    assert _git_guard_classify(shlex.join(['git', *args])) == 'readonly'
    (repo / 'untracked').write_bytes(b'keep\n')
    # The requested guard grammar accepts attached global values, but Git
    # 2.56 itself rejects -C/path (and -ck=v). Classification does not promise
    # Git execution success. Check the installed behavior, then its portable
    # separated equivalent against real Git.
    result = git(*args, check=False)
    if result.returncode:
        assert 'unknown option' in result.stderr
    separated = ['-C', str(repo), '-c', 'color.ui=false', *args[2:]]
    assert git_invocation_parts(separated) == ('clean', ['-ndx'])
    git(*separated)
    assert (repo / 'untracked').read_bytes() == b'keep\n'


@pytest.mark.parametrize('verb, arguments', [
    ('merge', ['--cleanup=invalid', 'topic']),
    ('cherry-pick', ['--empty=invalid', 'topic']),
    ('cherry-pick', ['--mainline=0', 'topic']),
    ('revert', ['--mainline=invalid', 'topic']),
])
def test_real_invalid_options_fail_without_borrowing_write_receipt(git_repo, verb, arguments):
    repo, git = git_repo
    head = git('rev-parse', 'HEAD').stdout
    before = (repo / 'tracked').read_bytes()
    assert _git_guard_classify(shlex.join(['git', verb, *arguments])) is None
    assert git(verb, *arguments, check=False).returncode != 0
    assert git('rev-parse', 'HEAD').stdout == head
    assert (repo / 'tracked').read_bytes() == before


@pytest.mark.parametrize('operation', ['soft-reset', 'staged-restore'])
def test_real_metadata_mutation_keeps_dirty_worktree_bytes(git_repo, operation):
    repo, git = git_repo
    git('cherry-pick', 'topic')
    (repo / 'tracked').write_bytes(b'private working tree edits\n')
    git('add', '--', 'tracked')
    (repo / 'tracked').write_bytes(b'later working edits, not staged\n')
    before = (repo / 'tracked').read_bytes()
    if operation == 'soft-reset':
        index = (repo / '.git' / 'index').read_bytes()
        git('reset', '--soft', 'HEAD~1')
        assert (repo / '.git' / 'index').read_bytes() == index
    else:
        git('restore', '--staged', '--', 'tracked')
    assert (repo / 'tracked').read_bytes() == before


def test_real_reviewed_path_commit_preserves_unrelated_staged_blob(git_repo):
    repo, git = git_repo
    (repo / 'unrelated').write_bytes(b'another task staged work\n')
    git('add', '--', 'unrelated')
    before = git('ls-files', '--stage', '--', 'unrelated').stdout
    (repo / 'reviewed').write_bytes(b'this task reviewed work\n')
    # An explicit path-only commit, not add -A or a whole-index commit.
    git('add', '--intent-to-add', '--', 'reviewed')
    git('commit', '--only', '-m', 'reviewed task', '--', 'reviewed')
    assert git('ls-files', '--stage', '--', 'unrelated').stdout == before
    assert git('show', 'HEAD:unrelated').stdout == 'base\n'
    assert git('show', ':unrelated').stdout == 'another task staged work\n'
    assert git('show', 'HEAD:reviewed').stdout == 'this task reviewed work\n'


def test_hardened_real_clean_preview_keeps_index_and_worktree(git_repo):
    from lib.project_mod.command_analysis import _lease_free_git_segment
    repo, git = git_repo
    (repo / 'untracked').write_bytes(b'keep\n')
    before = (repo / '.git' / 'index').read_bytes()
    prepared = _lease_free_git_segment('git clean -ndx')
    assert prepared is not None
    git(*shlex.split(prepared)[1:])
    assert (repo / '.git' / 'index').read_bytes() == before
    assert (repo / 'untracked').read_bytes() == b'keep\n'
