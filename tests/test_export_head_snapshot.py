"""tests/test_export_head_snapshot.py — internal/opensource export copies
COMMITTED HEAD (git archive), never the dirty working tree.

WHY (incident 2026-08-06, twice in one day)
-------------------------------------------
The export tar-copied the WORKTREE while excluding untracked files. In a
multi-sibling shared-worktree workflow that publishes half-written code:

1. Round-19 export shipped a sibling's dirty server.py importing
   lib/log_aggregates.py — still untracked, so skipped — and public CI
   red-filed with 800+ ModuleNotFoundError cascades.
2. The recovery re-export then shipped a dirty mid-edit
   lib/motion_video/_scene_author.py (F821 Undefined name `theme`),
   failing lint AND ~20 unit tests.

Copying committed HEAD via ``git archive`` makes the published tree immune
to worktree state by construction. ``--worktree`` preserves the legacy
behavior for an intentional WIP publish.

WHAT IS PINNED
--------------
* ``_stage_head_snapshot`` extracts exactly the pinned commit: committed
  content present at committed bytes; dirty edits and untracked files
  absent; ``_EXPORT_SOURCE_SHA`` records the same sha the archive used.
* A mid-export sibling commit must not confuse the integrity check: it
  lists from ``_EXPORT_SOURCE_SHA``, not live HEAD (source-anchored pin).
* ``--worktree`` keeps the worktree copy path (source-anchored pin).
* The torn-snapshot guard fires on a dirty tracked file referencing a
  skipped untracked module, and goes quiet once committed.

Run:  python -B -m pytest tests/test_export_head_snapshot.py
"""

from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/core.py', 'lib/wip_module.py'}

import subprocess
from pathlib import Path

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(is_opensource_build(),
                       reason='export.py is not shipped in opensource builds'),
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
        cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    """Committed base, then: dirty a tracked file + add an untracked module
    the dirty file imports (the torn-snapshot incident shape)."""
    tmp_dir = tmp_path / 'repo'
    (tmp_dir / 'lib').mkdir(parents=True)
    (tmp_dir / 'lib' / '__init__.py').write_text('', encoding='utf-8')
    (tmp_dir / 'lib' / 'core.py').write_text('VALUE = "committed"\n',
                                             encoding='utf-8')
    _git(tmp_dir, 'init', '-q')
    _git(tmp_dir, 'add', '.')
    _git(tmp_dir, 'commit', '-qm', 'init')
    # Incident shape: dirty tracked edit references an untracked new module.
    (tmp_dir / 'lib' / 'core.py').write_text(
        'VALUE = "wip"\nimport lib.wip_module\n', encoding='utf-8')
    (tmp_dir / 'lib' / 'wip_module.py').write_text('X = 1\n', encoding='utf-8')
    return tmp_dir


def test_snapshot_contains_committed_not_worktree(repo):
    import export as exp
    snap = exp._stage_head_snapshot(repo)
    try:
        assert snap is not None
        assert (snap / 'lib' / 'core.py').read_text() == 'VALUE = "committed"\n', (
            'the snapshot must carry COMMITTED bytes, not the dirty edit')
        assert not (snap / 'lib' / 'wip_module.py').exists(), (
            'untracked files must not appear in a HEAD snapshot')
    finally:
        import shutil
        shutil.rmtree(snap, ignore_errors=True)


def test_opted_in_linear_repo_exports_stable_not_development_head(repo):
    """Linear mode publishes its verified pointer without any merge/copy."""
    import export as exp

    stable = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(repo, 'update-ref', 'refs/tofu/stable', stable)
    _git(repo, 'update-ref', 'refs/tofu/workspace-checkpoint-baseline', stable)
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'development checkpoint')
    _git(repo, 'config', 'tofu.linearCheckpoint', 'true')

    snap = exp._stage_head_snapshot(repo)
    assert snap is not None
    try:
        assert (snap / 'lib' / 'core.py').read_text(encoding='utf-8') == \
            'VALUE = "committed"\n'
        assert not (snap / 'lib' / 'wip_module.py').exists()
        assert exp._state._EXPORT_SOURCE_SHA == stable
    finally:
        import shutil
        shutil.rmtree(snap, ignore_errors=True)


def test_unactivated_linear_setting_does_not_export_an_old_stable(repo):
    """The opt-in bit alone cannot regress export to an isolated-mode ref."""
    import export as exp

    old_stable = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(repo, 'update-ref', 'refs/tofu/stable', old_stable)
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'reviewed newer head')
    current_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(repo, 'config', 'tofu.linearCheckpoint', 'true')

    snap = exp._stage_head_snapshot(repo)
    assert snap is not None
    try:
        assert (snap / 'lib' / 'core.py').read_text(encoding='utf-8') == \
            'VALUE = "wip"\nimport lib.wip_module\n'
        assert (snap / 'lib' / 'wip_module.py').exists()
        assert exp._state._EXPORT_SOURCE_SHA == current_head
    finally:
        import shutil
        shutil.rmtree(snap, ignore_errors=True)


def test_invalid_explicit_export_ref_fails_closed(repo, monkeypatch):
    import export as exp

    monkeypatch.setenv('TOFU_EXPORT_SOURCE_REF', '--upload-pack=evil')
    monkeypatch.setattr(
        exp.tempfile, 'mkdtemp',
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError('unsafe source policy must not allocate a temp tree')),
    )
    with pytest.raises(exp.ExportIntegrityError,
                       match='source-ref configuration is unsafe'):
        exp._stage_head_snapshot(repo)


def test_activated_linear_repo_without_stable_fails_closed(repo):
    import export as exp

    head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(repo, 'update-ref', 'refs/tofu/workspace-checkpoint-baseline', head)
    _git(repo, 'config', 'tofu.linearCheckpoint', 'true')

    with pytest.raises(exp.ExportIntegrityError,
                       match='refs/tofu/stable is missing'):
        exp._stage_head_snapshot(repo)


def test_snapshot_pins_the_archived_sha(repo, monkeypatch):
    import export as exp
    monkeypatch.setattr(exp._state, '_EXPORT_SOURCE_SHA', None)
    snap = exp._stage_head_snapshot(repo)
    try:
        want = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
        assert exp._state._EXPORT_SOURCE_SHA == want, (
            'the integrity check must compare against the SAME commit the '
            'archive was taken from — a mid-export sibling commit otherwise '
            'flags files the snapshot legitimately predates')
    finally:
        import shutil
        shutil.rmtree(snap, ignore_errors=True)


def test_integrity_check_lists_from_the_snapshot_sha():
    """Source-anchored: _verify_exported_py_integrity must ls-tree the
    recorded snapshot sha, not live HEAD."""
    import inspect

    import export as exp
    text = inspect.getsource(exp._verify_exported_py_integrity)
    assert "_EXPORT_SOURCE_SHA or 'HEAD'" in text, (
        'integrity check regressed to live HEAD — mid-export commits will '
        'false-flag the tree again')


def test_worktree_flag_preserves_the_legacy_path():
    """--worktree must still copy the worktree (intentional WIP publish)."""
    import inspect

    import export as exp
    facade = (Path(__file__).resolve().parent.parent
              / 'export.py').read_text(encoding='utf-8')
    assert "'--worktree'" in facade, 'the --worktree escape hatch is gone'
    fn_src = inspect.getsource(exp._export_via_tar_with_sanitize)
    assert 'worktree' in fn_src, (
        '_export_via_tar_with_sanitize no longer takes the worktree switch')


def test_torn_snapshot_guard_fires_then_goes_quiet(repo):
    import export as exp
    pairs = exp._torn_snapshot_pairs(repo)
    assert ('lib/core.py', 'lib/wip_module.py') in pairs, (
        'dirty tracked file importing an untracked module must trip the guard')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'wip')
    assert exp._torn_snapshot_pairs(repo) == [], (
        'once committed, the guard must go quiet — it only bites in-flight work')
