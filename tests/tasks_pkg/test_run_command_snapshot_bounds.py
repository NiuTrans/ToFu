"""Snapshot work is bounded even for wide directories and partial walks."""
import os

import pytest

from lib.project_mod import run_command as owner

pytestmark = pytest.mark.unit


def test_wide_directory_iteration_stops_at_entry_budget(tmp_path, monkeypatch):
    visited = []

    class Entry:
        name = 'ignored'
        path = str(tmp_path / 'ignored')
        def is_dir(self, **kwargs):
            return False
        def is_file(self, **kwargs):
            return False

    class Entries:
        def __enter__(self):
            return self
        def __iter__(self):
            def generate():
                for i in range(100_000):
                    visited.append(i)
                    yield Entry()
            return generate()
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(owner, '_SNAPSHOT_MAX_ENTRIES', 40, raising=False)
    monkeypatch.setattr(owner.os, 'scandir', lambda path: Entries())
    import tracemalloc
    tracemalloc.start()
    try:
        snap = owner._snapshot_project_files(str(tmp_path))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024
    assert len(visited) <= 41
    assert snap.complete is False


def test_partial_snapshot_absence_is_not_creation_or_deletion(tmp_path):
    class Partial(dict):
        complete = False
    assert owner._diff_snapshots(str(tmp_path), Partial({'old': (1, 1)}),
                                 Partial({'new': (1, 1)})) == []


def test_size_change_with_preserved_mtime_is_modified(tmp_path):
    assert owner._diff_snapshots(str(tmp_path), {'file': (1, 1)},
                                 {'file': (1, 2)}) == [
        {'rel_path': 'file', 'change_type': 'modified'}]


def test_complete_walk_detects_creation_and_deletion(tmp_path):
    (tmp_path / 'old').write_text('before')
    before = owner._snapshot_project_files(str(tmp_path))
    (tmp_path / 'old').unlink()
    (tmp_path / 'new').write_text('after')
    after = owner._snapshot_project_files(str(tmp_path))
    assert before.complete and after.complete
    assert owner._diff_snapshots(str(tmp_path), before, after) == [
        {'rel_path': 'new', 'change_type': 'created'},
        {'rel_path': 'old', 'change_type': 'deleted'}]


def test_depth_and_access_limits_mark_snapshot_incomplete(tmp_path, monkeypatch):
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested' / 'file').write_text('x')
    monkeypatch.setattr(owner, '_SNAPSHOT_MAX_DEPTH', 0)
    assert owner._snapshot_project_files(str(tmp_path)).complete is False
    def denied(path):
        raise PermissionError('denied')
    monkeypatch.setattr(owner.os, 'scandir', denied)
    assert owner._snapshot_project_files(str(tmp_path)).complete is False


def test_command_surfaces_partial_tracking_without_false_history(tmp_path, monkeypatch):
    from lib.project_mod import tools
    monkeypatch.setattr(owner, '_SNAPSHOT_MAX_ENTRIES', 1)
    monkeypatch.setattr(tools, '_snapshot_scope_rel_targets', lambda targets: None)
    (tmp_path / 'one').write_text('1')
    (tmp_path / 'two').write_text('2')
    records = []
    monkeypatch.setattr(owner, '_record_modification', lambda *a, **k: records.append(k))
    result = tools.execute_tool('run_command', {'command': 'echo x > new'}, str(tmp_path))
    assert '[exit code: 0]' in result
    assert 'File tracking incomplete' in result
    assert not records


@pytest.mark.parametrize('scoped', [False, True])
def test_slow_snapshot_stops_before_exhausting_entry_count(tmp_path, monkeypatch, scoped):
    targets = {f'file-{i}' for i in range(20)}
    for name in targets:
        (tmp_path / name).write_text('content')
    ticks = iter(range(1000))
    monkeypatch.setattr(owner, '_snapshot_clock', lambda: next(ticks), raising=False)
    monkeypatch.setattr(owner, '_SNAPSHOT_MAX_SECONDS', 5, raising=False)
    monkeypatch.setattr(owner, '_SNAPSHOT_MAX_ENTRIES', 1000)
    snap = (owner._snapshot_targets(str(tmp_path), targets) if scoped
            else owner._snapshot_project_files(str(tmp_path)))
    assert len(snap) < len(targets)
    assert snap.complete is False
