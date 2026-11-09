"""Integration radar: cross-checkout change awareness from Git ground truth.

Every test builds its own repository, linked worktrees and writer records
under tmp_path; nothing touches the checkout running the suite.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lib.project_mod import integration_radar as radar

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/shared.py', 'lib/other.py', 'lib/third.py', 'lib/new.py', 'lib/x.py'}


def git(cwd, *args) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    main = tmp_path / 'main'
    (main / 'lib').mkdir(parents=True)
    git(main, 'init', '-q', '-b', 'trunk')
    git(main, 'config', 'user.name', 'Writer')
    git(main, 'config', 'user.email', 'writer@example.invalid')
    (main / 'lib' / 'shared.py').write_text('VALUE = 1\n')
    (main / 'lib' / 'other.py').write_text('OTHER = 1\n')
    git(main, 'add', '-A')
    git(main, 'commit', '-qm', 'base')
    return main


def worktree(main: Path, name: str) -> Path:
    path = main.parent / name
    git(main, 'worktree', 'add', '-q', '-b', name, str(path))
    return path


def task(conversation: str, title: str = '') -> dict:
    return {'id': f'task-{conversation}', 'convId': conversation,
            'config': {'convTitle': title}}


def write(root: Path, relative: str, text: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return relative


def texts(advice) -> list[str]:
    return [text for _key, text in advice]


def records(main: Path) -> list[dict]:
    directory = main / '.git' / radar.RADAR_DIR / 'writers'
    return [json.loads(path.read_text()) for path in sorted(directory.glob('*.json'))]


def test_unlanded_work_in_another_worktree_is_named_both_ways(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    left, right = task('conv-a', 'Fix wakes'), task('conv-b', 'Queue view')

    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    assert radar.note_writes(left, str(left_root), ['lib/shared.py']) == []
    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    advice = texts(radar.note_writes(right, str(right_root), ['lib/shared.py']))

    assert len(advice) == 1
    assert '"Fix wakes" (conv-a) also has unlanded changes to lib/shared.py' in advice[0]
    assert 'branch feature-a' in advice[0]
    assert f'git -C {left_root.resolve()} diff trunk -- lib/shared.py' in advice[0]
    # The earlier writer learns about the later one at its next round boundary, once.
    notices = radar.take_notices(left, str(left_root))
    assert len(notices) == 1
    assert '"Queue view" (conv-b) started changing lib/shared.py' in notices[0]
    assert radar.take_notices(left, str(left_root)) == []
    radar.note_writes(right, str(right_root), ['lib/shared.py'])
    assert radar.take_notices(left, str(left_root)) == []


def test_reverted_work_stops_counting(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])
    write(left_root, 'lib/shared.py', 'VALUE = 1\n')

    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    assert radar.note_writes(task('conv-b'), str(right_root), ['lib/shared.py']) == []


def test_landed_work_becomes_a_trunk_change_since_the_branch_point(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])
    git(left_root, 'commit', '-qam', 'fix(shared): two')
    git(repo, 'merge', '-q', '--no-ff', '-m', 'land feature-a', 'feature-a')

    right = task('conv-b')
    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    advice = texts(radar.note_writes(right, str(right_root), ['lib/shared.py']))

    assert len(advice) == 1, advice
    assert 'trunk has landed changes to lib/shared.py since your branch point' in advice[0]
    assert 'fix(shared): two' in advice[0]
    assert 'Merge trunk into your branch before you verify' in advice[0]
    assert radar.note_writes(right, str(right_root), ['lib/shared.py']) == []


def test_untracked_new_files_count_as_unlanded(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/new.py', 'x = 1\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/new.py'])
    write(right_root, 'lib/new.py', 'x = 2\n')

    advice = texts(radar.note_writes(task('conv-b'), str(right_root), ['lib/new.py']))
    assert len(advice) == 1 and 'lib/new.py' in advice[0]


def test_two_writers_of_the_shared_checkout(repo):
    write(repo, 'lib/shared.py', 'VALUE = 5\n')
    radar.note_writes(task('conv-x'), str(repo), [str(repo / 'lib' / 'shared.py')])
    advice = texts(radar.note_writes(task('conv-y'), str(repo), ['lib/shared.py']))

    assert len(advice) == 1
    assert 'in the shared checkout' in advice[0]
    assert 'You both edit the same working tree' in advice[0]


def test_landing_notifies_writers_with_live_changes_and_retires_landed_records(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    third_root = worktree(repo, 'feature-c')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])
    write(right_root, 'lib/shared.py', 'VALUE = 3\n')
    radar.note_writes(task('conv-b'), str(right_root), ['lib/shared.py'])
    write(third_root, 'lib/other.py', 'OTHER = 3\n')
    radar.note_writes(task('conv-c'), str(third_root), ['lib/other.py'])
    radar.take_notices(task('conv-a'), str(left_root))

    git(left_root, 'commit', '-qam', 'fix(shared): two')
    git(repo, 'merge', '-q', '--no-ff', '-m', 'land feature-a', 'feature-a')
    commit = git(repo, 'rev-parse', 'HEAD').strip()
    notified = radar.notify_landing(repo, ['lib/shared.py'], commit=commit,
                                    subject='fix(shared): two', branch='feature-a',
                                    landed_root=left_root)

    assert [item['writer'] for item in notified] == ['conv-b']
    notices = radar.take_notices(task('conv-b'), str(right_root))
    assert len(notices) == 1
    assert 'feature-a landed on trunk' in notices[0] and 'lib/shared.py' in notices[0]
    assert 'Merge trunk into your branch before you verify or land' in notices[0]
    assert 'git_land runs no tests' in notices[0]
    assert radar.take_notices(task('conv-c'), str(third_root)) == []
    assert sorted(item['conversation'] for item in records(repo)) == ['conv-b', 'conv-c']


def test_shared_checkout_writer_is_told_its_edits_were_merged_on_top(repo):
    branch_root = worktree(repo, 'feature-a')
    write(repo, 'lib/other.py', 'OTHER = 9\n')
    radar.note_writes(task('conv-direct'), str(repo), ['lib/other.py'])
    write(branch_root, 'lib/other.py', 'OTHER = 1\nEXTRA = 2\n')
    git(branch_root, 'commit', '-qam', 'feat(other): extra')

    notified = radar.notify_landing(repo, ['lib/other.py'], commit='0' * 40,
                                    subject='feat(other): extra', branch='feature-a',
                                    landed_root=branch_root)
    assert [item['writer'] for item in notified] == ['conv-direct']
    notices = radar.take_notices(task('conv-direct'), str(repo))
    assert 'Your uncommitted edits in the shared checkout were merged on top' in notices[0]


def test_records_of_removed_checkouts_are_pruned(repo):
    left_root = worktree(repo, 'feature-a')
    write(left_root, 'lib/shared.py', 'VALUE = 2\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/shared.py'])
    git(repo, 'worktree', 'remove', '--force', str(left_root))

    write(repo, 'lib/shared.py', 'VALUE = 4\n')
    assert radar.note_writes(task('conv-x'), str(repo), ['lib/shared.py']) == []
    assert [item['conversation'] for item in records(repo)] == ['conv-x']


def test_a_new_task_keeps_only_unlanded_paths_of_earlier_tasks(repo):
    root = worktree(repo, 'feature-a')
    write(root, 'lib/shared.py', 'VALUE = 2\n')
    write(root, 'lib/other.py', 'OTHER = 2\n')
    radar.note_writes(task('conv-a'), str(root), ['lib/shared.py', 'lib/other.py'])
    write(root, 'lib/shared.py', 'VALUE = 1\n')

    write(root, 'lib/third.py', 'THIRD = 1\n')
    radar.note_writes(task('conv-a'), str(root), ['lib/third.py'])
    assert sorted(records(repo)[0]['paths']) == ['lib/other.py', 'lib/third.py']


def test_paths_are_literal_not_pathspec_patterns(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    write(left_root, 'lib/[x].py', 'x = 1\n')
    radar.note_writes(task('conv-a'), str(left_root), ['lib/[x].py'])
    write(left_root, 'lib/x.py', 'x = 1\n')
    write(right_root, 'lib/[x].py', 'x = 2\n')

    advice = texts(radar.note_writes(task('conv-b'), str(right_root), ['lib/[x].py']))
    assert len(advice) == 1 and 'lib/[x].py' in advice[0]


def test_generated_outputs_are_not_reported(repo):
    left_root, right_root = worktree(repo, 'feature-a'), worktree(repo, 'feature-b')
    bundle = 'frontend/src/runtime/app-runtime.js'
    write(left_root, bundle, 'left build\n')
    radar.note_writes(task('conv-a'), str(left_root), [bundle])
    write(right_root, bundle, 'right build\n')

    assert radar.note_writes(task('conv-b'), str(right_root), [bundle]) == []
    assert radar.notify_landing(repo, [bundle], commit='0' * 40, subject='build',
                                branch='feature-a') == []


def test_outside_git_or_without_a_conversation_is_silent(tmp_path, repo):
    plain = tmp_path / 'plain'
    plain.mkdir()
    write(plain, 'x.py', '1\n')
    assert radar.note_writes(task('conv-z'), str(plain), ['x.py']) == []
    assert radar.take_notices(task('conv-z'), str(plain)) == []
    assert radar.note_writes({'config': {}}, str(repo), ['lib/shared.py']) == []
    assert not (repo / '.git' / radar.RADAR_DIR).exists()
