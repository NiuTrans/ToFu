"""tests/test_export_untracked_file_basis.py — the export mirrors COMMITTED
source, never the dirty working tree.

WHY (incident 2026-08-05, epic pt_be8275eed168401e)
--------------------------------------------------
The export tar-copied the WORKING TREE and only excluded untracked ROOT
directories. An untracked FILE inside a tracked dir shipped verbatim: the
uncommitted ``tests/test_frontend_autopilot_run_notice.py`` rode the
opensource export into rangehow/ToFu and red-filed the public CI, and the
``tests/tmp*.js`` NC-harness temp copies were published the same way. The
published tree must be a mirror of committed source — if a file is worth
publishing it is worth ``git add``ing; tracking is the keeper mechanism.

WHAT IS PINNED
--------------
* ``_untracked_nested_files`` lists an untracked file inside a tracked dir
  (the incident shape) and NOT: tracked files, gitignored files, or files
  under an already-excluded stray root dir (no double-counting).
* ``_untracked_file_excludes`` turns each into a tar ``--exclude=./path``
  arg, and the opensource-mode exclude bundle actually contains them.
* The dry-run preview consults the SAME set (a preview that says "will copy"
  while the real copy drops it is how operators get bitten).

Run:  python -B -m pytest tests/test_export_untracked_file_basis.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(is_opensource_build(),
                       reason='export.py is not shipped in opensource builds'),
]


@pytest.fixture()
def fake_repo(tmp_path):
    """A minimal git repo: one tracked file, one untracked nested file, one
    gitignored file, one stray untracked root dir, one untracked root file."""
    import export as exp

    repo = tmp_path / 'repo'
    (repo / 'tests').mkdir(parents=True)
    (repo / '.gitignore').write_text('ignored.txt\n', encoding='utf-8')
    (repo / 'tests' / 'test_real.py').write_text('# tracked\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'init'], cwd=repo, check=True)
    # The incident shapes:
    (repo / 'tests' / 'tmpab12cd34.js').write_text('// nc temp copy\n',
                                                   encoding='utf-8')
    (repo / 'tests' / 'test_wip_uncommitted.py').write_text('# wip\n',
                                                            encoding='utf-8')
    (repo / 'ignored.txt').write_text('ignored\n', encoding='utf-8')
    (repo / 'scratchpad').mkdir()
    (repo / 'scratchpad' / 'x.py').write_text('# stray dir content\n',
                                             encoding='utf-8')
    (repo / '_root_tmp.js').write_text('// root scratch\n', encoding='utf-8')
    return exp, repo


def test_nested_untracked_files_are_listed(fake_repo):
    exp, repo = fake_repo
    stray_dirs = exp._untracked_root_dirs(repo)
    assert stray_dirs == {'scratchpad'}
    nested = exp._untracked_nested_files(repo, stray_dirs)
    assert 'tests/tmpab12cd34.js' in nested, 'NC temp copy must be caught'
    assert 'tests/test_wip_uncommitted.py' in nested, (
        'uncommitted WIP inside a tracked dir must be caught')
    assert '_root_tmp.js' in nested, 'untracked root FILES count too'
    assert 'tests/test_real.py' not in nested, 'tracked files are the base set'
    assert 'ignored.txt' not in nested, 'gitignored files are already excluded'
    assert 'scratchpad/x.py' not in nested, (
        'covered by the root-dir rule — must not be double-counted')


def test_excludes_become_tar_args_and_reach_opensource_mode(fake_repo, monkeypatch):
    exp, repo = fake_repo
    args = exp._untracked_file_excludes(repo)
    assert '--exclude=./tests/tmpab12cd34.js' in args
    assert '--exclude=./tests/test_wip_uncommitted.py' in args
    # The opensource bundle wires them in (patch ROOT at the call site).
    monkeypatch.setattr(exp, 'ROOT', repo)
    excludes, _preserved = exp._build_tar_excludes_for_mode('opensource', repo / 'dest')
    assert '--exclude=./tests/tmpab12cd34.js' in excludes


def test_personal_mode_keeps_everything(fake_repo, monkeypatch):
    """Personal mode is a full self-use backup — the new rule must not bite it."""
    exp, repo = fake_repo
    monkeypatch.setattr(exp, 'ROOT', repo)
    excludes, _p = exp._build_tar_excludes_for_mode('personal', repo / 'dest')
    assert '--exclude=./tests/tmpab12cd34.js' not in excludes


def test_a_fully_committed_tree_excludes_nothing_extra(fake_repo):
    """NEUTER-flavored control: once the WIP files are committed, nothing is
    flagged — the rule only ever bites UNCOMMITTED content."""
    exp, repo = fake_repo
    subprocess.run(['git', 'add', '-A'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'wip'], cwd=repo, check=True)
    stray_dirs = exp._untracked_root_dirs(repo)
    nested = exp._untracked_nested_files(repo, stray_dirs)
    assert nested == set()


def test_dry_run_preview_consults_the_same_set():
    """The dry-run walk must consult _untracked_nested_files — a preview that
    says "will copy" while the real copy drops the file is how the WIP test
    got published unnoticed. Source-anchored: the walk's exclusion block must
    reference the helper (delete the wiring and this goes red)."""
    src = Path(__file__).resolve().parent.parent / 'export.py'
    text = src.read_text(encoding='utf-8')
    walk_idx = text.find('os.walk(ROOT)')
    assert walk_idx > 0, 'dry-run walk not found — structure changed, re-anchor'
    window = text[walk_idx - 3000:walk_idx + 3000]
    assert '_untracked_nested_files' in window, (
        'the dry-run preview no longer consults the untracked-file set — '
        'preview and real copy would diverge again')


# ── The root-anchored 'data' contract (2026-08-05, android Room incident) ──
# 'data' is excluded ROOT-ANCHORED ONLY (ALWAYS_EXCLUDE_ROOT_ONLY_DIRS,
# sibling commit 2e751b38): android/app/src/main/java/com/tofu/client/data/
# — a TRACKED Room package — must SHIP, while root data/ (databases,
# configs, runtime state) stays excluded. The gitignore drift guard covers
# the same invariant at full-tree scale; these pins name the contract AT
# the seam so a future regression names itself.

def test_data_exclusion_is_root_anchored_in_should_exclude():
    import export as exp
    for mode in ('internal', 'opensource'):
        assert exp._should_exclude('data/config/x.json', 'x.json', mode), (
            f'{mode}: root data/ must stay excluded (databases, configs, '
            'runtime state)')
        nested = 'android/app/src/main/java/com/tofu/client/data/Profile.kt'
        assert exp._should_exclude(nested, 'Profile.kt', mode) is None, (
            f'{mode}: a nested data/ package must SHIP — the 2026-08-05 '
            'android Room incident stripped 3 tracked Kotlin files')


def test_tar_excludes_anchor_root_data_without_the_nested_form(
        fake_repo, monkeypatch):
    exp, repo = fake_repo
    monkeypatch.setattr(exp, 'ROOT', repo)
    for mode in ('internal', 'opensource'):
        excludes, _p = exp._build_tar_excludes_for_mode(mode, repo / 'dest')
        assert '--exclude=./data' in excludes, (
            mode, 'the root-anchored data exclusion is lost')
        assert '--exclude=data' not in excludes, (
            mode, 'the UNANCHORED --exclude=data form is back — tar strips '
            'nested data/ packages wherever they live (the android Room '
            'incident class)')
