"""User Git probe stays available during writes; legacy preparation never repeats opaque code."""
import shlex

import pytest

from lib.project_mod import tools
from lib.project_mod.command_analysis import _lease_free_git_command
from tests.project_mod.test_command_outer_writer_lease import _other_writer, repo as _repo_fixture

repo = _repo_fixture
pytestmark = pytest.mark.unit

COUNTER = ('import sys; from collections import Counter; '
           'print("Main status counts:",dict(Counter(x[:2] for x in sys.stdin)))')
PROBE = (
    "/usr/bin/git branch --show-current; "
    "/usr/bin/git worktree list --porcelain; "
    "/usr/bin/git remote -v; "
    "/usr/bin/git for-each-ref --format='%(refname:short)|%(objectname:short)|%(committerdate:iso8601)|%(upstream:short)' refs/heads refs/remotes; "
    "/usr/bin/git status --porcelain | python3 -c " + shlex.quote(COUNTER)
)


def test_canonical_user_probe_is_complete_and_idempotent():
    canonical = _lease_free_git_command(PROBE)
    assert canonical is not None
    assert _lease_free_git_command(canonical) == canonical
    assert canonical.count('--no-optional-locks') == 5


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
def test_user_compound_probe_available_during_writer(repo, route):
    from lib.project_mod.run_command import tool_run_command

    with _other_writer(repo):
        result = (tool_run_command(str(repo), PROBE) if route == 'runner' else
                  tools.execute_tool('run_command', {'command': PROBE}, str(repo)))
    assert '[workspace-busy]' not in result
    assert '[exit code: 0]' in result
    assert "Main status counts: {'??': 2}" in result
    assert 'unknown option: --no-optional-locks' not in result
    # Legacy Git may lack branch --show-current; that is not a lease failure.


@pytest.mark.parametrize('suffix', [
    '; touch copied.txt', '; git reset --hard', '; git branch new-branch',
    '; git remote add origin example', ' > copied.txt',
])
def test_known_write_cannot_borrow_probe_exemption(repo, suffix):
    source = PROBE + suffix
    assert _lease_free_git_command(source) is None
    with _other_writer(repo):
        result = tools.execute_tool('run_command', {'command': source}, str(repo))
    assert '[workspace-busy]' in result
    assert not (repo / 'copied.txt').exists()


def test_compound_legacy_preparation_preserves_index_and_counter_once(repo, monkeypatch):
    import subprocess
    from lib.project_mod import git_inspection_execution as preparation
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: False)
    canonical = _lease_free_git_command(PROBE)
    with preparation.prepare_git_inspections(canonical, str(repo), environment={}) as command:
        assert '--no-optional-locks' not in command
        assert shlex.quote(COUNTER) in command
        result = subprocess.run(command, shell=True, cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.count('Main status counts:') == 1
    assert not (repo / '.git' / 'index').exists()


def test_legacy_multiple_checkouts_keep_distinct_private_indexes(repo, tmp_path, monkeypatch):
    import subprocess
    from lib.project_mod import git_inspection_execution as preparation
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: False)
    other = tmp_path / 'other'
    subprocess.run(['git', 'init', '-q', str(other)], check=True)
    canonical = _lease_free_git_command('git status; git -C ' + shlex.quote(str(other)) + ' status')
    with preparation.prepare_git_inspections(canonical, str(repo), environment={}) as command:
        indexes = [word.split('=', 1)[1] for word in shlex.split(command) if word.startswith('GIT_INDEX_FILE=')]
        assert len(set(indexes)) == 2
        assert subprocess.run(command, shell=True, cwd=repo, capture_output=True).returncode == 0
    assert not (repo / '.git' / 'index').exists()
    assert not (other / '.git' / 'index').exists()


def test_legacy_opaque_consumer_executes_once(repo, monkeypatch):
    import subprocess
    from lib.project_mod import git_inspection_execution as preparation
    monkeypatch.setattr(preparation, '_optional_locks_supported', lambda *args: False)
    consumer = 'from pathlib import Path; p=Path("consumer-count"); p.write_text(p.read_text()+"x" if p.exists() else "x")'
    canonical = _lease_free_git_command('git status | python3 -c ' + shlex.quote(consumer))
    with preparation.prepare_git_inspections(canonical, str(repo), environment={}) as command:
        assert subprocess.run(command, shell=True, cwd=repo, capture_output=True).returncode == 0
    assert (repo / 'consumer-count').read_text() == 'x'
