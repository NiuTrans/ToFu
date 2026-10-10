"""Pre-image races cannot read substituted files, links, or FIFOs."""
import os

import pytest

from lib.project_mod import run_command, snapshot_content

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not snapshot_content._ANCHORED_OPEN_SUPPORTED, reason='POSIX anchored open required')]


def _snapshot(tmp_path):
    (tmp_path / 'dir').mkdir()
    (tmp_path / 'dir' / 'file').write_bytes(b'original')
    return run_command._snapshot_targets(str(tmp_path), {'dir/file'})


def test_unchanged_file_capture_is_complete_and_bounded(tmp_path):
    snap = _snapshot(tmp_path)
    read = snapshot_content.read_snapshot_content
    assert read(str(tmp_path), 'dir/file', snap, 8) == b'original'
    assert read(str(tmp_path), 'dir/file', snap, 7) is None


def test_replacement_with_identical_size_and_mtime_is_rejected(tmp_path):
    snap = _snapshot(tmp_path)
    target = tmp_path / 'dir' / 'file'
    old = target.stat()
    # Keep the original inode alive: some filesystems immediately recycle an
    # unlinked inode and expose timestamps coarser than a Python call.
    target.rename(tmp_path / 'previous-file')
    target.write_bytes(b'new data')
    os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert snapshot_content.read_snapshot_content(str(tmp_path), 'dir/file', snap, 100) is None


@pytest.mark.parametrize('replacement', ['file_link', 'parent_link', 'fifo'])
def test_replacement_between_check_and_open_is_not_followed(tmp_path, monkeypatch, replacement):
    snap = _snapshot(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'file').write_bytes(b'secret!!')
    real_open = os.open
    injected = []
    def racing_open(path, flags, *args, **kwargs):
        trigger = 'dir' if replacement == 'parent_link' else 'file'
        if path == trigger and not injected:
            injected.append(path)
            if replacement == 'parent_link':
                (tmp_path / 'dir').rename(tmp_path / 'old-dir')
                (tmp_path / 'dir').symlink_to(outside, target_is_directory=True)
            else:
                (tmp_path / 'dir' / 'file').unlink()
                if replacement == 'file_link':
                    (tmp_path / 'dir' / 'file').symlink_to(outside / 'file')
                else:
                    os.mkfifo(tmp_path / 'dir' / 'file')
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(snapshot_content.os, 'open', racing_open)
    assert snapshot_content.read_snapshot_content(str(tmp_path), 'dir/file', snap, 100) is None
    assert injected == [trigger_name(replacement)]


def trigger_name(replacement):
    return 'dir' if replacement == 'parent_link' else 'file'


def test_file_mutation_during_read_discards_bytes(tmp_path, monkeypatch):
    snap = _snapshot(tmp_path)
    real_read = os.read
    def racing_read(fd, length):
        data = real_read(fd, length)
        (tmp_path / 'dir' / 'file').write_bytes(b'different content')
        return data
    monkeypatch.setattr(snapshot_content.os, 'read', racing_read)
    assert snapshot_content.read_snapshot_content(str(tmp_path), 'dir/file', snap, 100) is None


def test_root_replacement_rejects_new_root(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    snap = _snapshot(root)
    root.rename(tmp_path / 'old-root')
    root.mkdir()
    _snapshot(root)
    assert snapshot_content.read_snapshot_content(str(root), 'dir/file', snap, 100) is None


def test_fingerprint_metadata_remains_within_file_and_memory_caps(tmp_path, monkeypatch):
    import tracemalloc
    target = tmp_path / 'metadata'
    target.write_bytes(b'x')
    metadata = target.stat()
    class Entry:
        def __init__(self, index):
            self.name = f'file-{index}'
            self.path = os.path.join(str(tmp_path), self.name)
        def is_dir(self, **kwargs):
            return False
        def is_file(self, **kwargs):
            return True
        def stat(self, **kwargs):
            return metadata
    class Entries:
        def __enter__(self):
            return (Entry(i) for i in range(100_000))
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(run_command.os, 'scandir', lambda path: Entries())
    monkeypatch.setattr(run_command, '_SNAPSHOT_MAX_ENTRIES', 20_000)
    monkeypatch.setattr(run_command, '_SNAPSHOT_MAX_SECONDS', 60)
    tracemalloc.start()
    try:
        snap = run_command._snapshot_project_files(str(tmp_path))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(snap) == len(snap.fingerprints) == 5000
    assert peak < 4 * 1024 * 1024
    assert snap.complete is False
