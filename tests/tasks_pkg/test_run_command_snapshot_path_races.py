"""Whole-tree/scoped snapshots cannot follow a directory swapped into a link."""
import os
import pytest

from lib.project_mod import run_command

pytestmark = pytest.mark.unit


def test_directory_swap_before_scan_does_not_observe_external_names(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    (root / 'dir').mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private-name').write_text('secret')
    scan, open_file = os.scandir, os.open
    injected = []
    def swap():
        if not injected:
            injected.append(True)
            (root / 'dir').rename(root / 'old-dir')
            (root / 'dir').symlink_to(outside, target_is_directory=True)
    def race_scan(path):
        if not isinstance(path, int) and os.fspath(path) == str(root / 'dir'):
            swap()
        return scan(path)
    def race_open(path, flags, *args, **kwargs):
        if path == 'dir' and 'dir_fd' in kwargs:
            swap()
        return open_file(path, flags, *args, **kwargs)
    monkeypatch.setattr(run_command.os, 'scandir', race_scan)
    monkeypatch.setattr(run_command.os, 'open', race_open)
    snapshot = run_command._snapshot_project_files(str(root))
    assert injected
    assert not any('private-name' in name for name in snapshot)
    assert snapshot.complete is False
