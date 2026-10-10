"""Scoped write-target snapshots for run_command change tracking.

A tracked command with a small, cleanly-resolvable write-target set is
snapshotted only at those targets (``_snapshot_targets``) instead of the
whole tree.  The diff+filter contract must produce byte-identical reported
changes while skipping the full-tree walk and its content reads.
"""
import os

import pytest

from lib.project_mod import tools as project_tools
from lib.project_mod.run_command import (

    _diff_snapshots,
    _filter_changes_by_targets,
    _snapshot_project_files,
    _snapshot_scope_rel_targets,
    _snapshot_targets,
    _SCOPED_SNAPSHOT_MAX_TARGETS,
)

# Intentional references to paths that do not (and must not)
# exist in the real repo — negative-case fixture anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = {'docs/c.md', 'docs/new.md'}


def _tree(root):
    (root / 'src' / 'pkg').mkdir(parents=True)
    (root / 'src' / 'pkg' / 'a.py').write_text('alpha\n', encoding='utf-8')
    (root / 'src' / 'b.py').write_text('beta\n', encoding='utf-8')
    (root / 'docs').mkdir()
    (root / 'docs' / 'c.md').write_text('gamma\n', encoding='utf-8')
    (root / 'node_modules').mkdir()
    (root / 'node_modules' / 'junk.js').write_text('junk\n', encoding='utf-8')
    # Change detection compares mtimes only: pin fixture files to an
    # explicitly old time so a later mutation always advances the clock,
    # independent of filesystem timestamp granularity.
    import time as _time
    old = _time.time() - 100
    for f in (root / 'src' / 'pkg' / 'a.py', root / 'src' / 'b.py',
              root / 'docs' / 'c.md'):
        os.utime(f, (old, old))


@pytest.mark.unit
class TestSnapshotScopeEligibility:
    def test_clean_file_and_dir_targets(self):
        assert _snapshot_scope_rel_targets({'src/a.py', 'docs/'}) == {
            os.path.normpath('src/a.py'), 'docs'}

    def test_glob_target_falls_back(self):
        assert _snapshot_scope_rel_targets({'*.pyc'}) is None
        assert _snapshot_scope_rel_targets({'src/?.py'}) is None
        assert _snapshot_scope_rel_targets({'src/[ab].py'}) is None

    def test_substitution_target_falls_back(self):
        assert _snapshot_scope_rel_targets({'$HOME/x'}) is None
        assert _snapshot_scope_rel_targets({'~/x'}) is None
        assert _snapshot_scope_rel_targets({'out-`date`.txt'}) is None

    def test_absolute_and_escaping_targets_fall_back(self):
        assert _snapshot_scope_rel_targets({'/etc/passwd'}) is None
        assert _snapshot_scope_rel_targets({'../outside'}) is None
        assert _snapshot_scope_rel_targets({'a/../../outside'}) is None

    def test_root_target_falls_back(self):
        assert _snapshot_scope_rel_targets({'.'}) is None

    def test_empty_and_over_cap_sets_fall_back(self):
        assert _snapshot_scope_rel_targets(set()) is None
        assert _snapshot_scope_rel_targets(
            {f'f{i}.txt' for i in range(_SCOPED_SNAPSHOT_MAX_TARGETS + 1)}) is None

    def test_quotes_are_tolerated(self):
        assert _snapshot_scope_rel_targets({'"src/a.py"'}) == {
            os.path.normpath('src/a.py')}


@pytest.mark.unit
class TestScopedSnapshot:
    def test_file_dir_and_missing_targets(self, tmp_path):
        _tree(tmp_path)
        snap = _snapshot_targets(str(tmp_path), {
            os.path.normpath('src/pkg'), 'docs', 'missing.txt'})
        assert os.path.normpath('src/pkg/a.py') in snap
        assert os.path.normpath('docs/c.md') in snap
        # Ignore rules still apply inside scoped walks.
        assert not any('node_modules' in rel for rel in snap)
        # Missing target contributes nothing (creation shows in after).
        assert 'missing.txt' not in snap

    def test_nested_targets_are_deduplicated(self, tmp_path):
        _tree(tmp_path)
        snap = _snapshot_targets(str(tmp_path), {'src', os.path.normpath('src/pkg')})
        keys = list(snap)
        assert sorted(keys) == sorted(set(keys))
        assert os.path.normpath('src/pkg/a.py') in snap

    def test_symlink_targets_are_skipped(self, tmp_path):
        _tree(tmp_path)
        link = tmp_path / 'link.py'
        try:
            link.symlink_to(tmp_path / 'src' / 'b.py')
        except OSError:
            pytest.skip('symlink unavailable')
        snap = _snapshot_targets(str(tmp_path), {'link.py'})
        assert snap == {}

    def test_scoped_diff_matches_full_diff_on_target_domain(self, tmp_path):
        _tree(tmp_path)
        targets = {os.path.normpath('src/b.py'), 'docs', 'created.txt'}
        before_scoped = _snapshot_targets(str(tmp_path), targets)
        before_full = _snapshot_project_files(str(tmp_path))
        # Mutate: modify in-scope, create in-scope, create out-of-scope.
        (tmp_path / 'src' / 'b.py').write_text('beta-changed\n', encoding='utf-8')
        (tmp_path / 'docs' / 'new.md').write_text('new\n', encoding='utf-8')
        (tmp_path / 'src' / 'outside.py').write_text('outside\n', encoding='utf-8')
        after_scoped = _snapshot_targets(str(tmp_path), targets)
        after_full = _snapshot_project_files(str(tmp_path))
        scoped_changes = _filter_changes_by_targets(
            _diff_snapshots(str(tmp_path), before_scoped, after_scoped),
            targets, str(tmp_path))
        full_changes = _filter_changes_by_targets(
            _diff_snapshots(str(tmp_path), before_full, after_full),
            targets, str(tmp_path))
        key = lambda ch: (ch['rel_path'], ch['change_type'])
        assert sorted(map(key, scoped_changes)) == sorted(map(key, full_changes))
        assert {key(ch) for ch in scoped_changes} == {
            (os.path.normpath('src/b.py'), 'modified'),
            (os.path.normpath('docs/new.md'), 'created'),
        }


@pytest.mark.unit
class TestRunCommandScopedTracking:
    def test_tracked_command_records_identical_changes(self, tmp_path, monkeypatch):
        _tree(tmp_path)
        records = []
        monkeypatch.setattr(
            'lib.project_mod.run_command._record_modification',
            lambda base, source, rel, original_content=None, conv_id=None,
                   task_id=None, action=None, fingerprint_byte_budget=None: records.append(
                       (rel, action, original_content)))
        result = project_tools.execute_tool(
            'run_command',
            {'command': 'echo changed > src/b.py && echo fresh > created.txt'},
            str(tmp_path))
        assert '[exit code: 0]' in result
        by_rel = {rel: (action, content) for rel, action, content in records}
        assert by_rel[os.path.normpath('src/b.py')] == ('modified', 'beta\n')
        assert by_rel['created.txt'] == ('created', None)
        # Out-of-target churn is not reported.
        assert not any(rel == 'docs/c.md' for rel, *_ in records)

    def test_glob_target_still_uses_full_snapshot(self, tmp_path, monkeypatch):
        _tree(tmp_path)
        spy = {'full': 0, 'scoped': 0}
        real_full = project_tools._snapshot_project_files
        real_scoped = project_tools._snapshot_targets
        monkeypatch.setattr(
            'lib.project_mod.tools._snapshot_project_files',
            lambda cwd: (spy.__setitem__('full', spy['full'] + 1), real_full(cwd))[1])
        monkeypatch.setattr(
            'lib.project_mod.tools._snapshot_targets',
            lambda cwd, targets: (
                spy.__setitem__('scoped', spy['scoped'] + 1),
                real_scoped(cwd, targets))[1])
        monkeypatch.setattr(
            'lib.project_mod.run_command._record_modification',
            lambda *a, **k: None)
        result = project_tools.execute_tool(
            'run_command', {'command': 'rm -f src/*.py'}, str(tmp_path))
        assert '[exit code: 0]' in result
        assert spy['full'] == 2 and spy['scoped'] == 0


@pytest.mark.unit
def test_literal_file_scope_never_walks_unrelated_tree(tmp_path, monkeypatch):
    (tmp_path / 'target').write_text('before')
    def forbidden_walk(path):
        raise AssertionError('A literal file target must not scan any directory')
    monkeypatch.setattr(os, 'scandir', forbidden_walk)
    assert _snapshot_targets(str(tmp_path), {'target'})['target'][1] == 6


@pytest.mark.unit
def test_scope_rejects_symlink_ancestor(tmp_path):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'real' / 'file').write_text('private')
    (tmp_path / 'alias').symlink_to(tmp_path / 'real', target_is_directory=True)
    assert _snapshot_targets(str(tmp_path), {'alias/file'}) == {}


@pytest.mark.unit
def test_scoped_targets_share_a_single_entry_budget(tmp_path, monkeypatch):
    from lib.project_mod import run_command
    for name in ['one', 'two', 'three']:
        (tmp_path / name).write_text(name)
    monkeypatch.setattr(run_command, '_SNAPSHOT_MAX_ENTRIES', 1)
    snap = _snapshot_targets(str(tmp_path), {'one', 'two', 'three'})
    assert len(snap) == 1
    assert snap.complete is False
