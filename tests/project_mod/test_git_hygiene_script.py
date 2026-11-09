"""scripts/git_hygiene.py: read-only inventory, guarded apply and restore.

Every test builds its own repository under tmp_path; nothing touches the
checkout running the suite.
"""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'git_hygiene.py'
MARKER = '.tofu/file-history/README.txt'
_CLOCK = itertools.count(1_800_000_000, 60)


def git(cwd, *args):
    stamp = f'@{next(_CLOCK)} +0000'
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_AUTHOR_NAME='Hygiene', GIT_AUTHOR_EMAIL='hygiene@example.invalid',
               GIT_COMMITTER_NAME='Hygiene', GIT_COMMITTER_EMAIL='hygiene@example.invalid',
               GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def commit(repo, name, content):
    if isinstance(content, bytes):
        (repo / name).write_bytes(content)
    else:
        (repo / name).write_text(content)
    git(repo, 'add', name)
    git(repo, 'commit', '-q', '-m', name)
    return git(repo, 'rev-parse', 'HEAD')


def admin(worktree: Path) -> Path:
    return Path((worktree / '.git').read_text().split('gitdir: ', 1)[1].strip())


def make_idle(worktree: Path) -> None:
    old = time.time() - 3 * 24 * 3600
    for target in (worktree, admin(worktree) / 'HEAD', admin(worktree) / 'logs' / 'HEAD'):
        if target.exists():
            os.utime(target, (old, old))


def heads(repo):
    output = git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/')
    return dict(line.split(' ', 1) for line in output.splitlines())


def run(repo, *args):
    return subprocess.run([sys.executable, str(SCRIPT), '--repo', str(repo), *args],
                          capture_output=True, text=True, timeout=300)


@pytest.fixture
def scene(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    repo = repo.resolve()
    git(repo, 'init', '-q', '-b', 'main')
    lines = ''.join(f'line {number}\n' for number in range(1, 21))
    (repo / '.gitignore').write_text('.tofu*\n/data/\n__pycache__/\n')
    git(repo, 'add', '.gitignore')
    base = commit(repo, 'base.txt', lines)
    for name in ('merged-head', 'rebasing'):
        git(repo, 'branch', name)
    # A rewritten-away lineage, named by its backup branch (the anchor).
    git(repo, 'checkout', '-q', '--orphan', 'backup/pre-rewrite')
    commit(repo, 'old.txt', 'old 1\n')
    git(repo, 'branch', 'merged-also')
    old = commit(repo, 'old.txt', 'old 2\n')
    git(repo, 'checkout', '-q', 'main')
    commit(repo, 'base.txt', lines.replace('line 20\n', 'line 20 main\n'))
    git(repo, 'checkout', '-q', '-b', 'equivalent', base)
    commit(repo, 'base.txt', lines.replace('line 1\n', 'line 1 changed\n'))
    git(repo, 'checkout', '-q', '-b', 'unmerged', base)
    unmerged = commit(repo, 'base.txt', lines.replace('line 10\n', 'line 10 unmerged\n'))
    git(repo, 'checkout', '-q', 'main')
    # Same hunk on a different base blob: equal patch-id, different raw diff.
    git(repo, 'cherry-pick', 'equivalent')
    # git cherry compares binary files by blob id, not by "Binary files differ".
    commit(repo, 'blob.bin', b'\0version 1\n')
    git(repo, 'checkout', '-q', '-b', 'binary-diverged')
    commit(repo, 'blob.bin', b'\0version 3\n')
    git(repo, 'checkout', '-q', 'main')
    commit(repo, 'blob.bin', b'\0version 2\n')
    # A patch already in the tip's own history is no upstream twin.
    commit(repo, 'toggle.txt', 'off\n')
    commit(repo, 'toggle.txt', 'on\n')
    commit(repo, 'toggle.txt', 'off\n')
    git(repo, 'checkout', '-q', '-b', 'reapplied')
    commit(repo, 'toggle.txt', 'on\n')
    git(repo, 'checkout', '-q', 'main')

    worktrees = tmp_path / 'worktrees'
    busy, clean, dirty, durable = (worktrees / name for name in
                                   ('busy', 'clean', 'dirty', 'durable'))
    git(repo, 'worktree', 'add', '-q', '-b', 'checked-out', str(busy), base)
    for path in (clean, dirty, durable):
        git(repo, 'worktree', 'add', '-q', '--detach', str(path), base)
    (clean / '.tofu' / 'file-history' / 'backups').mkdir(parents=True)
    (clean / MARKER).write_text('bootstrap marker\n')
    (clean / '__pycache__').mkdir()
    (clean / '__pycache__' / 'cache.pyc').write_bytes(b'\0')
    (dirty / 'notes.txt').write_text('uncaptured\n')
    (admin(dirty) / 'rebase-merge').mkdir()
    (admin(dirty) / 'rebase-merge' / 'head-name').write_text('refs/heads/rebasing\n')
    (durable / '.tofu' / 'file-history').mkdir(parents=True)
    (durable / '.tofu' / 'file-history' / 'snapshots.jsonl').write_text('{"id": "undo"}\n')
    for path in (clean, dirty, durable):
        make_idle(path)
    # Integration status scans refresh idle clones' indexes; that is not activity.
    os.utime(admin(clean) / 'index')

    key = hashlib.sha256(str(repo).encode('utf-8')).hexdigest()[:12]
    pool = repo / 'data' / 'integration' / 'worktrees' / key / 'u1'
    junk, history = pool / 'reclaimed-clone', pool / 'history-clone'
    (junk / '.tofu' / 'file-history' / 'backups').mkdir(parents=True)
    (junk / MARKER).write_text('bootstrap marker\n')
    (history / '.tofu' / 'file-history').mkdir(parents=True)
    (history / '.tofu' / 'file-history' / 'snapshots.jsonl').write_text('{"id": "undo"}\n')

    git(repo, 'update-ref', 'refs/tofu/checkpoints/u1/first', base)
    git(repo, 'update-ref', 'refs/tofu/checkpoints/u1/second', 'main')
    git(repo, 'update-ref', 'refs/tofu/inputs/u1/third', base)
    return {'repo': repo, 'base': base, 'old': old, 'unmerged': unmerged, 'busy': busy,
            'clean': clean, 'dirty': dirty, 'durable': durable, 'junk': junk,
            'history': history}


def observable_state(scene):
    repo = scene['repo']
    indexes = [repo / '.git' / 'index'] + [admin(scene[name]) / 'index'
                                           for name in ('busy', 'clean', 'dirty', 'durable')]
    return {
        'refs': git(repo, 'for-each-ref', '--format=%(refname) %(objectname)'),
        'worktrees': git(repo, 'worktree', 'list', '--porcelain'),
        'indexes': [path.read_bytes() for path in indexes],
        'pool': sorted(str(path.relative_to(repo)) for path in (repo / 'data').rglob('*')),
    }


def test_dry_run_classifies_every_branch_and_changes_nothing(scene):
    repo = scene['repo']
    before = observable_state(scene)

    result = run(repo, '--also-merged-into', scene['old'], '--remove-clean-worktrees', '--json')

    assert result.returncode == 0, result.stdout + result.stderr
    assert observable_state(scene) == before
    assert not (repo / 'data' / 'git-hygiene').exists()
    report = json.loads(result.stdout)
    branches = {item['name']: item for item in report['branches']['items']}
    assert {name: item['class'] for name, item in branches.items()} == {
        'main': 'merged_head', 'merged-head': 'merged_head', 'rebasing': 'merged_head',
        'checked-out': 'merged_head', 'merged-also': 'merged_also',
        'backup/pre-rewrite': 'merged_also', 'equivalent': 'patch_equivalent',
        'unmerged': 'unmerged', 'binary-diverged': 'unmerged', 'reapplied': 'unmerged'}
    assert {name: item['protection'] for name, item in branches.items() if item['protection']} == {
        'main': 'current', 'checked-out': 'checked_out', 'rebasing': 'checked_out',
        'backup/pre-rewrite': 'anchor'}
    assert sorted(item['ref'] for item in report['plan']['branches']) == [
        'refs/heads/merged-also', 'refs/heads/merged-head']
    # The bulk classification agrees with git cherry for every outcome.
    assert '+' not in git(repo, 'cherry', 'main', 'equivalent')
    for name in ('unmerged', 'binary-diverged', 'reapplied'):
        assert git(repo, 'cherry', 'main', name).startswith('+'), name

    worktrees = {Path(item['path']).name: item for item in report['worktrees']['items']}
    assert worktrees['dirty']['state'] == 'dirty'
    assert worktrees['durable']['ignoredBlockers'] == ['.tofu/']
    assert worktrees['busy']['idle'] is False
    assert all(item['headReachable'] for item in worktrees.values())
    assert [Path(item['path']).name for item in report['plan']['worktrees']] == ['clean']

    pool = {Path(item['path']).name: item for item in report['poolDirectories']['items']}
    assert pool['reclaimed-clone']['rebuildable'] is True
    assert pool['history-clone']['rebuildable'] is False
    assert [Path(item['path']).name for item in report['plan']['poolDirectories']] == [
        'reclaimed-clone']
    tofu = report['tofuRefs']
    assert {name: entry['count'] for name, entry in tofu.items()} == {
        'refs/tofu/checkpoints': 2, 'refs/tofu/inputs': 1}
    assert tofu['refs/tofu/checkpoints']['oldest'] < tofu['refs/tofu/checkpoints']['newest']


def test_apply_deletes_only_merged_branches_and_rebuildable_pool_directories(scene):
    repo = scene['repo']
    branches_before = heads(repo)
    tofu_before = git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/tofu/')
    worktrees_before = git(repo, 'worktree', 'list', '--porcelain')

    result = run(repo, '--also-merged-into', scene['old'], '--apply', '--json')

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    branches_after = heads(repo)
    assert set(branches_before) - set(branches_after) == {
        'refs/heads/merged-head', 'refs/heads/merged-also'}
    assert all(branches_before[ref] == sha for ref, sha in branches_after.items())
    assert git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/tofu/') == tofu_before
    assert git(repo, 'worktree', 'list', '--porcelain') == worktrees_before
    assert (scene['dirty'] / 'notes.txt').is_file()
    assert not scene['junk'].exists()
    assert (scene['history'] / '.tofu' / 'file-history' / 'snapshots.jsonl').is_file()

    manifest = Path(report['applied']['manifest'])
    assert manifest.parent == repo / 'data' / 'git-hygiene'
    assert f'--restore {manifest}' in result.stderr
    recorded = json.loads(manifest.read_text())
    assert {item['ref']: item['sha'] for item in recorded['branches']} == {
        ref: branches_before[ref] for ref in ('refs/heads/merged-head', 'refs/heads/merged-also')}

    restored = subprocess.run(report['applied']['restoreCommand'], shell=True,
                              capture_output=True, text=True, timeout=300)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert heads(repo) == branches_before
    assert (scene['junk'] / MARKER).read_text() == 'bootstrap marker\n'
    assert (scene['junk'] / '.tofu' / 'file-history' / 'backups').is_dir()
    # Restore never overwrites: a second run reports every branch as existing.
    again = run(repo, '--restore', str(manifest))
    assert again.returncode == 1
    assert heads(repo) == branches_before


def test_remove_clean_worktrees_keeps_dirty_recent_and_durable_worktrees(scene):
    repo = scene['repo']

    result = run(repo, '--apply', '--remove-clean-worktrees', '--json')

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['applied']['worktrees'] == {'removed': 1, 'failed': []}
    assert not scene['clean'].exists()
    assert (scene['dirty'] / 'notes.txt').read_text() == 'uncaptured\n'
    assert (scene['durable'] / '.tofu' / 'file-history' / 'snapshots.jsonl').is_file()
    assert scene['busy'].is_dir()
    assert heads(repo)['refs/heads/checked-out'] == scene['base']

    restored = run(repo, '--restore', report['applied']['manifest'])
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert git(scene['clean'], 'rev-parse', 'HEAD') == scene['base']


def test_apply_keeps_a_branch_that_moved_after_inventory(scene):
    spec = importlib.util.spec_from_file_location('git_hygiene_under_test', SCRIPT)
    hygiene = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hygiene)
    repo = scene['repo']
    report = hygiene.inventory(str(repo), [], None, False)
    assert [item['ref'] for item in report['plan']['branches']] == ['refs/heads/merged-head']
    moved_to = git(repo, 'rev-parse', 'main~1')
    git(repo, 'update-ref', 'refs/heads/merged-head', moved_to)

    applied = hygiene.apply(report, report['alsoMergedInto'])

    assert applied['branches']['deleted'] == 0
    assert [item['ref'] for item in applied['branches']['failed']] == ['refs/heads/merged-head']
    assert heads(repo)['refs/heads/merged-head'] == moved_to


def test_unreachable_also_merged_revision_is_refused(scene):
    repo = scene['repo']
    tree = git(repo, 'rev-parse', 'main^{tree}')
    dangling = git(repo, 'commit-tree', tree, '-p', scene['old'], '-m', 'dangling')
    before = heads(repo)

    result = run(repo, '--also-merged-into', dangling, '--apply')

    assert result.returncode == 2
    assert 'not reachable' in json.loads(result.stdout)['error']
    assert heads(repo) == before
    assert not (repo / 'data' / 'git-hygiene').exists()


def test_pool_directories_under_local_placement_roots_are_inventoried(scene, tmp_path, monkeypatch):
    from serverctl_pkg.local_placement import local_workspace_roots
    repo = scene['repo']
    monkeypatch.setenv('TOFU_LOCAL_DIR', str(tmp_path / 'local-state'))
    [placed] = local_workspace_roots(repo / 'data')
    key = hashlib.sha256(str(repo).encode('utf-8')).hexdigest()[:12]
    local_clone = placed / key / 'u1' / 'local-clone'
    (local_clone / '.tofu' / 'file-history').mkdir(parents=True)
    (local_clone / MARKER).write_text('bootstrap marker\n')

    result = run(repo, '--json')

    assert result.returncode == 0, result.stdout + result.stderr
    pool = json.loads(result.stdout)['poolDirectories']
    assert pool['roots'] == [str(repo / 'data' / 'integration' / 'worktrees' / key),
                             str(placed / key)]
    assert {Path(item['path']).name: item['rebuildable'] for item in pool['items']} == {
        'reclaimed-clone': True, 'history-clone': False, 'local-clone': True}
