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


@pytest.mark.parametrize('operation', ['restore', 'delete'])
@pytest.mark.parametrize('link_kind', ['parent', 'leaf'])
def test_redo_refuses_symlink_targets(tmp_path, monkeypatch, operation, link_kind):
    from lib import file_history as fh
    from lib.file_history import store

    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    root = tmp_path / 'project'
    parent = root / 'src'
    parent.mkdir(parents=True)
    target = parent / 'victim.txt'
    target.write_bytes(b'task postimage')
    assert fh.make_snapshot(str(root), task_id='task', rel_paths=['src/victim.txt'])
    target.unlink()
    if operation == 'delete':
        assert fh.make_snapshot(str(root), task_id='task', rel_paths=['src/victim.txt'])
    outside = tmp_path / 'outside'
    outside.mkdir()
    external = outside / 'victim.txt'
    external.write_bytes(b'outside sentinel')
    os.utime(external, ns=(10_000_000_000, 10_000_000_000))
    before_stat = external.stat()
    if link_kind == 'parent':
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
        link = parent
    else:
        target.symlink_to(external)
        link = target
    tracked_before = store.load_tracked(str(root))
    snapshots_before = list(store.iter_snapshots(str(root)))

    result = mods.redo_task_modifications(str(root), 'task')

    assert external.read_bytes() == b'outside sentinel'
    assert external.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert link.is_symlink()
    assert result['ok'] is False
    assert result.get('redone', 0) == 0
    assert result['error']
    assert result['details']['failed'][0]['path'] == 'src/victim.txt'
    assert store.load_tracked(str(root)) == tracked_before
    assert list(store.iter_snapshots(str(root))) == snapshots_before


def _redo_snapshot(root, operation):
    from lib import file_history as fh
    parent = root / 'src'
    parent.mkdir(parents=True)
    target = parent / 'victim.txt'
    target.write_bytes(b'task postimage')
    assert fh.make_snapshot(str(root), task_id='task', rel_paths=['src/victim.txt'])
    if operation == 'delete':
        target.unlink()
        assert fh.make_snapshot(str(root), task_id='task', rel_paths=['src/victim.txt'])
    target.write_bytes(b'current')
    return parent, target


@pytest.mark.parametrize('operation', ['restore', 'delete', 'notification', 'metadata'])
def test_redo_parent_swap_fails_without_external_mutation(tmp_path, monkeypatch, operation):
    from lib.file_history import store
    from lib.project_mod import undo_filesystem as fs

    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    root = tmp_path / 'project'
    parent, target = _redo_snapshot(root, operation)
    outside = tmp_path / 'outside'
    outside.mkdir()
    external = outside / 'victim.txt'
    external.write_bytes(b'outside sentinel')
    os.utime(external, ns=(10_000_000_000, 10_000_000_000))
    metadata = external.stat()
    tracked_before = store.load_tracked(str(root))
    snapshots_before = list(store.iter_snapshots(str(root)))
    swapped = []

    def swap():
        if not swapped:
            swapped.append(True)
            parent.rename(root / 'old-src')
            parent.symlink_to(outside, target_is_directory=True)

    if operation == 'restore':
        original = os.fsync
        def fsync(fd):
            original(fd)
            swap()
        monkeypatch.setattr(fs.os, 'fsync', fsync)
    elif operation == 'delete':
        original = fs._stat_regular
        def stat_regular(fd, name):
            result = original(fd, name)
            swap()
            return result
        monkeypatch.setattr(fs, '_stat_regular', stat_regular)
    else:
        name = 'write_file' if operation == 'notification' else 'touch_file'
        original = getattr(fs, name)
        def finish_then_swap(*args, **kwargs):
            result = original(*args, **kwargs)
            swap()
            return result
        monkeypatch.setattr(fs, name, finish_then_swap)

    result = mods.redo_task_modifications(str(root), 'task')

    assert swapped
    assert result['ok'] is False and result['redone'] == 0
    assert result['details']['failed']
    assert external.read_bytes() == b'outside sentinel'
    assert external.stat().st_mtime_ns == metadata.st_mtime_ns
    assert parent.is_symlink()
    assert not list((root / 'old-src').glob('.tofu_undo_*'))
    assert store.load_tracked(str(root)) == tracked_before
    assert list(store.iter_snapshots(str(root))) == snapshots_before
    expected = b'current' if operation in ('restore', 'delete') else b'task postimage'
    assert (root / 'old-src' / target.name).read_bytes() == expected


@pytest.mark.parametrize('key', ['../outside/victim.txt', 'src/../../outside/victim.txt',
                                  'src/../victim.txt', './victim.txt', '', 'src//victim.txt',
                                  'src\\victim.txt', '/absolute/victim.txt'])
def test_redo_refuses_unsafe_persisted_keys(tmp_path, monkeypatch, key):
    from lib.file_history import store
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    store.append_snapshot_record(str(tmp_path), {
        'id': 'unsafe', 'taskId': 'task', 'files': {key: 0},
    })
    before = list(store.iter_snapshots(str(tmp_path)))
    result = mods.redo_task_modifications(str(tmp_path), 'task')
    assert result['ok'] is False and result['redone'] == 0
    assert result['details']['failed'][0]['path'] == key
    assert list(store.iter_snapshots(str(tmp_path))) == before


@pytest.mark.parametrize('operation', ['restore', 'delete'])
def test_redo_unavailable_anchoring_fails_visibly(tmp_path, monkeypatch, operation):
    from lib.project_mod import snapshot_content
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    _, target = _redo_snapshot(tmp_path, operation)
    monkeypatch.setattr(snapshot_content, '_ANCHORED_OPEN_SUPPORTED', False)
    result = mods.redo_task_modifications(str(tmp_path), 'task')
    assert result['ok'] is False and result['redone'] == 0
    assert result['details']['failed']
    assert target.read_bytes() == b'current'


@pytest.mark.parametrize('operation', ['restore', 'delete'])
def test_redo_safe_target_still_succeeds(tmp_path, monkeypatch, operation):
    from lib.file_history import store
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    parent, target = _redo_snapshot(tmp_path, operation)
    target.chmod(0o640)
    if operation == 'restore':
        target.unlink()
        parent.rmdir()
    result = mods.redo_task_modifications(str(tmp_path), 'task')
    assert result['ok'] is True and result['redone'] == 1
    assert result['snapshotId']
    if operation == 'restore':
        assert target.read_bytes() == b'task postimage'
        assert store.load_tracked(str(tmp_path))['src/victim.txt']['size'] == len(b'task postimage')
    else:
        assert not target.exists()
        assert store.load_tracked(str(tmp_path))['src/victim.txt']['deleted'] is True


def test_redo_partial_failure_keeps_rejected_evidence_and_no_success_snapshot(tmp_path, monkeypatch):
    from lib import file_history as fh
    from lib.file_history import store
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    (tmp_path / 'good').write_bytes(b'postimage')
    (tmp_path / 'bad').write_bytes(b'postimage')
    assert fh.make_snapshot(str(tmp_path), task_id='task', rel_paths=['good', 'bad'])
    (tmp_path / 'good').write_bytes(b'current')
    (tmp_path / 'bad').unlink()
    (tmp_path / 'bad').symlink_to(tmp_path / 'good')
    before = store.load_tracked(str(tmp_path))['bad']
    snapshots = list(store.iter_snapshots(str(tmp_path)))
    result = mods.redo_task_modifications(str(tmp_path), 'task')
    assert result['ok'] is False and result['redone'] == 1
    assert result['files'] == ['good']
    assert result['details']['failed'][0]['path'] == 'bad'
    assert (tmp_path / 'good').read_bytes() == b'postimage'
    assert (tmp_path / 'bad').is_symlink()
    assert store.load_tracked(str(tmp_path))['bad'] == before
    assert list(store.iter_snapshots(str(tmp_path))) == snapshots


@pytest.mark.parametrize('operation', ['restore', 'delete'])
@pytest.mark.parametrize('kind', ['dangling_link', 'directory', 'fifo'])
def test_redo_refuses_nonregular_targets(tmp_path, monkeypatch, operation, kind):
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    _, target = _redo_snapshot(tmp_path, operation)
    target.unlink()
    if kind == 'dangling_link':
        target.symlink_to(tmp_path / 'missing')
    elif kind == 'directory':
        target.mkdir()
    else:
        if not hasattr(os, 'mkfifo'):
            pytest.skip('FIFO unavailable')
        os.mkfifo(target)
    before = target.lstat()
    result = mods.redo_task_modifications(str(tmp_path), 'task')
    assert result['ok'] is False and result['redone'] == 0
    assert result['details']['failed']
    after = target.lstat()
    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)


@pytest.mark.parametrize('operation', ['restore', 'delete'])
def test_snapshot_rewind_uses_same_symlink_boundary(tmp_path, monkeypatch, operation):
    from lib import file_history as fh
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    root = tmp_path / 'project'
    parent = root / 'src'
    parent.mkdir(parents=True)
    target = parent / 'victim.txt'
    target.write_bytes(b'original')
    if operation == 'restore':
        assert fh.make_snapshot(str(root), task_id='prior', rel_paths=['src/victim.txt'])
    target.write_bytes(b'postimage')
    snapshot = fh.make_snapshot(str(root), task_id='task', rel_paths=['src/victim.txt'])
    assert snapshot
    target.unlink()
    parent.rmdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    external = outside / 'victim.txt'
    external.write_bytes(b'outside sentinel')
    parent.symlink_to(outside, target_is_directory=True)
    result = fh.rewind_to(str(root), snapshot)
    assert result['ok'] is False and not result['files']
    assert result['failed'][0]['path'] == 'src/victim.txt'
    assert external.read_bytes() == b'outside sentinel'
    assert parent.is_symlink()
