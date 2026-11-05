"""scripts/git_maintenance.py: explicit maintenance keeps every in-flight root.

Each test builds its own repository under tmp_path. Lease storage, including
the ancestor barrier namespace, is private to the test.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib.project_mod import workspace_lease as leases

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'git_maintenance.py'
OLD = 3 * 24 * 3600
IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def load_script():
    spec = importlib.util.spec_from_file_location('git_maintenance_script', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd, *args, stdin=None, **extra):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(IDENTITY, GIT_CONFIG_NOSYSTEM='1', **extra)
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, input=stdin, text=True,
                            capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def exists(repo, sha):
    return subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', sha],
                          capture_output=True, timeout=60).returncode == 0


def age(repo, *shas):
    """Make loose objects look written days ago, like abandoned in-flight work."""
    common = Path(repo, git(repo, 'rev-parse', '--git-common-dir'))
    for sha in shas:
        loose = common / 'objects' / sha[:2] / sha[2:]
        os.utime(loose, (loose.stat().st_atime - OLD, loose.stat().st_mtime - OLD))


def commit_objects(repo, revision):
    commit = git(repo, 'rev-parse', revision)
    return [commit, git(repo, 'rev-parse', commit + '^{tree}'),
            *[line.split()[2] for line in git(repo, 'ls-tree', '-r', commit).splitlines()]]


@pytest.fixture
def private_leases(tmp_path, monkeypatch):
    namespace = tmp_path / 'tree-leases'
    namespace.mkdir(mode=0o700)
    monkeypatch.setattr(leases, '_tree_lock_namespace', lambda: namespace)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    for name in [name for name in os.environ if name.startswith('GIT_')]:
        monkeypatch.delenv(name)
    return namespace


@pytest.fixture
def repository(tmp_path, private_leases):
    """Garbage next to every kind of root another session may hold.

    staged: blob only in the main index; detached: commit only in a linked
    worktree's HEAD; vanished: commit of a worktree whose directory is gone;
    reflog: commit only in a reflog whose entries are older than Git's expiry;
    recent: unreferenced but new. All referenced objects look three days old.
    """
    repo = tmp_path / 'repo'
    git(tmp_path, 'init', '-q', str(repo))
    (repo / 'tracked').write_text('base\n')
    git(repo, 'add', 'tracked')
    git(repo, 'commit', '-q', '-m', 'base')
    git(repo, 'commit-graph', 'write', '--reachable')
    git(repo, 'commit', '-q', '--allow-empty', '-m', 'after the commit-graph')
    roots = {}
    (repo / 'staged').write_text('staged only\n')
    git(repo, 'add', 'staged')
    roots['staged'] = [git(repo, 'rev-parse', ':staged')]
    for name in ('detached', 'vanished'):
        checkout = tmp_path / name
        git(repo, 'worktree', 'add', '-q', '--detach', str(checkout))
        (checkout / name).write_text(name + '\n')
        git(checkout, 'add', name)
        git(checkout, 'commit', '-q', '-m', name)
        roots[name] = commit_objects(checkout, 'HEAD')
    shutil.rmtree(tmp_path / 'vanished')
    tree = git(repo, 'mktree', stdin=f'100644 blob {git(repo, "hash-object", "-w", "--stdin", stdin="reflog only")}\treflog\n')
    reflog = git(repo, 'commit-tree', tree, '-m', 'reflog only')
    old_date = {'GIT_COMMITTER_DATE': '2000-01-01T00:00:00 +0000'}
    git(repo, 'update-ref', '-m', 'create', 'refs/heads/topic', reflog, **old_date)
    git(repo, 'update-ref', '-m', 'move', 'refs/heads/topic', 'HEAD', **old_date)
    roots['reflog'] = commit_objects(repo, reflog)
    garbage = git(repo, 'hash-object', '-w', '--stdin', stdin='abandoned garbage\n')
    recent = git(repo, 'hash-object', '-w', '--stdin', stdin='recent unreferenced\n')
    age(repo, garbage, *[sha for shas in roots.values() for sha in shas])
    return repo, roots, garbage, recent


def counts(repo):
    return dict(line.split(': ') for line in git(repo, 'count-objects', '-v').splitlines())


def test_apply_prunes_old_garbage_and_keeps_every_root(repository, capsys):
    repo, roots, garbage, recent = repository
    graph = repo / '.git' / 'objects' / 'info' / 'commit-graph'
    stale_graph = hashlib.sha256(graph.read_bytes()).hexdigest()

    assert load_script().main(['--repo', str(repo), '--apply']) == 0

    assert not exists(repo, garbage)
    assert exists(repo, recent)
    for name, shas in roots.items():
        assert all(exists(repo, sha) for sha in shas), name
    assert git(repo, 'fsck', '--no-dangling', '--no-progress') == ''
    worktrees = git(repo, 'worktree', 'list', '--porcelain')
    assert str(repo.parent / 'vanished') in worktrees and 'prunable' in worktrees
    assert len(git(repo, 'reflog', 'show', 'refs/heads/topic').splitlines()) == 2
    git(repo, 'commit-graph', 'verify')
    assert hashlib.sha256(graph.read_bytes()).hexdigest() != stale_graph
    after = counts(repo)
    assert (after['count'], after['packs']) == ('1', '1')
    output = capsys.readouterr().out
    for step in ('pack-refs', 'repack', 'prune', 'commit-graph', 'verify'):
        assert f'{step}: ok in ' in output


def test_report_only_by_default_and_expiry_below_one_day_is_refused(repository, capsys):
    repo, _roots, garbage, _recent = repository
    before = counts(repo)
    script = load_script()

    assert script.main(['--repo', str(repo)]) == 0
    output = capsys.readouterr().out
    assert 'would remove 1 loose objects' in output
    assert 'report only' in output
    with pytest.raises(SystemExit) as refused:
        script.main(['--repo', str(repo), '--apply', '--expire-days', '0'])
    assert refused.value.code == 2
    assert exists(repo, garbage)
    assert counts(repo) == before


def test_apply_waits_for_the_git_metadata_lease(repository, private_leases):
    repo, _roots, garbage, _recent = repository
    runner = ('import runpy, sys; from pathlib import Path; '
              'import lib.project_mod.workspace_lease as leases; '
              f'leases._tree_lock_namespace = lambda: Path({str(private_leases)!r}); '
              f'sys.argv = ["git_maintenance.py", "--repo", {str(repo)!r}, "--apply", '
              '"--lease-timeout", "0.2"]; '
              f'runpy.run_path({str(SCRIPT)!r}, run_name="__main__")')
    environment = dict(os.environ, PYTHONPATH=str(ROOT))

    with leases.workspace_git_metadata_lease(str(repo)):
        blocked = subprocess.run([sys.executable, '-c', runner], env=environment, text=True,
                                 capture_output=True, timeout=120, check=False)

    assert blocked.returncode == 3, blocked.stderr
    assert 'busy; nothing changed' in blocked.stderr
    assert exists(repo, garbage)


def test_shallow_repository_drops_the_unusable_stale_commit_graph(tmp_path, private_leases, capsys):
    """Git neither reads nor writes a commit-graph while shallow; a stale one only fails fsck."""
    repo = tmp_path / 'repo'
    git(tmp_path, 'init', '-q', str(repo))
    git(repo, 'config', 'core.logAllRefUpdates', 'false')  # Like the shared checkout.
    for name in ('first', 'second', 'third'):
        git(repo, 'commit', '-q', '--allow-empty', '-m', name)
    first, second = git(repo, 'rev-parse', 'HEAD~2'), git(repo, 'rev-parse', 'HEAD~1')
    git(repo, 'commit-graph', 'write', '--reachable')
    (repo / '.git' / 'shallow').write_text(second + '\n')
    (repo / '.git' / 'objects' / first[:2] / first[2:]).unlink()
    stale = subprocess.run(['git', '-C', str(repo), 'commit-graph', 'verify'],
                           capture_output=True, text=True, timeout=60)
    assert stale.returncode != 0 and first in stale.stderr
    script = load_script()

    assert script.main(['--repo', str(repo)]) == 0
    assert 'shallow repository' in capsys.readouterr().out
    assert script.main(['--repo', str(repo), '--apply']) == 0

    assert 'removed unused' in capsys.readouterr().out
    assert not (repo / '.git' / 'objects' / 'info' / 'commit-graph').exists()
    git(repo, 'commit-graph', 'verify')
    assert git(repo, 'fsck', '--no-dangling', '--no-progress') == ''
    assert git(repo, 'rev-list', '--count', 'HEAD') == '2'
