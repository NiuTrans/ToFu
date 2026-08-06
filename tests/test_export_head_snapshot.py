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
    r = tmp_path / 'repo'
    (r / 'lib').mkdir(parents=True)
    (r / 'lib' / '__init__.py').write_text('', encoding='utf-8')
    (r / 'lib' / 'core.py').write_text('VALUE = "committed"\n',
                                       encoding='utf-8')
    _git(r, 'init', '-q')
    _git(r, 'add', '.')
    _git(r, 'commit', '-qm', 'init')
    # Incident shape: dirty tracked edit references an untracked new module.
    (r / 'lib' / 'core.py').write_text(
        'VALUE = "wip"\nimport lib.wip_module\n', encoding='utf-8')
    (r / 'lib' / 'wip_module.py').write_text('X = 1\n', encoding='utf-8')
    return r


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


def test_snapshot_pins_the_archived_sha(repo, monkeypatch):
    import export as exp
    monkeypatch.setattr(exp, '_EXPORT_SOURCE_SHA', None)
    snap = exp._stage_head_snapshot(repo)
    try:
        want = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
        assert exp._EXPORT_SOURCE_SHA == want, (
            'the integrity check must compare against the SAME commit the '
            'archive was taken from — a mid-export sibling commit otherwise '
            'flags files the snapshot legitimately predates')
    finally:
        import shutil
        shutil.rmtree(snap, ignore_errors=True)


def test_integrity_check_lists_from_the_snapshot_sha():
    """Source-anchored: _verify_exported_py_integrity must ls-tree the
    recorded snapshot sha, not live HEAD."""
    text = (Path(__file__).resolve().parent.parent
            / 'export.py').read_text(encoding='utf-8')
    idx = text.find('def _verify_exported_py_integrity')
    assert idx > 0
    window = text[idx:idx + 2000]
    assert "_EXPORT_SOURCE_SHA or 'HEAD'" in window, (
        'integrity check regressed to live HEAD — mid-export commits will '
        'false-flag the tree again')


def test_worktree_flag_preserves_the_legacy_path():
    """--worktree must still copy the worktree (intentional WIP publish)."""
    text = (Path(__file__).resolve().parent.parent
            / 'export.py').read_text(encoding='utf-8')
    assert "'--worktree'" in text, 'the --worktree escape hatch is gone'
    fn = text.find('def _export_via_tar_with_sanitize')
    assert fn > 0 and 'worktree' in text[fn:fn + 3000], (
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
