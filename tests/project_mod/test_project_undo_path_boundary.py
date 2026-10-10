"""Undo journal paths do not expand the selected project authority."""
import os
import pytest

from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind', ['traversal', 'absolute', 'file_link', 'parent_link', 'wrong_root'])
def test_undo_refuses_paths_outside_selected_root(tmp_path, kind):
    root = tmp_path / 'project'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'file'
    target.write_text('external content')
    path = '../outside/file' if kind == 'traversal' else str(target)
    row = {'type': 'write_file', 'existed': False}
    if kind == 'file_link':
        (root / 'file').symlink_to(target)
        path = 'file'
    elif kind == 'parent_link':
        (root / 'linked').symlink_to(outside, target_is_directory=True)
        path = 'linked/file'
    elif kind == 'wrong_root':
        (root / 'file').write_text('keep project content')
        path = 'file'
        row['basePath'] = str(outside)
    row['path'] = path
    undone, failed = mods._undo_modifications_list(str(root), [row])
    assert not undone and len(failed) == 1
    assert target.read_text() == 'external content'
    if kind == 'wrong_root':
        assert (root / 'file').read_text() == 'keep project content'
    if kind == 'file_link':
        assert (root / 'file').is_symlink()


def test_reverse_patch_refuses_fifo_without_reading(tmp_path):
    if not hasattr(os, 'mkfifo'):
        pytest.skip('FIFO unavailable')
    os.mkfifo(tmp_path / 'pipe')
    undone, failed = mods._undo_modifications_list(str(tmp_path), [{
        'type': 'apply_diff', 'path': 'pipe',
        'reversePatch': {'search': 'old', 'replace': 'new'}}])
    assert not undone and len(failed) == 1


@pytest.mark.parametrize('operation', ['restore', 'delete', 'patch'])
def test_parent_swap_after_validation_cannot_mutate_external_file(tmp_path, monkeypatch, operation):
    from lib.project_mod.snapshot_content import _ANCHORED_OPEN_SUPPORTED
    if not _ANCHORED_OPEN_SUPPORTED:
        pytest.skip('Anchored operations unavailable')
    root = tmp_path / 'project'
    parent = root / 'dir'
    parent.mkdir(parents=True)
    (parent / 'file').write_text('current')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'file').write_text('current')
    row = {'type':'write_file', 'path':'dir/file', 'existed':operation != 'delete',
           'originalContent':'original'}
    if operation == 'patch':
        row = {'type':'apply_diff', 'path':'dir/file',
               'reversePatch':{'search':'current','replace':'original'}}
    resolve = mods._resolve_undo_target
    def swap(base, modification):
        target = resolve(base, modification)
        parent.rename(root / 'old-dir')
        parent.symlink_to(outside, target_is_directory=True)
        return target
    monkeypatch.setattr(mods, '_resolve_undo_target', swap)
    undone, failed = mods._undo_modifications_list(str(root), [row])
    assert not undone and len(failed) == 1
    assert (outside / 'file').read_text() == 'current'
    assert (root / 'old-dir' / 'file').read_text() == 'current'


def test_parent_swap_during_atomic_restore_cleans_only_its_pinned_temporary(tmp_path, monkeypatch):
    from lib.project_mod import undo_filesystem
    root = tmp_path / 'root'
    parent = root / 'dir'
    parent.mkdir(parents=True)
    (parent / 'file').write_text('current')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'file').write_text('external')
    fsync = os.fsync
    swapped = []
    def swap(fd):
        fsync(fd)
        if not swapped:
            swapped.append(True)
            parent.rename(root / 'old-dir')
            parent.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(undo_filesystem.os, 'fsync', swap)
    with pytest.raises(OSError):
        undo_filesystem.write_file(str(root), str(parent / 'file'), b'original')
    assert (outside / 'file').read_text() == 'external'
    assert (root / 'old-dir' / 'file').read_text() == 'current'
    assert not list((root / 'old-dir').glob('.tofu_undo_*'))


def test_reverse_patch_rechecks_content_before_publish(tmp_path):
    from lib.project_mod import undo_filesystem
    target = tmp_path / 'file'
    target.write_text('new edit')
    with pytest.raises(ValueError, match='content changed'):
        undo_filesystem.write_file(str(tmp_path), str(target), 'old', expected_content=b'previous')
    assert target.read_text() == 'new edit'


def test_editor_notification_after_restore_cannot_touch_substituted_parent(tmp_path, monkeypatch):
    from lib.project_mod import undo_filesystem
    root = tmp_path / 'root'
    parent = root / 'dir'
    parent.mkdir(parents=True)
    (parent / 'file').write_text('task')
    outside = tmp_path / 'outside'
    outside.mkdir()
    external = outside / 'file'
    external.write_text('outside')
    os.utime(external, ns=(10_000_000_000, 10_000_000_000))
    expected_mtime = external.stat().st_mtime_ns
    mods._record_modification(str(root), 'write_file', 'dir/file',
                             original_content='before', task_id='t')
    write = undo_filesystem.write_file
    def replace_parent_after_write(base, target, data, **kwargs):
        write(base, target, data, **kwargs)
        parent.rename(root / 'old-dir')
        parent.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(undo_filesystem, 'write_file', replace_parent_after_write)
    mods.undo_task_modifications(str(root), 't')
    assert external.stat().st_mtime_ns == expected_mtime
    assert external.read_text() == 'outside'
    assert (root / 'old-dir' / 'file').read_text() == 'before'
