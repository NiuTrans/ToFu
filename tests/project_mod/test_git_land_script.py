"""scripts/git_land.py: land worktree branches on a checkout with concurrent edits.

Every test builds its own repositories under tmp_path; nothing touches the
checkout running the suite. The shared checkout plays the role of the
canonical project folder that other writers edit without committing.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'git_land.py'
LINES = ''.join(f'line {number}\n' for number in range(1, 21))


def _load():
    spec = importlib.util.spec_from_file_location('git_land_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


land = _load()


def git(cwd, *args) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def rev(cwd, revision='HEAD') -> str:
    return git(cwd, 'rev-parse', revision).strip()


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    repo = tmp_path / 'shared'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'Lander')
    git(repo, 'config', 'user.email', 'lander@example.invalid')
    (repo / 'notes.txt').write_text(LINES)
    (repo / 'old.txt').write_text('retire me\n')
    (repo / 'tool.sh').write_text('#!/bin/sh\necho base\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'base')
    return repo


@pytest.fixture()
def feature(shared: Path, tmp_path: Path) -> Path:
    path = tmp_path / 'feature-worktree'
    git(shared, 'worktree', 'add', '-q', '-b', 'feature', str(path))
    return path


def commit_all(worktree: Path, message: str) -> str:
    git(worktree, 'add', '-A')
    git(worktree, 'commit', '-qm', message)
    return rev(worktree)


def replace_line(path: Path, number: int, text: str) -> None:
    path.write_text(path.read_text().replace(f'line {number}\n', f'{text}\n'))


def state(repo: Path) -> tuple:
    """Everything a refused landing must leave byte-identical."""
    files = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob('*')
             if path.is_file() and '.git' not in path.relative_to(repo).parts}
    return (git(repo, 'for-each-ref'), rev(repo), (repo / '.git' / 'index').read_bytes(),
            files, git(repo, 'worktree', 'list', '--porcelain'))


def _snapshot_branch(shared: Path, tmp_path: Path, name: str) -> tuple[str, str]:
    input_worktree = tmp_path / f'{name}-input'
    git(shared, 'worktree', 'add', '-q', '--detach', str(input_worktree))
    (input_worktree / 'foreign.txt').write_text('another writer\n')
    git(input_worktree, 'add', 'foreign.txt')
    tree = git(input_worktree, 'write-tree').strip()
    snapshot = git(input_worktree, 'commit-tree', tree, '-p', rev(shared),
                   '-m', 'Tofu checkpoint: example input').strip()
    git(shared, 'branch', name, snapshot)
    return snapshot, str(tmp_path / f'{name}-worktree')


def test_task_input_snapshot_refuses_before_landing(shared, tmp_path):
    snapshot, worktree = _snapshot_branch(shared, tmp_path, 'snapshot-based')
    git(shared, 'update-ref', 'refs/tofu/inputs/u1/example', snapshot)
    git(shared, 'worktree', 'add', '-q', '-b', 'snapshot-based-work', worktree,
        'snapshot-based')
    (Path(worktree) / 'own.txt').write_text('my work\n')
    commit_all(Path(worktree), 'own work')
    before = state(shared)

    with pytest.raises(land.Refused, match='task-input snapshot') as refused:
        land.land('snapshot-based-work', cwd=shared)

    assert snapshot in refused.value.reason
    assert any('foreign.txt' in path for path in refused.value.paths)
    assert 'fresh worktree' in refused.value.advice
    assert str(shared) in refused.value.advice and 'refs/heads/main' in refused.value.advice
    assert 'cherry-pick only your own commits' in refused.value.advice
    assert state(shared) == before
    assert Path(worktree).exists()


def test_snapshot_identification_uses_the_input_ref_not_commit_metadata(shared, tmp_path):
    _snapshot, worktree = _snapshot_branch(shared, tmp_path, 'lookalike')
    git(shared, 'worktree', 'add', '-q', '-b', 'lookalike-work', worktree, 'lookalike')
    (Path(worktree) / 'own.txt').write_text('my work\n')
    commit_all(Path(worktree), 'own work')

    result = land.land('lookalike-work', cwd=shared)

    assert result['status'] == 'landed'
    assert (shared / 'foreign.txt').read_text() == 'another writer\n'
    assert (shared / 'own.txt').read_text() == 'my work\n'


def test_concurrent_uncommitted_edit_is_carried_forward(shared, feature):
    replace_line(feature / 'notes.txt', 2, 'line 2 from the branch')
    (feature / 'added.txt').write_text('new\n')
    (feature / 'old.txt').unlink()
    tip = commit_all(feature, 'feature work')
    before = rev(shared)
    replace_line(shared / 'notes.txt', 19, 'line 19 being edited elsewhere')

    result = land.land('feature', cwd=shared)

    assert result['status'] == 'landed' and result['preIntegration'] == before
    assert rev(shared, 'HEAD^1') == before and rev(shared, 'HEAD^2') == tip
    assert git(shared, 'log', '-1', '--format=%s').strip() == "Merge branch 'feature' into main"
    committed = git(shared, 'show', 'HEAD:notes.txt')
    assert 'line 2 from the branch' in committed and 'line 19 being edited' not in committed
    working = (shared / 'notes.txt').read_text()
    assert 'line 2 from the branch' in working and 'line 19 being edited elsewhere' in working
    assert git(shared, 'status', '--porcelain') == ' M notes.txt\n'
    assert '+line 19 being edited elsewhere' in git(shared, 'diff')
    assert (shared / 'added.txt').read_text() == 'new\n' and not (shared / 'old.txt').exists()
    assert result['carried'] == ['notes.txt']
    assert not feature.exists() and git(shared, 'branch', '--list', 'feature') == ''


def test_overlapping_uncommitted_lines_refuse_without_changes(shared, feature):
    replace_line(feature / 'notes.txt', 2, 'line 2 from the branch')
    commit_all(feature, 'feature work')
    replace_line(shared / 'notes.txt', 2, 'line 2 being edited elsewhere')
    before = state(shared)

    with pytest.raises(land.Refused) as refused:
        land.land('feature', cwd=shared)

    assert any(path.startswith('notes.txt: ') and 'same lines' in path
               for path in refused.value.paths)
    assert state(shared) == before
    assert feature.exists()


def test_branch_conflict_refuses_before_touching_the_checkout(shared, feature):
    replace_line(feature / 'notes.txt', 5, 'line 5 from the branch')
    commit_all(feature, 'feature work')
    replace_line(shared / 'notes.txt', 5, 'line 5 committed meanwhile')
    git(shared, 'commit', '-qam', 'concurrent commit')
    before = state(shared)

    with pytest.raises(land.Refused, match='conflicts with the current branch') as refused:
        land.land('feature', cwd=shared)

    assert refused.value.paths == ['notes.txt']
    assert 'generated' not in refused.value.advice
    assert state(shared) == before  # also proves the scratch worktree is gone


def test_conflict_only_in_generated_files_asks_for_regeneration(shared, feature):
    (feature / 'keys.generated.json').write_text('{\n  "branch": 1\n}\n')
    commit_all(feature, 'branch regenerated its output')
    (shared / '.gitattributes').write_text('*.generated.json linguist-generated=true\n')
    (shared / 'keys.generated.json').write_text('{\n  "main": 1\n}\n')
    git(shared, 'add', '-A')
    git(shared, 'commit', '-qm', 'main regenerated its output')
    before = state(shared)

    with pytest.raises(land.Refused) as refused:
        land.land('feature', cwd=shared)

    assert refused.value.paths == ['keys.generated.json']
    assert refused.value.advice.startswith('Only generated files conflict')
    assert 'regenerate' in refused.value.advice
    assert state(shared) == before


def test_generated_i18n_conflict_regenerates_from_merged_sources(shared, feature):
    import json
    for directory in (shared / 'frontend/src/i18n/locales',
                      shared / 'frontend/src/i18n/generated', shared / 'scripts'):
        directory.mkdir(parents=True, exist_ok=True)
    (shared / 'frontend/src/i18n/languages.json').write_text(
        '{"languages":[{"code":"en","tag":"en","name":"English",'
        '"englishName":"English"}],"sources":["en"],"fallback":"en",'
        '"default":"en"}\n')
    (shared / 'frontend/src/i18n/locales/en.json').write_text(
        '{\n  "key0": "Value0",\n  "key1": "Value1",\n  "key2": "Value2"\n}\n')
    (shared / 'scripts/gen_i18n_contract.mjs').write_bytes(
        (ROOT / 'scripts/gen_i18n_contract.mjs').read_bytes())
    subprocess.run(['node', 'scripts/gen_i18n_contract.mjs'], cwd=shared,
                   check=True, capture_output=True, timeout=60)
    commit_all(shared, 'shared locale base')
    git(feature, 'merge', '-q', '--ff-only', 'main')
    assert rev(feature) == rev(shared)
    for repo, key, value in ((feature, 'task', 'Task'), (shared, 'main', 'Main')):
        source = json.loads((repo / 'frontend/src/i18n/locales/en.json').read_text())
        source = {key: value, **source} if repo == feature else {**source, key: value}
        (repo / 'frontend/src/i18n/locales/en.json').write_text(
            json.dumps(source, indent=2) + '\n')
        subprocess.run(['node', 'scripts/gen_i18n_contract.mjs'], cwd=repo,
                       check=True, capture_output=True, timeout=60)
        commit_all(repo, f'{key} authored locale')

    result = land.land('feature', cwd=shared)

    assert result['status'] == 'landed'
    catalog = json.loads((shared / 'frontend/src/i18n/generated/en.generated.json').read_text())
    assert catalog == {'task': 'Task', 'key0': 'Value0', 'key1': 'Value1',
                       'key2': 'Value2', 'main': 'Main'}
    assert '"task"' in (shared / 'frontend/src/i18n/contract.generated.ts').read_text()
    assert git(shared, 'status', '--porcelain') == ''


def test_generated_preview_conflict_uses_task_bytes(shared, feature):
    for repo, payload in ((feature, b'task preview'), (shared, b'canonical preview')):
        path = repo / 'designs/project/previews/workspace/light-320.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        commit_all(repo, 'generated preview')

    result = land.land('feature', cwd=shared)

    assert result['status'] == 'landed'
    assert (shared / 'designs/project/previews/workspace/light-320.png').read_bytes() == b'task preview'
    assert git(shared, 'status', '--porcelain') == ''


def test_untracked_file_in_the_way_refuses_unless_identical(shared, feature):
    (feature / 'added.txt').write_text('new\n')
    commit_all(feature, 'feature work')
    (shared / 'added.txt').write_text('someone else\n')
    before = state(shared)

    with pytest.raises(land.Refused) as refused:
        land.land('feature', cwd=shared)
    assert any(path.startswith('added.txt: ') for path in refused.value.paths)
    assert state(shared) == before

    (shared / 'added.txt').write_text('new\n')
    assert land.land('feature', cwd=shared)['status'] == 'landed'
    assert git(shared, 'status', '--porcelain') == ''


def test_staged_change_on_a_landed_path_refuses(shared, feature):
    replace_line(feature / 'notes.txt', 2, 'line 2 from the branch')
    commit_all(feature, 'feature work')
    replace_line(shared / 'notes.txt', 19, 'line 19 staged elsewhere')
    git(shared, 'add', 'notes.txt')
    before = state(shared)

    with pytest.raises(land.Refused, match='staged changes') as refused:
        land.land('feature', cwd=shared)

    assert refused.value.paths == ['notes.txt']
    assert state(shared) == before


def test_fast_forwardable_branch_still_records_a_merge_commit(shared, feature):
    (feature / 'added.txt').write_text('new\n')
    tip = commit_all(feature, 'feature work')
    before = rev(shared)

    result = land.land('feature', cwd=shared, keep=True)

    assert result['status'] == 'landed' and result['carried'] == []
    assert rev(shared, 'HEAD^1') == before and rev(shared, 'HEAD^2') == tip
    assert rev(shared, 'HEAD^{tree}') == rev(shared, f'{tip}^{{tree}}')
    assert git(shared, 'status', '--porcelain') == ''
    assert feature.exists() and git(shared, 'branch', '--list', 'feature').strip()


def test_already_merged_branch_is_only_cleaned_up(shared, feature):
    (feature / 'added.txt').write_text('new\n')
    commit_all(feature, 'feature work')
    git(shared, 'merge', '-q', '--no-ff', '-m', 'merged by hand', 'feature')
    head = rev(shared)

    result = land.land('feature', cwd=shared)

    assert result['status'] == 'already-merged' and rev(shared) == head
    assert not feature.exists() and git(shared, 'branch', '--list', 'feature') == ''


def test_check_reports_without_changing_anything(shared, feature):
    replace_line(feature / 'notes.txt', 2, 'line 2 from the branch')
    commit_all(feature, 'feature work')
    replace_line(shared / 'notes.txt', 19, 'line 19 being edited elsewhere')
    before = state(shared)

    result = land.land('feature', cwd=shared, check=True)

    assert result['status'] == 'landable' and result['carried'] == ['notes.txt']
    assert state(shared) == before


def test_index_lock_held_open_is_reported_busy_and_left_alone(shared, feature):
    replace_line(feature / 'notes.txt', 2, 'line 2 from the branch')
    commit_all(feature, 'feature work')
    lock = shared / '.git' / 'index.lock'
    before = state(shared)

    with open(lock, 'w') as held, pytest.raises(land.Refused) as refused:
        land.land('feature', cwd=shared)

    assert f'held by process {os.getpid()}' in refused.value.reason
    assert refused.value.advice == 'Rerun when it finishes.'
    assert lock.exists() and state(shared) == before


def test_orphaned_index_lock_is_reported_stale_and_left_alone(shared, feature):
    replace_line(feature / 'notes.txt', 2, 'line 2 from the branch')
    commit_all(feature, 'feature work')
    lock = shared / '.git' / 'index.lock'
    lock.write_text('')
    before = state(shared)

    with pytest.raises(land.Refused, match='no process of this user holds it open') as refused:
        land.land('feature', cwd=shared)

    assert 'remove that file' in refused.value.advice
    assert lock.exists() and state(shared) == before


def test_branch_moving_mid_landing_replans_onto_the_new_tip(shared, feature, monkeypatch):
    replace_line(feature / 'notes.txt', 2, 'line 2 from the branch')
    tip = commit_all(feature, 'feature work')
    original = land._plan
    moved = []

    def plan_then_commit_elsewhere(*args):
        steps = original(*args)
        if not moved:
            (shared / 'other.txt').write_text('committed meanwhile\n')
            git(shared, 'add', 'other.txt')
            git(shared, 'commit', '-qm', 'concurrent commit')
            moved.append(rev(shared))
        return steps

    monkeypatch.setattr(land, '_plan', plan_then_commit_elsewhere)

    result = land.land('feature', cwd=shared)

    assert rev(shared, 'HEAD^1') == moved[0] == result['preIntegration']
    assert rev(shared, 'HEAD^2') == tip
    assert git(shared, 'status', '--porcelain') == ''


def test_edit_after_planning_is_replanned_not_overwritten(shared, feature, monkeypatch):
    replace_line(feature / 'notes.txt', 2, 'line 2 from the branch')
    commit_all(feature, 'feature work')
    original = land._plan
    edited = []

    def plan_then_edit(*args):
        steps = original(*args)
        if not edited:
            replace_line(shared / 'notes.txt', 19, 'line 19 written after planning')
            edited.append(True)
        return steps

    monkeypatch.setattr(land, '_plan', plan_then_edit)

    result = land.land('feature', cwd=shared)

    working = (shared / 'notes.txt').read_text()
    assert 'line 2 from the branch' in working and 'line 19 written after planning' in working
    assert result['carried'] == ['notes.txt']


def test_mode_change_and_symlink_land_with_concurrent_content_edit(shared, feature):
    (feature / 'tool.sh').chmod(0o755)
    os.symlink('notes.txt', feature / 'notes-link')
    commit_all(feature, 'feature work')
    (shared / 'tool.sh').write_text('#!/bin/sh\necho edited elsewhere\n')

    land.land('feature', cwd=shared)

    assert (shared / 'tool.sh').read_text() == '#!/bin/sh\necho edited elsewhere\n'
    assert os.access(shared / 'tool.sh', os.X_OK)
    assert git(shared, 'ls-files', '-s', 'tool.sh').startswith('100755 ')
    assert os.readlink(shared / 'notes-link') == 'notes.txt'
    assert git(shared, 'status', '--porcelain') == ' M tool.sh\n'


def test_command_line_reports_and_exits_by_outcome(shared, feature):
    replace_line(feature / 'notes.txt', 2, 'line 2 from the branch')
    commit_all(feature, 'feature work')
    replace_line(shared / 'notes.txt', 2, 'line 2 being edited elsewhere')

    def run(*args):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=shared, env=env,
                              text=True, capture_output=True, timeout=120)

    refused = run('feature')
    assert refused.returncode == 1 and refused.stdout.startswith('Refused:')
    assert 'Nothing was changed' in refused.stdout and 'notes.txt' in refused.stdout

    (shared / 'notes.txt').write_text(LINES)
    landed = run('feature')
    assert landed.returncode == 0, landed.stdout + landed.stderr
    assert landed.stdout.startswith('Landed feature on main: merge ')
    assert 'deleted branch feature' in landed.stdout
