"""Lanes: a branch plus a warm checkout per unit of work, recycled on landing.

Every test builds its own repository under tmp_path. ``TOFU_LOCAL_DIR`` points
at a temporary directory, which placement never selects, so lanes fall back
to ``<repo>/.tofu/worktrees`` and nothing touches the developer's home.
"""
from __future__ import annotations

import fcntl
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.project_mod import lanes

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _load_git_land():
    spec = importlib.util.spec_from_file_location('git_land_for_lanes', ROOT / 'scripts' / 'git_land.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


git_land = _load_git_land()


def git(cwd, *args) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv('TOFU_LOCAL_DIR', str(tmp_path / 'local'))
    main = tmp_path / 'main'
    main.mkdir()
    git(main, 'init', '-q', '-b', 'trunk')
    git(main, 'config', 'user.name', 'Lane')
    git(main, 'config', 'user.email', 'lane@example.invalid')
    (main / '.gitignore').write_text('.tofu/\nnode_modules\n.venv\n')
    (main / 'app.py').write_text('VALUE = 1\n')
    git(main, 'add', '-A')
    git(main, 'commit', '-qm', 'base')
    (main / 'node_modules').mkdir()
    (main / '.venv').mkdir()
    return main


def commit(checkout: Path, message: str) -> str:
    git(checkout, 'add', '-A')
    git(checkout, 'commit', '-qm', message)
    return git(checkout, 'rev-parse', 'HEAD').strip()


def test_a_new_lane_is_a_branch_with_its_own_checkout_and_shared_dependencies(repo):
    path, recycled = lanes.create_lane(repo, 'fix/value')

    assert not recycled
    assert path.parent == repo / '.tofu' / 'worktrees'
    assert git(path, 'symbolic-ref', '--short', 'HEAD').strip() == 'fix/value'
    assert git(path, 'rev-parse', 'HEAD') == git(repo, 'rev-parse', 'trunk')
    assert (path / 'node_modules').resolve() == (repo / 'node_modules').resolve()
    assert (path / '.venv').is_symlink()
    assert git(path, 'status', '--porcelain') == ''
    assert lanes.is_lane(lanes.repository(repo), path)
    assert not lanes.is_lane(lanes.repository(repo), repo)


def test_a_dependency_the_repository_does_not_ignore_as_a_file_is_not_linked(repo):
    (repo / '.gitignore').write_text('.tofu/\nnode_modules/\n.venv\n')
    commit(repo, 'only directories named node_modules are ignored')

    path, _ = lanes.create_lane(repo, 'fix/links')

    assert not os.path.lexists(path / 'node_modules')
    assert (path / '.venv').is_symlink()
    assert git(path, 'status', '--porcelain', '--untracked-files=normal') == ''


def test_landing_recycles_the_lane_and_the_next_lane_starts_from_it(repo):
    first, _ = lanes.create_lane(repo, 'feat/one')
    (first / 'app.py').write_text('VALUE = 2\n')
    commit(first, 'feat: two')

    result = git_land.land('feat/one', into=str(repo))

    assert result['status'] == 'landed'
    assert any(note.startswith('recycled lane') for note in result['cleanup'])
    assert 'deleted branch feat/one' in result['cleanup']
    assert not first.exists()
    assert lanes.spare_count(lanes.repository(repo)) == 1
    second, recycled = lanes.create_lane(repo, 'feat/two')
    assert recycled
    assert lanes.spare_count(lanes.repository(repo)) == 0
    assert git(second, 'rev-parse', 'HEAD') == git(repo, 'rev-parse', 'trunk')
    assert (second / 'app.py').read_text() == 'VALUE = 2\n'
    assert git(second, 'status', '--porcelain') == ''
    assert [item.branch for item in lanes.checkouts(lanes.repository(repo))
            if item.path == second] == ['feat/two']


def test_a_lane_with_uncommitted_work_is_kept_on_landing(repo):
    path, _ = lanes.create_lane(repo, 'feat/dirty')
    (path / 'app.py').write_text('VALUE = 3\n')
    commit(path, 'feat: three')
    (path / 'notes.txt').write_text('still working\n')

    result = git_land.land('feat/dirty', into=str(repo))

    assert result['status'] == 'landed'
    assert result['cleanup'][0].startswith('kept lane')
    assert (path / 'notes.txt').exists()
    assert git(repo, 'rev-parse', '--verify', '--quiet', 'refs/heads/feat/dirty')


def test_saved_work_reconstructs_a_lost_lane_checkout(repo):
    path, _ = lanes.create_lane(repo, 'feat/wip')
    (path / 'app.py').write_text('VALUE = 4\n')
    tip = commit(path, 'feat: four')
    (path / 'app.py').write_text('VALUE = 5\n')
    (path / 'new.py').write_text('NEW = True\n')

    saved = lanes.save_wip(path)

    assert saved and git(repo, 'rev-parse', 'refs/tofu/lanes/feat/wip').strip() == saved
    assert git(repo, 'rev-parse', 'refs/heads/feat/wip').strip() == tip
    assert git(path, 'status', '--porcelain', '--untracked-files=normal').splitlines() == [
        ' M app.py', '?? new.py']
    shutil.rmtree(path)
    git(repo, 'worktree', 'prune')

    restored, outcome = lanes.restore_lane(repo, 'feat/wip')

    assert outcome == 'restored with its saved uncommitted work'
    assert git(restored, 'rev-parse', 'HEAD').strip() == tip
    assert (restored / 'app.py').read_text() == 'VALUE = 5\n'
    assert (restored / 'new.py').read_text() == 'NEW = True\n'
    assert git(restored, 'diff', '--cached', '--name-only') == ''


def test_a_clean_lane_saves_nothing_and_drops_an_older_save(repo):
    path, _ = lanes.create_lane(repo, 'feat/clean')
    (path / 'app.py').write_text('VALUE = 6\n')
    assert lanes.save_wip(path)
    commit(path, 'feat: six')

    assert lanes.save_wip(path) is None
    listed = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet',
                             'refs/tofu/lanes/feat/clean'], capture_output=True, text=True)
    assert listed.returncode != 0


def test_an_existing_branch_is_refused_with_the_restore_command(repo):
    lanes.create_lane(repo, 'feat/again')

    with pytest.raises(lanes.LaneError, match='lane.py restore feat/again'):
        lanes.create_lane(repo, 'feat/again')


def test_a_spare_whose_registration_vanished_is_replaced_by_a_fresh_checkout(repo):
    path, _ = lanes.create_lane(repo, 'feat/old')
    git_land.land('feat/old', into=str(repo))
    spare = next((repo / '.tofu' / 'worktrees').glob('.spare-*'))
    admin = Path((spare / '.git').read_text().split(':', 1)[1].strip())
    shutil.rmtree(admin)

    fresh, recycled = lanes.create_lane(repo, 'feat/new')

    assert not recycled
    assert git(fresh, 'symbolic-ref', '--short', 'HEAD').strip() == 'feat/new'
    assert not path.exists()


def test_settlement_saves_only_the_lanes_a_task_wrote_in(repo):
    path, _ = lanes.create_lane(repo, 'feat/task')
    (path / 'app.py').write_text('VALUE = 7\n')
    (repo / 'app.py').write_text('VALUE = 8\n')
    task = {'_radarCheckouts': [str(repo), str(path)]}

    saved = lanes.save_task_lanes(task)

    assert [item['checkout'] for item in saved] == [str(path)]
    assert saved[0]['wip'] == git(repo, 'rev-parse', 'refs/tofu/lanes/feat/task').strip()


def _wait_for_prepared_spare(repo: Path) -> bool:
    root = repo / '.tofu' / 'worktrees'
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if lanes.spare_count(lanes.repository(repo)) and not lanes._preparation_in_progress(root):
            return True
        time.sleep(0.2)
    return False


def test_the_next_lane_recycles_a_checkout_prepared_in_advance(repo):
    assert lanes.prepare_spare(repo) == 'ready'
    assert lanes.prepare_spare(repo) == 'present'

    path, recycled = lanes.create_lane(repo, 'feat/prepared')

    assert recycled
    assert git(path, 'symbolic-ref', '--short', 'HEAD').strip() == 'feat/prepared'
    assert git(path, 'status', '--porcelain', '--untracked-files=normal') == ''
    assert not list((repo / '.tofu' / 'worktrees').glob('.preparing-*'))


def test_an_abandoned_preparation_is_cleared_and_a_live_one_is_not_duplicated(repo):
    root = repo / '.tofu' / 'worktrees'
    root.mkdir(parents=True, exist_ok=True)
    abandoned = root / '.preparing-dead'
    git(repo, 'worktree', 'add', '-q', '--detach', str(abandoned), 'HEAD')
    (root / '.preparing-dead.lock').write_text('')

    assert lanes.prepare_spare(repo) == 'ready'

    assert not abandoned.exists() and not (root / '.preparing-dead.lock').exists()
    assert str(abandoned) not in git(repo, 'worktree', 'list', '--porcelain')
    git(repo, 'worktree', 'remove', '--force', str(next(root.glob('.spare-*'))))
    with open(root / '.preparing-live.lock', 'w') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert lanes.ensure_next_spare(repo) is False, 'a live preparation is not duplicated'
    (root / '.preparing-live.lock').unlink()


def test_the_command_line_creates_lists_and_restores_lanes(repo):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    script = [sys.executable, str(ROOT / 'scripts' / 'lane.py')]

    created = subprocess.run([*script, 'new', 'feat/cli'], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=120)
    assert _wait_for_prepared_spare(repo), (
        repo / '.tofu' / 'worktrees' / '.preparation.log').read_text()
    listed = subprocess.run([*script, 'list'], cwd=repo, env=env,
                            capture_output=True, text=True, timeout=120)
    again = subprocess.run([*script, 'new', 'feat/cli'], cwd=repo, env=env,
                           capture_output=True, text=True, timeout=120)

    assert created.returncode == 0, created.stdout + created.stderr
    assert created.stdout.startswith('Lane feat/cli ready in ')
    assert 'git_land.py feat/cli' in created.stdout
    assert 'feat/cli: ' in listed.stdout and '1 lanes; 1 recycled checkouts' in listed.stdout
    assert again.returncode == 1 and again.stdout.startswith('Refused: branch feat/cli already exists')
